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
DESDE = "2026-06-01"               # inicio de la temporada (totales de "temporada")
HISTORIAL_DESDE = "2024-01-01"     # desde cuándo se baja historia (aplicaciones y horas por día)
DIAS_RECIENTES = 35                # horas por día: solo se vuelven a pedir los últimos días; lo anterior se reutiliza
ORG_ID = "4041481"
API = "https://partnerapi.deere.com/platform"   # producción (indicada por Deere al aprobar la aplicación)
API_RESPALDO = "https://api.deere.com/platform"  # dirección anterior, por si la nueva no responde
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
# Respaldo por si la API de equipos no responde. Normalmente la flota se descubre sola (descubrir_flota).
FLOTA_RESPALDO = {"5090EN": "2346453", "6910": "2792651", "6170J": "2778685", "7230J": "1211344", "5082E": "3900599"}
FLOTA = dict(FLOTA_RESPALDO)

H = {}   # encabezados con el token; se llenan en conectar()
SIN_TELEMETRIA = set()   # equipos sin JDLink: no se les piden mediciones ni posiciones
PREVIO = {}              # última corrida guardada (para reutilizar historia sin volver a pedirla)
ULTIMO_GUARDADO = os.path.join(CACHE_DIR, "ultimo_datos.json")   # base para las corridas livianas
DIAS_RECORRIDOS = 30   # días de recorridos que se conservan (se acumulan corrida a corrida, sin llamadas extra)


def conservar_recorridos(posiciones, anterior, ahora):
    """Suma a las posiciones nuevas los recorridos de días anteriores que ya se habían bajado."""
    limite = (ahora.date() - timedelta(days=DIAS_RECORRIDOS - 1)).isoformat()
    previos = {t["name"]: t for t in (anterior or {}).get("tractores", [])}
    for t in posiciones.get("tractores", []):
        viejo = previos.get(t["name"], {}).get("rutas", {})
        for dia, ruta in viejo.items():
            if dia >= limite and dia not in t.setdefault("rutas", {}):
                t["rutas"][dia] = ruta
        t["rutas"] = {d: r for d, r in sorted(t["rutas"].items()) if d >= limite}
    return posiciones


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
    elegir_base()


def elegir_base():
    """Usa la dirección de producción nueva; si no responde, vuelve a la anterior y lo avisa en el registro."""
    global API
    for base in (API, API_RESPALDO):
        try:
            r = requests.get(f"{base}/organizations/{ORG_ID}", headers=H, timeout=60)
        except Exception as e:
            log(f"  {base}: sin respuesta ({e})")
            continue
        if r.status_code == 200:
            if base != API:
                log(f"AVISO: {API} respondió con error; se usa {base}. Revisar con Deere.")
            API = base
            log(f"API: {API}")
            return
        log(f"  {base}: HTTP {r.status_code}")
    sys.exit("Ninguna dirección de la API de Deere respondió correctamente.")


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


def descubrir_flota():
    """Pregunta a Deere qué tractores tiene la organización, para que un tractor nuevo aparezca solo."""
    try:
        vals = todas_las_paginas("https://equipmentapi.deere.com/isg/equipment",
                                 {"organizationIds": ORG_ID, "categories": "machine", "deprecated": "false", "itemLimit": 5000})
    except Exception as e:
        log(f"  no se pudo consultar la lista de equipos ({e}); se usa la lista de respaldo")
        return
    encontrados = {}
    for v in vals:
        pid = v.get("principalId")
        if v.get("archived") or not pid:
            continue
        nombre = v.get("name") or (v.get("model") or {}).get("name") or str(pid)
        if nombre in encontrados:                       # dos tractores con el mismo nombre: se distinguen por serie
            nombre = f"{nombre} ({str(v.get('serialNumber', pid))[-5:]})"
        encontrados[nombre] = str(pid)
    if not encontrados:
        log("  la lista de equipos vino vacía; se usa la lista de respaldo")
        return
    conocidos = set(encontrados.values())
    for nombre, pid in FLOTA_RESPALDO.items():           # no perder los de respaldo si la API no los lista
        if pid not in conocidos:
            encontrados[nombre] = pid
    FLOTA.clear()
    FLOTA.update(encontrados)
    log(f"  flota descubierta en Deere: {', '.join(FLOTA)}")


