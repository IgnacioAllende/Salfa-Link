"""
SALFA Link — sincroniza datos desde John Deere Operations Center.

Lo ejecuta GitHub Actions dos veces al día. Las credenciales NO están en este
archivo: vienen de los secretos del repositorio (JD_CLIENT_ID, JD_CLIENT_SECRET,
JD_REFRESH_TOKEN).

Resultado: datos.json, con la flota (horómetro, horas por estado y TDF) y las
aplicaciones de la temporada con el detalle por entre-hilera.

Los mapas de cobertura ya procesados se guardan en cache_operaciones/ para no
volver a descargarlos. Los que Deere no entrega a tiempo se reintentan solos en
la corrida siguiente.
"""
import io
import json
import math
import os
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely import contains_xy
from shapely.geometry import LineString, Polygon

# ---------------------------------------------------------------- configuración
DESDE = "2026-06-01"               # inicio de la temporada
ORG_ID = "4041481"
API = "https://api.deere.com/platform"
TOKEN_URL = "https://signin.johndeere.com/oauth2/aus78tnlaysMraFhC1t7/v1/token"
ESPACIO = 5.0                      # metros entre hileras
ESPERA_MAX_MAPA_SEG = 240          # si Deere no entrega un mapa en 4 min, se reintenta en la próxima corrida
CACHE_DIR = "cache_operaciones"
SALIDA = "datos.json"
TZ = ZoneInfo("America/Santiago")

CAMPOS = {   # línea AB real de cada cuartel (punto A + rumbo), desde Operations Center
    "Giffoni 2016": {"heading": 16.6801,  "a": (-36.835554800495245, -72.17817518907725)},
    "Giffoni 2019": {"heading": 119.8386, "a": (-36.83326585634119, -72.21167564384166)},
    "Lewis 2016":   {"heading": 16.5766,  "a": (-36.83222987709997, -72.18284446225306)},
}
FLOTA = {"5090EN": "2346453", "6910": "2792651", "6170J": "2778685", "7230J": "1211344", "5082E": "3900599"}

H = {}   # encabezados con el token; se llenan en conectar()


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- conexión
def conectar():
    faltan = [k for k in ("JD_CLIENT_ID", "JD_CLIENT_SECRET", "JD_REFRESH_TOKEN") if not os.environ.get(k)]
    if faltan:
        sys.exit(f"Faltan secretos en el repositorio: {', '.join(faltan)}")
    r = requests.post(TOKEN_URL,
                      data={"grant_type": "refresh_token", "refresh_token": os.environ["JD_REFRESH_TOKEN"]},
                      auth=(os.environ["JD_CLIENT_ID"], os.environ["JD_CLIENT_SECRET"]), timeout=30)
    if r.status_code != 200:
        sys.exit(f"Deere rechazó las credenciales (HTTP {r.status_code}). Revisa los secretos JD_* del repositorio.")
    tok = r.json()
    if tok.get("refresh_token") and tok["refresh_token"] != os.environ["JD_REFRESH_TOKEN"]:
        # No se imprime el valor: los registros de un repositorio público son visibles.
        log("AVISO: Deere entregó un refresh token distinto. Si la próxima corrida falla al conectar, "
            "genera uno nuevo con Colab y actualiza el secreto JD_REFRESH_TOKEN.")
    H.update({"Accept": "application/vnd.deere.axiom.v3+json", "Authorization": "Bearer " + tok["access_token"]})


def get(url, params=None, extra=None):
    for intento in range(3):
        r = requests.get(url, headers={**H, **(extra or {})}, params=params, timeout=180)
        if r.status_code != 429:
            return r
        time.sleep(int(r.headers.get("Retry-After", 30)))   # Deere pide esperar si hay exceso de consultas
    return r


def todas_las_paginas(url, params=None, extra=None, maximo=5000):
    out = []
    while url and len(out) < maximo:
        r = get(url, params, extra)
        if r.status_code != 200:
            break
        p = r.json()
        out += p.get("values", [])
        url = next((l["uri"] for l in p.get("links", []) if l.get("rel") == "nextPage"), None)
        params = None   # el link de la página siguiente ya trae los parámetros
    return out


