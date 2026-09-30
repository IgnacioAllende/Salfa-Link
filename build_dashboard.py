"""SALFA Link — mete datos.json dentro de la plantilla y deja el sitio listo en site/index.html."""
import pathlib

datos = pathlib.Path("datos.json").read_text(encoding="utf-8").replace("</", "<\\/")
plantilla = pathlib.Path("plantilla_dashboard.html").read_text(encoding="utf-8")
if plantilla.count("__DATOS_REALES__") != 1:
    raise SystemExit("La plantilla debe tener exactamente una marca __DATOS_REALES__")
salida = pathlib.Path("site")
salida.mkdir(exist_ok=True)
(salida / "index.html").write_text(plantilla.replace("__DATOS_REALES__", datos), encoding="utf-8")
print("Dashboard armado en site/index.html")