def mediciones_diarias(pid, ahora, previo=None):
    """Horas por estado de cada día (Deere entrega buckets diarios en ventanas de menos de 31 días).
    Los días antiguos ya bajados se reutilizan; solo se piden los últimos DIAS_RECIENTES."""
    corte = (ahora - timedelta(days=DIAS_RECIENTES)).replace(hour=0, minute=0, second=0, microsecond=0)
    diario = {d: v for d, v in (previo or {}).items() if d < corte.date().isoformat()}
    inicio = datetime.fromisoformat(HISTORIAL_DESDE).replace(tzinfo=TZ)
    if previo:
        inicio = max(inicio, corte)
    while inicio < ahora:
        fin = min(inicio + timedelta(days=28), ahora)
        r = get(f"{API}/machines/{pid}/machineMeasurements",
                {"embed": "measurementDefinition", "startDate": a_utc(inicio), "endDate": a_utc(fin)},
                {"x-deere-no-paging": "true"})
        if r.status_code != 200:
            return None
        for v in r.json().get("values", []):
            md = v.get("machineMeasurementDefinition", {})
            if md.get("name") not in ("Machine Utilization", "Rear PTO"):
                continue
            defs = md.get("bucketDefinitions", {}).get("bucketDefinitions", [])
            nombres = {str(d.get("sequenceNumber", i)): d.get("description") for i, d in enumerate(defs)}
            for itv in v.get("series", {}).get("intervals", []):
                ini = itv.get("intervalStartDate")
                if not ini:
                    continue
                dia = (datetime.fromisoformat(ini.replace("Z", "+00:00")).astimezone(TZ) + timedelta(hours=1)).date().isoformat()
                for b in itv.get("buckets", {}).get("buckets", []):
                    k = nombres.get(str(b.get("sequenceNumber")), "seq" + str(b.get("sequenceNumber")))
                    if md["name"] == "Rear PTO":
                        k = "PTO_" + str(k)
                    reg = diario.setdefault(dia, {})
                    reg[k] = round(reg.get(k, 0) + float(b.get("value", 0)) / 3600, 2)
        inicio = fin
    return {d: v for d, v in sorted(diario.items()) if any(x > 0 for x in v.values())}


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
        if horometro is None:
            SIN_TELEMETRIA.add(nombre)
            flota[nombre] = {"principalId": pid, "horometro": None, "periodos": {}}
            log(f"  {nombre}: sin telemetría")
            continue
        flota[nombre] = {"principalId": pid, "horometro": horometro,
                         "periodos": {k: mediciones(pid, d, ahora) for k, d in periodos.items()}}
        if horometro is not None:
            diario = mediciones_diarias(pid, ahora, PREVIO.get("flota", {}).get(nombre, {}).get("diario"))
            if diario:
                flota[nombre]["diario"] = diario
        estado = "sin telemetría" if horometro is None else f"horómetro {horometro['horas']:.1f} h, {len(flota[nombre].get('diario', {}))} días con horas"
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


def trabajando(x):
    e = str(x.get("estado") or "").lower()
    return 1 if e.startswith("work") else (0 if e else None)


def tdf_por_hora(pid, dia):
    """Minutos de TDF trasera activada en cada hora de un día (Deere entrega buckets horarios en ventanas de 8 a 24 h)."""
    ini = datetime.fromisoformat(dia).replace(tzinfo=TZ)
    r = get(f"{API}/machines/{pid}/machineMeasurements",
            {"embed": "measurementDefinition", "startDate": a_utc(ini), "endDate": a_utc(ini + timedelta(hours=23, minutes=59))},
            {"x-deere-no-paging": "true"})
    if r.status_code != 200:
        return None
    horas = {}
    for v in r.json().get("values", []):
        md = v.get("machineMeasurementDefinition", {})
        if md.get("name") != "Rear PTO":
            continue
        defs = md.get("bucketDefinitions", {}).get("bucketDefinitions", [])
        nombres = {str(d.get("sequenceNumber", i)): str(d.get("description")) for i, d in enumerate(defs)}
        for itv in v.get("series", {}).get("intervals", []):
            if not itv.get("intervalStartDate"):
                continue
            h = datetime.fromisoformat(itv["intervalStartDate"].replace("Z", "+00:00")).astimezone(TZ).hour
            for b in itv.get("buckets", {}).get("buckets", []):
                if nombres.get(str(b.get("sequenceNumber"))) == "On" and float(b.get("value", 0)) > 0:
                    horas[str(h)] = horas.get(str(h), 0) + round(float(b.get("value", 0)) / 60)
    return horas