# ---------------------------------------------------------------- flota
def a_utc(d):
    return d.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def mediciones(pid, desde, hasta):
    r = get(f"{API}/machines/{pid}/machineMeasurements",
            {"embed": "measurementDefinition", "interval": "aggregated",
             "startDate": a_utc(desde), "endDate": a_utc(hasta)}, {"x-deere-no-paging": "true"})
    if r.status_code != 200:
        return {"error": r.status_code}
    out = {}
    for v in r.json().get("values", []):
        md = v.get("machineMeasurementDefinition", {})
        if md.get("name") not in ("Machine Utilization", "Rear PTO"):
            continue
        defs = md.get("bucketDefinitions", {}).get("bucketDefinitions", [])
        nombres = {str(d.get("sequenceNumber", i)): d.get("description") for i, d in enumerate(defs)}
        seg = {}
        for itv in v.get("series", {}).get("intervals", []):
            for b in itv.get("buckets", {}).get("buckets", []):
                k = nombres.get(str(b.get("sequenceNumber")), "seq" + str(b.get("sequenceNumber")))
                seg[k] = seg.get(k, 0) + float(b.get("value", 0))
        out[md["name"]] = {k: round(s / 3600, 2) for k, s in seg.items()}
    return out


def bajar_flota(ahora):
    hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    periodos = {"hoy": hoy, "semana": hoy - timedelta(days=hoy.weekday()), "mes": hoy.replace(day=1),
                "temporada": datetime.fromisoformat(DESDE).replace(tzinfo=TZ)}
    flota = {}
    for nombre, pid in FLOTA.items():
        eh = get(f"{API}/machines/{pid}/engineHours", {"lastKnown": "true"})
        horometro = None
        if eh.status_code == 200 and eh.json().get("values"):
            v0 = eh.json()["values"][0]
            horometro = {"horas": v0.get("reading", {}).get("valueAsDouble"), "fecha": v0.get("reportTime")}
        flota[nombre] = {"principalId": pid, "horometro": horometro,
                         "periodos": {k: mediciones(pid, d, ahora) for k, d in periodos.items()}}
        estado = "sin telemetría" if horometro is None else f"horómetro {horometro['horas']:.1f} h"
        log(f"  {nombre}: {estado}")
    return flota


# ---------------------------------------------------------------- posiciones y recorridos
def _punto(v):
    p = v.get("point") or v.get("location") or v
    lat, lon = p.get("lat"), p.get("lon")
    t = v.get("gpsFixTimestamp") or v.get("eventTimestamp") or v.get("timestamp") or v.get("time")
    if lat is None or lon is None or not t:
        return None
    estado = v.get("machineState")
    return {"lat": float(lat), "lon": float(lon), "t": t, "estado": estado}


