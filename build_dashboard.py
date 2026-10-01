"""SALFA Link — mete datos.json dentro de las plantillas.
site/index.html    = versión 1 (plantilla_dashboard.html)
site/v2/index.html = versión 2 (plantilla_v2.html), si la plantilla existe."""
import pathlib

datos = pathlib.Path("datos.json").read_text(encoding="utf-8").replace("</", "<\\/")
salidas = {"plantilla_dashboard.html": pathlib.Path("site/index.html"),
           "plantilla_v2.html": pathlib.Path("site/v2/index.html")}
for plantilla, destino in salidas.items():
    p = pathlib.Path(plantilla)
    if not p.exists():
        print(f"{plantilla}: no está en el repositorio, se omite")
        continue
    texto = p.read_text(encoding="utf-8")
    if texto.count("__DATOS_REALES__") != 1:
        raise SystemExit(f"{plantilla} debe tener exactamente una marca __DATOS_REALES__")
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto.replace("__DATOS_REALES__", datos), encoding="utf-8")
    print(f"Armado {destino}")
