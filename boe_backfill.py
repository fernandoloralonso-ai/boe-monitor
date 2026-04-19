#!/usr/bin/env python3
"""
BOE Backfill — Recupera todo el año en curso.
Ejecutar UNA VEZ manualmente desde GitHub Actions antes del monitor diario.
Todos los archivos viven en la raíz del repositorio.
"""

import os, json, logging, time, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, date
from pathlib import Path
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE      = Path(__file__).parent
KW_FILE   = BASE / "keywords.json"
USER_CFG  = BASE / "user_config.json"
HIST_DIR  = BASE / "historial"
DATA_FILE = BASE / "data.json"

BOE_API  = "https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}"
BOE_BASE = "https://www.boe.es"
MAX      = 400
SLEEP    = 1.5


def cargar_keywords():
    src = USER_CFG if USER_CFG.exists() else KW_FILE
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    mapa = {}
    for tematica, val in data.get("tematicas", {}).items():
        if isinstance(val, dict):
            if val.get("activa", True) is False: continue
            kws = val.get("keywords", [])
        else:
            kws = val
        for k in kws:
            if isinstance(k, dict):
                if k.get("activa", True) and not k.get("texto","").startswith("_"):
                    mapa[k["texto"].lower()] = tematica
            elif isinstance(k, str) and not k.startswith("_"):
                mapa[k.lower()] = tematica
    for k in data.get("extras", []):
        if isinstance(k, dict):
            if k.get("activa", True): mapa[k["texto"].lower()] = "Extras"
        elif isinstance(k, str) and not k.startswith("_"):
            mapa[k.lower()] = "Extras"
    return mapa


def obtener_sumario(fecha):
    try:
        r = requests.get(BOE_API.format(fecha=fecha), timeout=30,
                         headers={"Accept":"application/xml","User-Agent":"BOEMonitor/2.0-backfill"})
        if r.status_code == 404: return []
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.warning(f"  Error {fecha}: {e}"); return []

    ctx = {}
    for sec in root.iter("seccion"):
        ns = sec.get("nombre", sec.get("id",""))
        for dep in sec.iter("departamento"):
            nd = dep.findtext("nombre","")
            for it in dep.iter("item"):
                bid = it.findtext("id","").strip()
                if bid: ctx[bid] = {"seccion":ns,"departamento":nd}

    items = []
    for item in root.iter("item"):
        bid = item.findtext("id","").strip()
        tit = item.findtext("titulo","").strip()
        uh  = item.findtext("urlHtml","").strip()
        up  = item.findtext("urlPdf","").strip()
        if not bid or not tit: continue
        c = ctx.get(bid,{})
        items.append({"id":bid,"titulo":tit,
                      "url_html":BOE_BASE+uh if uh.startswith("/") else uh,
                      "url_pdf": BOE_BASE+up if up.startswith("/") else up,
                      "departamento":c.get("departamento",""),"seccion":c.get("seccion","")})
    return items


def filtrar(items, kw_map):
    res = {}
    for item in items:
        txt = f"{item['titulo']} {item['departamento']} {item['seccion']}".lower()
        for kw, t in kw_map.items():
            if kw in txt:
                res.setdefault(t,[]).append(item); break
    return res


def cargar_data():
    if DATA_FILE.exists():
        try: return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except: pass
    return {"alertas":[],"stats":{}}


def guardar_data(data):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def recalcular(data):
    stats = {}
    for a in data["alertas"]:
        for i in a.get("items",[]):
            stats[i["tematica"]] = stats.get(i["tematica"],0)+1
    data["stats"] = stats
    data["ultima_update"] = datetime.now().isoformat()
    data["total_alertas"] = len(data["alertas"])


def procesar_dia(fecha_str, kw_map, data):
    HIST_DIR.mkdir(exist_ok=True)
    hp = HIST_DIR / f"{fecha_str}.json"
    if hp.exists():
        log.info(f"  {fecha_str} — ya procesado, saltando"); return False

    items = obtener_sumario(fecha_str)
    if not items: return False

    hp.write_text(json.dumps([i["id"] for i in items], ensure_ascii=False), encoding="utf-8")

    por_t = filtrar(items, kw_map)
    if not por_t: return False

    items_planos = []
    for t, tems in por_t.items():
        for item in tems:
            items_planos.append({"id":item["id"],"titulo":item["titulo"],
                "departamento":item["departamento"],"tematica":t,
                "url_html":item["url_html"],"url_pdf":item.get("url_pdf",""),
                "extracto":"","nuevo":True})

    fd = f"{fecha_str[:4]}-{fecha_str[4:6]}-{fecha_str[6:]}"
    entrada = {"fecha":fd,"total":len(items_planos),"nuevos":len(items_planos),
               "tematicas":list(por_t.keys()),"items":items_planos,
               "ejecutado":datetime.now().isoformat(),"backfill":True}
    data["alertas"] = [a for a in data["alertas"] if a["fecha"] != fd]
    data["alertas"].append(entrada)
    return True


def main():
    hoy = date.today()
    year_s = os.environ.get("BACKFILL_YEAR","").strip()
    year   = int(year_s) if year_s.isdigit() else hoy.year
    mon_s  = os.environ.get("BACKFILL_FROM_MONTH","1").strip()
    month  = int(mon_s) if mon_s.isdigit() else 1

    f_ini = date(year, month, 1)
    f_fin = min(date(year, 12, 31), hoy)
    total = (f_fin - f_ini).days + 1

    log.info(f"Backfill: {f_ini} → {f_fin} ({total} días)")
    kw_map = cargar_keywords()
    log.info(f"Keywords: {len(kw_map)} términos")

    data = cargar_data()
    proc = 0; con_items = 0
    fecha_act = f_ini

    while fecha_act <= f_fin:
        fs = fecha_act.strftime("%Y%m%d")
        log.info(f"[{proc+1}/{total}] {fs}…")
        try:
            if procesar_dia(fs, kw_map, data): con_items += 1
        except Exception as e:
            log.error(f"  Error en {fs}: {e}")
        proc += 1
        fecha_act += timedelta(days=1)
        if proc % 10 == 0:
            data["alertas"].sort(key=lambda x:x["fecha"],reverse=True)
            data["alertas"] = data["alertas"][:MAX]
            recalcular(data); guardar_data(data)
            log.info(f"  💾 Guardado parcial ({proc}/{total}, {con_items} con ítems)")
        time.sleep(SLEEP)

    data["alertas"].sort(key=lambda x:x["fecha"],reverse=True)
    data["alertas"] = data["alertas"][:MAX]
    recalcular(data); guardar_data(data)
    log.info(f"Backfill completado: {proc} días, {con_items} con resultados")


if __name__ == "__main__":
    main()