def completar_tdf(flota, posiciones, ahora, solo=None):
    """TDF por hora de los días con recorrido. Lo ya bajado se reutiliza; hoy y ayer se vuelven a pedir."""
    hoy = ahora.date().isoformat(); ayer = (ahora.date() - timedelta(days=1)).isoformat()
    limite = (ahora.date() - timedelta(days=DIAS_RECORRIDOS - 1)).isoformat()
    for t in posiciones.get("tractores", []):
        nombre = t["name"]; f = flota.get(nombre)
        if not f or not f.get("horometro") or (solo and nombre not in solo):
            continue
        if not ((f.get("periodos", {}).get("temporada") or {}).get("Rear PTO") or f.get("tdf_hora")):
            continue   # el equipo no reporta TDF
        previo = f.get("tdf_hora") or PREVIO.get("flota", {}).get(nombre, {}).get("tdf_hora") or {}
        tdf = {d: h for d, h in previo.items() if d >= limite}
        for dia in t.get("rutas", {}):
            if dia in tdf and dia not in (hoy, ayer):
                continue
            h = tdf_por_hora(f["principalId"], dia)
            if h is not None:
                tdf[dia] = h
        f["tdf_hora"] = tdf


def bajar_posiciones(ahora):
    """Recorridos de los últimos DIAS_RECORRIDOS días por tractor y su última posición conocida."""
    hoy = ahora.date()
    dias = [(hoy - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]          # 7 días (los usa la versión 1)
    ventana = [(hoy - timedelta(days=i)).isoformat() for i in range(DIAS_RECORRIDOS - 1, -1, -1)]
    desde7 = datetime.fromisoformat(ventana[0]).replace(tzinfo=TZ)                      # inicio de la ventana completa
    desde30 = desde7
    tractores = []
    for nombre, pid in FLOTA.items():
        if nombre in SIN_TELEMETRIA:
            tractores.append({"name": nombre, "fuente": None, "ultimo": None, "rutas": {}})
            continue
        puntos, fuente = [], None
        crudos = todas_las_paginas(f"{API}/machines/{pid}/breadcrumbs",
                                   {"startDate": a_utc(desde7), "endDate": a_utc(ahora)}, {"x-deere-no-paging": "true"})
        puntos = [x for x in map(_punto, crudos) if x]
        if puntos:
            fuente = "breadcrumbs"
        historial = [x for x in map(_punto, todas_las_paginas(f"{API}/machines/{pid}/locationHistory",
                     {"startDate": a_utc(desde30), "endDate": a_utc(ahora)}, {"x-deere-no-paging": "true"})) if x]
        # se suman las dos fuentes: algunos equipos envían pocos breadcrumbs pero sí su posición horaria
        vistos = {x["t"] for x in puntos}
        extra = [x for x in historial if x["t"] >= a_utc(desde7) and x["t"] not in vistos]
        if extra:
            puntos = puntos + extra
            fuente = "breadcrumbs + historial" if fuente else "historial de ubicación"
        todos = sorted(puntos + historial, key=lambda x: x["t"])
        ultimo = todos[-1] if todos else None
        rutas = {}
        for x in sorted(puntos, key=lambda x: x["t"]):
            loc = datetime.fromisoformat(x["t"].replace("Z", "+00:00")).astimezone(TZ)
            dia = loc.date().isoformat()
            if dia in ventana:
                rutas.setdefault(dia, []).append([round(x["lat"], 7), round(x["lon"], 7), loc.hour * 60 + loc.minute, trabajando(x)])
        for dia, pts in rutas.items():          # máximo ~600 puntos por día para que el dashboard siga liviano
            paso = max(1, len(pts) // 600)
            rutas[dia] = pts[::paso]
        tractores.append({"name": nombre, "fuente": fuente, "ultimo": ultimo, "rutas": rutas})
        detalle = f"{sum(len(v) for v in rutas.values())} puntos en {len(rutas)} días con recorrido ({fuente})" if fuente else f"sin recorridos en {DIAS_RECORRIDOS} días"
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
               if o.get("fieldOperationType") == "application" and o.get("startDate", "") >= HISTORIAL_DESDE]
        for o in sorted(ops, key=lambda o: o["startDate"]):
            guardada = leer_cache(o["id"], o.get("modifiedTime")) if o.get("modifiedTime") else None
            if guardada and guardada.get("reg"):
                operaciones.append(guardada["reg"])            # sin cambios en Deere: no se vuelve a pedir
                continue
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
                if not cache.get("reg"):
                    guardar_cache(o["id"], {**cache, "modifiedTime": o.get("modifiedTime") or cache.get("modifiedTime"), "reg": reg})
                log(f"{etiqueta}: ya procesada ({len(cache['entrehileras'])} entre-hileras)")
            else:
                gdf = descargar_mapa(o["id"])
                if gdf is None:
                    pendientes += 1
                    log(f"{etiqueta}: mapa aún no disponible en Deere, se reintenta en la próxima corrida")
                else:
                    filas, lit, ha = por_entrehilera(gdf, f, poly)
                    reg.update({"entrehileras": filas, "litros_en_cuartel": round(lit), "ha_en_cuartel": round(ha, 2)})
                    guardar_cache(o["id"], {"modifiedTime": o.get("modifiedTime") or det.get("modifiedTime"), "entrehileras": filas,
                                            "litros_en_cuartel": round(lit), "ha_en_cuartel": round(ha, 2), "reg": reg})
                    log(f"{etiqueta}: {len(filas)} entre-hileras procesadas")
                    del gdf
            operaciones.append(reg)
    return operaciones, pendientes


# ---------------------------------------------------------------- corrida liviana
def recorrido_de_hoy(pid, ahora):
    desde = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    rango = {"startDate": a_utc(desde), "endDate": a_utc(ahora)}
    crudos = todas_las_paginas(f"{API}/machines/{pid}/breadcrumbs", rango, {"x-deere-no-paging": "true"})
    pts = [x for x in map(_punto, crudos) if x]
    vistos = {x["t"] for x in pts}
    hist = todas_las_paginas(f"{API}/machines/{pid}/locationHistory", rango, {"x-deere-no-paging": "true"})
    pts += [x for x in map(_punto, hist) if x and x["t"] not in vistos]
    pts.sort(key=lambda x: x["t"])
    ruta = []
    for x in pts:
        loc = datetime.fromisoformat(x["t"].replace("Z", "+00:00")).astimezone(TZ)
        ruta.append([round(x["lat"], 7), round(x["lon"], 7), loc.hour * 60 + loc.minute, trabajando(x)])
    paso = max(1, len(ruta) // 600)
    return ruta[::paso], (pts[-1] if pts else None)


def corrida_liviana(ahora):
    """Cada 30 min: revisa el horómetro de cada tractor y baja el recorrido de hoy solo de los que se movieron."""
    with open(ULTIMO_GUARDADO, encoding="utf-8") as fh:
        datos = json.load(fh)
    hoy = ahora.date().isoformat()
    dias = [(ahora.date() - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    pos = datos.setdefault("posiciones", {"dias": [], "tractores": []})
    pos["dias"] = dias
    tractores = {t["name"]: t for t in pos.get("tractores", [])}
    cambios = 0
    for nombre, t in datos.get("flota", {}).items():
        if not t.get("horometro"):
            continue
        eh = get(f"{API}/machines/{t['principalId']}/engineHours", {"lastKnown": "true"})
        if eh.status_code != 200 or not eh.json().get("values"):
            continue
        v0 = eh.json()["values"][0]
        nuevo = {"horas": v0.get("reading", {}).get("valueAsDouble"), "fecha": v0.get("reportTime")}
        anterior = (t.get("horometro") or {}).get("horas")
        t["horometro"] = nuevo
        tr = tractores.setdefault(nombre, {"name": nombre, "fuente": "breadcrumbs", "ultimo": None, "rutas": {}})
        limite = (ahora.date() - timedelta(days=DIAS_RECORRIDOS - 1)).isoformat()
        tr["rutas"] = {d: r for d, r in tr.get("rutas", {}).items() if d >= limite}
        if nuevo["horas"] == anterior and hoy in tr["rutas"]:
            continue
        ruta, ultimo = recorrido_de_hoy(t["principalId"], ahora)
        if ruta:
            tr["rutas"][hoy] = ruta
            tr["ultimo"] = ultimo
            tr["fuente"] = "breadcrumbs"
        cambios += 1
        log(f"  {nombre}: horómetro {nuevo['horas']:.1f} h, {len(ruta)} puntos hoy")
    pos["tractores"] = list(tractores.values())
    movidos = {n for n, t in tractores.items() if hoy in t.get("rutas", {})}
    try:
        completar_tdf(datos.get("flota", {}), {"tractores": [{"name": n, "rutas": {hoy: 1}} for n in movidos]}, ahora, solo=movidos)
    except Exception as e:
        log(f"  no se pudo actualizar la TDF: {e}")
    datos["generado"] = ahora.isoformat(timespec="milliseconds")
    log(f"Corrida liviana: {cambios} equipos actualizados.")
    return datos


# ---------------------------------------------------------------- principal
def guardar(salida):
    for ruta in (SALIDA, ULTIMO_GUARDADO):
        os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump(salida, fh, ensure_ascii=False, separators=(",", ":"))


def main():
    ahora = datetime.now(TZ)
    if os.path.exists(ULTIMO_GUARDADO):
        try:
            with open(ULTIMO_GUARDADO, encoding="utf-8") as fh:
                PREVIO.update(json.load(fh))
        except Exception:
            pass
    conectar()
    if os.environ.get("MODO", "completo") == "liviano":
        if os.path.exists(ULTIMO_GUARDADO):
            log("=== CORRIDA LIVIANA ===")
            guardar(corrida_liviana(ahora))
            return
        log("No hay una corrida completa previa guardada: se hace una completa.")
    log("=== FLOTA ===")
    descubrir_flota()
    flota = bajar_flota(ahora)
    log("=== POSICIONES ===")
    try:
        posiciones = bajar_posiciones(ahora)
    except Exception as e:   # las posiciones son un extra: si fallan, el resto del dashboard sigue
        log(f"  no se pudieron bajar las posiciones: {e}")
        posiciones = {"dias": [], "tractores": []}
    if os.path.exists(ULTIMO_GUARDADO):
        try:
            with open(ULTIMO_GUARDADO, encoding="utf-8") as fh:
                posiciones = conservar_recorridos(posiciones, json.load(fh).get("posiciones"), ahora)
        except Exception as e:
            log(f"  no se pudieron recuperar recorridos anteriores: {e}")
    try:
        completar_tdf(flota, posiciones, ahora)
        log(f"  TDF por hora: {sum(len(f.get('tdf_hora', {})) for f in flota.values())} días-equipo")
    except Exception as e:
        log(f"  no se pudo bajar la TDF por hora: {e}")
    log("=== APLICACIONES ===")
    operaciones, pendientes = bajar_aplicaciones()
    salida = {"generado": ahora.isoformat(timespec="milliseconds"), "desde": HISTORIAL_DESDE, "temporada_desde": DESDE,
              "flota": flota, "posiciones": posiciones, "operaciones": operaciones}
    guardar(salida)
    log(f"Listo: {len(operaciones)} aplicaciones, {pendientes} mapas pendientes en Deere.")


if __name__ == "__main__":
    main()