def bajar_posiciones(ahora):
    """Recorridos de los últimos 7 días por tractor y su última posición conocida (hasta 30 días atrás)."""
    hoy = ahora.date()
    dias = [(hoy - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    desde7 = datetime.fromisoformat(dias[0]).replace(tzinfo=TZ)
    desde30 = ahora - timedelta(days=30)
    tractores = []
    for nombre, pid in FLOTA.items():
        puntos, fuente = [], None
        crudos = todas_las_paginas(f"{API}/machines/{pid}/breadcrumbs",
                                   {"startDate": a_utc(desde7), "endDate": a_utc(ahora)}, {"x-deere-no-paging": "true"})
        puntos = [x for x in map(_punto, crudos) if x]
        if puntos:
            fuente = "breadcrumbs"
        historial = [x for x in map(_punto, todas_las_paginas(f"{API}/machines/{pid}/locationHistory",
                     {"startDate": a_utc(desde30), "endDate": a_utc(ahora)}, {"x-deere-no-paging": "true"})) if x]
        if not puntos:
            puntos = [x for x in historial if x["t"] >= a_utc(desde7)]
            fuente = "locationHistory" if puntos else None
        todos = sorted(puntos + historial, key=lambda x: x["t"])
        ultimo = todos[-1] if todos else None
        rutas = {}
        for x in sorted(puntos, key=lambda x: x["t"]):
            dia = datetime.fromisoformat(x["t"].replace("Z", "+00:00")).astimezone(TZ).date().isoformat()
            if dia in dias:
                rutas.setdefault(dia, []).append([round(x["lat"], 7), round(x["lon"], 7)])
        for dia, pts in rutas.items():          # máximo ~600 puntos por día para que el dashboard siga liviano
            paso = max(1, len(pts) // 600)
            rutas[dia] = pts[::paso]
        tractores.append({"name": nombre, "fuente": fuente, "ultimo": ultimo, "rutas": rutas})
        detalle = f"{sum(len(v) for v in rutas.values())} puntos en 7 días ({fuente})" if fuente else "sin recorridos en 7 días"
        log(f"  {nombre}: {detalle}" + (f", última posición {ultimo['t'][:16]}" if ultimo else ""))
    return {"dias": dias, "tractores": tractores}


# ---------------------------------------------------------------- aplicaciones
def proyector(a, heading):
    """Misma proyección local que usa el dashboard para dibujar las hileras."""
    lat0, lon0 = a
    m_lat, m_lon = 111320.0, 111320.0 * math.cos(math.radians(lat0))
    t = math.radians(heading)

    def f(lat, lon):
        x = (np.asarray(lon) - lon0) * m_lon
        y = (np.asarray(lat) - lat0) * m_lat
        return x * math.cos(t) - y * math.sin(t), x * math.sin(t) + y * math.cos(t)
    return f


def contorno(fid):
    try:
        vals = get(f"{API}/organizations/{ORG_ID}/fields/{fid}/boundaries").json().get("values", [])
        for b in sorted(vals, key=lambda b: not b.get("active", False)):
            for mp in b.get("multipolygons", []):
                for ring in mp.get("rings", []):
                    if ring.get("type", "exterior") == "exterior" and ring.get("points"):
                        return [(p["lat"], p["lon"]) for p in ring["points"]]
    except Exception as e:
        log(f"    no se pudo leer el contorno: {e}")
    return None


def descargar_mapa(op_id):
    """Devuelve el mapa de cobertura como GeoDataFrame, o None si Deere aún no lo entrega."""
    url = f"{API}/fieldOps/{op_id}"
    inicio = time.time()
    while True:
        s = requests.get(url, headers=H, allow_redirects=False, timeout=180)
        if s.status_code != 202:
            break
        if time.time() - inicio > ESPERA_MAX_MAPA_SEG:
            return None
        time.sleep(8)
    if s.status_code in (302, 303, 307):
        s = requests.get(s.headers["Location"], timeout=600)
    if s.status_code != 200 or s.content[:2] != b"PK":
        log(f"    Deere respondió {s.status_code} al pedir el mapa")
        return None
    carpeta = tempfile.mkdtemp()
    try:
        z = zipfile.ZipFile(io.BytesIO(s.content))
        z.extractall(carpeta)
        shp = [n for n in z.namelist() if n.endswith(".shp")][0]
        gdf = gpd.read_file(os.path.join(carpeta, shp))
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    return gdf


def por_entrehilera(gdf, f, poly):
    g = gdf[(gdf["AppliedRate"] > 0) & (gdf["VEHICLSPEED"] > 1)].copy()
    g["off"], g["dl"] = f(g.geometry.y.values, g.geometry.x.values)
    g["area_ha"] = g["DISTANCE"] * g["SWATHWIDTH"] / 10000
    g["litros"] = g["area_ha"] * g["AppliedRate"]
    c = g.groupby("IsoTime").agg(off=("off", "mean"), dl=("dl", "mean"),
                                 dist=("DISTANCE", "first"), vel=("VEHICLSPEED", "first")).reset_index()
    if poly is not None:
        c = c[contains_xy(poly, c["off"].values, c["dl"].values)].copy()
    c["n"] = np.round(c["off"] / ESPACIO).astype(int)
    g = g.merge(c[["IsoTime", "n"]], on="IsoTime", how="inner")
    sumas = g.groupby("n")[["litros", "area_ha"]].sum()
    c["t"] = pd.to_datetime(c["IsoTime"])
    c = c.sort_values("t")
    res = []
    for n, grp in c.groupby("n"):
        lit, ha = sumas.loc[n, "litros"], sumas.loc[n, "area_ha"]
        rec = {"n": int(n), "inicio": grp["IsoTime"].iloc[0], "fin": grp["IsoTime"].iloc[-1],
               "pasadas": int((grp["t"].diff().dt.total_seconds() > 120).sum() + 1),
               "litros": round(float(lit), 1), "ha": round(float(ha), 4),
               "L_ha": round(float(lit / ha), 1) if ha > 0 else None,
               "vel_kmh": round(float(grp["vel"].mean()), 2)}
        recorrido = float(grp["dist"].sum())
        if poly is not None:
            x0, y0, x1, y1 = poly.bounds
            largo = LineString([(n * ESPACIO, y0 - 10), (n * ESPACIO, y1 + 10)]).intersection(poly).length
            if largo > 0:
                rec["largo_m"] = round(largo, 1)
                rec["cobertura"] = round(recorrido / largo, 2)
        res.append(rec)
    return res, float(g["litros"].sum()), float(g["area_ha"].sum())


def leer_cache(op_id, modificado):
    ruta = os.path.join(CACHE_DIR, f"{op_id}.json")
    if not os.path.exists(ruta):
        return None
    with open(ruta, encoding="utf-8") as fh:
        c = json.load(fh)
    return c if c.get("modifiedTime") == modificado else None   # si Deere modificó la aplicación, se recalcula


def guardar_cache(op_id, datos):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, f"{op_id}.json"), "w", encoding="utf-8") as fh:
        json.dump(datos, fh, ensure_ascii=False)


def bajar_aplicaciones():
    campos_api = {c["name"]: c["id"] for c in todas_las_paginas(f"{API}/organizations/{ORG_ID}/fields")}
    operaciones, pendientes = [], 0
    for nombre, geo in CAMPOS.items():
        fid = campos_api.get(nombre)
        if not fid:
            log(f"  {nombre}: no aparece en Operations Center")
            continue
        f = proyector(geo["a"], geo["heading"])
        borde = contorno(fid)
        poly = Polygon(zip(*f([p[0] for p in borde], [p[1] for p in borde]))) if borde else None
        ops = [o for o in todas_las_paginas(f"{API}/organizations/{ORG_ID}/fields/{fid}/fieldOperations")
               if o.get("fieldOperationType") == "application" and o.get("startDate", "") >= DESDE]
        for o in sorted(ops, key=lambda o: o["startDate"]):
            det = get(f"{API}/fieldOperations/{o['id']}").json()
            tot = get(f"{API}/fieldOperations/{o['id']}/measurementTypes/ApplicationRateResult")
            pt = (tot.json().get("applicationProductTotals") or [{}])[0] if tot.status_code == 200 else {}
            val = lambda k: (pt.get(k) or {}).get("value")
            reg = {"campo": nombre, "id": o["id"], "inicio": o.get("startDate"), "fin": o.get("endDate"),
                   "productos": [p.get("name") for p in det.get("products", [])],
                   "maquinas": [m.get("name") for m in det.get("fieldOperationMachines", [])],
                   "deere_ha": val("area"), "deere_litros": val("totalMaterial"),
                   "deere_L_ha": val("averageMaterial"), "deere_vel_kmh": val("averageSpeed"),
                   "entrehileras": []}
            etiqueta = f"  {nombre} {o['startDate'][:10]}"
            cache = leer_cache(o["id"], det.get("modifiedTime"))
            if cache:
                reg.update({k: cache[k] for k in ("entrehileras", "litros_en_cuartel", "ha_en_cuartel")})
                log(f"{etiqueta}: ya procesada ({len(cache['entrehileras'])} entre-hileras)")
            else:
                gdf = descargar_mapa(o["id"])
                if gdf is None:
                    pendientes += 1
                    log(f"{etiqueta}: mapa aún no disponible en Deere, se reintenta en la próxima corrida")
                else:
                    filas, lit, ha = por_entrehilera(gdf, f, poly)
                    reg.update({"entrehileras": filas, "litros_en_cuartel": round(lit), "ha_en_cuartel": round(ha, 2)})
                    guardar_cache(o["id"], {"modifiedTime": det.get("modifiedTime"), "entrehileras": filas,
                                            "litros_en_cuartel": round(lit), "ha_en_cuartel": round(ha, 2)})
                    log(f"{etiqueta}: {len(filas)} entre-hileras procesadas")
                    del gdf
            operaciones.append(reg)
    return operaciones, pendientes


# ---------------------------------------------------------------- principal
def main():
    ahora = datetime.now(TZ)
    conectar()
    log("=== FLOTA ===")
    flota = bajar_flota(ahora)
    log("=== POSICIONES ===")
    try:
        posiciones = bajar_posiciones(ahora)
    except Exception as e:   # las posiciones son un extra: si fallan, el resto del dashboard sigue
        log(f"  no se pudieron bajar las posiciones: {e}")
        posiciones = {"dias": [], "tractores": []}
    log("=== APLICACIONES ===")
    operaciones, pendientes = bajar_aplicaciones()
    salida = {"generado": ahora.isoformat(timespec="milliseconds"), "desde": DESDE,
              "flota": flota, "posiciones": posiciones, "operaciones": operaciones}
    with open(SALIDA, "w", encoding="utf-8") as fh:
        json.dump(salida, fh, ensure_ascii=False, separators=(",", ":"))
    log(f"Listo: {len(operaciones)} aplicaciones, {pendientes} mapas pendientes en Deere.")


if __name__ == "__main__":
    main()
