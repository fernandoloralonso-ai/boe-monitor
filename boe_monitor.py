#!/usr/bin/env python3
"""
BOE Monitor — Script principal para GitHub Actions
Lee keywords.json / user_config.json, filtra el BOE, envía email
y actualiza data.json para el dashboard GitHub Pages.
Todos los archivos viven en la raíz del repositorio.
"""

import os, sys, json, smtplib, logging, xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE         = Path(__file__).parent
KW_FILE      = BASE / "keywords.json"
USER_CFG     = BASE / "user_config.json"
HIST_DIR     = BASE / "historial"
DATA_FILE    = BASE / "data.json"

BOE_API      = "https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}"
BOE_ITEM_API = "https://www.boe.es/datosabiertos/api/boe/id/{id}"
BOE_BASE     = "https://www.boe.es"
MAX_ALERTAS  = 400


def get_email_cfg():
    return {
        "smtp_server":   os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com"),
        "smtp_port":     int(os.environ.get("EMAIL_SMTP_PORT", "587")),
        "usuario":       os.environ.get("EMAIL_USUARIO", ""),
        "password":      os.environ.get("EMAIL_PASSWORD", ""),
        "destinatarios": [d.strip() for d in os.environ.get("EMAIL_DESTINATARIOS", "").split(",") if d.strip()],
    }


def cargar_keywords() -> dict:
    # user_config.json tiene prioridad si existe
    src = USER_CFG if USER_CFG.exists() else KW_FILE
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    mapa = {}
    for tematica, val in data.get("tematicas", {}).items():
        if isinstance(val, dict):
            if val.get("activa", True) is False:
                continue
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
            if k.get("activa", True):
                mapa[k["texto"].lower()] = "Extras"
        elif isinstance(k, str) and not k.startswith("_"):
            mapa[k.lower()] = "Extras"
    log.info(f"Keywords: {len(mapa)} términos en {len(set(mapa.values()))} temáticas")
    return mapa


def cargar_historial(fecha: str) -> set:
    HIST_DIR.mkdir(exist_ok=True)
    p = HIST_DIR / f"{fecha}.json"
    return set(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else set()


def guardar_historial(fecha: str, ids: set):
    HIST_DIR.mkdir(exist_ok=True)
    (HIST_DIR / f"{fecha}.json").write_text(json.dumps(list(ids), ensure_ascii=False), encoding="utf-8")


def obtener_sumario(fecha: str) -> list:
    try:
        r = requests.get(BOE_API.format(fecha=fecha), timeout=30,
                         headers={"Accept": "application/xml", "User-Agent": "BOEMonitor/2.0"})
        if r.status_code == 404: return []
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.warning(f"Error sumario {fecha}: {e}"); return []

    ctx = {}
    for sec in root.iter("seccion"):
        ns = sec.get("nombre", sec.get("id",""))
        for dep in sec.iter("departamento"):
            nd = dep.findtext("nombre","")
            for it in dep.iter("item"):
                bid = it.findtext("id","").strip()
                if bid: ctx[bid] = {"seccion": ns, "departamento": nd}

    items = []
    for item in root.iter("item"):
        bid = item.findtext("id","").strip()
        tit = item.findtext("titulo","").strip()
        uh  = item.findtext("urlHtml","").strip()
        up  = item.findtext("urlPdf","").strip()
        if not bid or not tit: continue
        c = ctx.get(bid, {})
        items.append({"id": bid, "titulo": tit,
                      "url_html": BOE_BASE+uh if uh.startswith("/") else uh,
                      "url_pdf":  BOE_BASE+up if up.startswith("/") else up,
                      "departamento": c.get("departamento",""),
                      "seccion": c.get("seccion",""), "extracto": ""})
    log.info(f"Sumario {fecha}: {len(items)} ítems")
    return items


def obtener_extracto(bid: str) -> str:
    try:
        r = requests.get(BOE_ITEM_API.format(id=bid), timeout=15,
                         headers={"Accept":"application/xml","User-Agent":"BOEMonitor/2.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        parrafos = [el.text.strip() for el in root.iter() if el.text and len(el.text.strip()) > 60]
        if parrafos:
            txt = " ".join(parrafos[:3])
            return txt[:500] + ("…" if len(txt) > 500 else "")
    except Exception: pass
    return ""


def filtrar(items: list, kw_map: dict) -> dict:
    res = {}
    for item in items:
        txt = f"{item['titulo']} {item['departamento']} {item['seccion']}".lower()
        for kw, t in kw_map.items():
            if kw in txt:
                res.setdefault(t, []).append(item)
                break
    return res


def generar_email_html(fecha_str, por_tematica, es_nuevo):
    fecha_fmt = datetime.strptime(fecha_str, "%Y%m%d").strftime("%d/%m/%Y")
    total  = sum(len(v) for v in por_tematica.values())
    nuevos = sum(1 for v in es_nuevo.values() if v)
    filas  = ""
    for t, items in sorted(por_tematica.items()):
        filas += f'<tr><td colspan="4" style="background:#1a1a2e;color:#e2b96f;padding:10px 14px;font-weight:700;font-size:13px">📌 {t}</td></tr>'
        for item in items:
            nuevo = es_nuevo.get(item["id"], True)
            bg    = "#fffbf2" if nuevo else "#fff"
            badge = ('<span style="background:#c9973a;color:#1a1a2e;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700">NUEVO</span>'
                     if nuevo else '<span style="color:#bbb;font-size:10px">conocido</span>')
            ext   = f'<div style="color:#888;font-size:11px;font-style:italic;margin-top:3px">{item["extracto"]}</div>' if item.get("extracto") else ""
            pdf   = f' &nbsp;<a href="{item["url_pdf"]}" style="color:#888;font-size:11px">PDF</a>' if item.get("url_pdf") else ""
            filas += f'''<tr style="background:{bg}">
              <td style="padding:10px 14px;border-bottom:1px solid #eee;max-width:400px">
                <a href="{item["url_html"]}" style="color:#1a1a2e;font-weight:600;text-decoration:none">{item["titulo"]}</a>{ext}
              </td>
              <td style="padding:10px 14px;border-bottom:1px solid #eee;color:#555;font-size:12px">{item["departamento"] or "—"}</td>
              <td style="padding:10px 14px;border-bottom:1px solid #eee;text-align:center">{badge}</td>
              <td style="padding:10px 14px;border-bottom:1px solid #eee;white-space:nowrap">
                <a href="{item["url_html"]}" style="color:#1a1a2e;font-size:12px">HTML</a>{pdf}
              </td>
            </tr>'''
    tabla = f'''<table style="width:100%;border-collapse:collapse;font-size:13px">
      <tr style="background:#f8f8f8">
        <th style="padding:10px 14px;text-align:left;color:#666;border-bottom:2px solid #eee">Título</th>
        <th style="padding:10px 14px;text-align:left;color:#666;border-bottom:2px solid #eee">Organismo</th>
        <th style="padding:10px 14px;text-align:center;color:#666;border-bottom:2px solid #eee">Estado</th>
        <th style="padding:10px 14px;color:#666;border-bottom:2px solid #eee">Ver</th>
      </tr>{filas}</table>''' if por_tematica else '<p style="padding:20px;color:#aaa;font-style:italic">Sin publicaciones relevantes hoy.</p>'
    return f'''<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:20px;background:#f0f0f0;font-family:Georgia,serif">
<div style="max-width:860px;margin:0 auto;background:#fff;border-radius:4px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.12)">
  <div style="background:#1a1a2e;padding:28px 30px">
    <div style="color:#e2b96f;font-size:11px;letter-spacing:3px;text-transform:uppercase;margin-bottom:8px">Monitor Normativo</div>
    <h1 style="margin:0;color:#fff;font-size:24px;font-weight:400">BOE · {fecha_fmt}</h1>
    <p style="margin:8px 0 0;color:#aaa;font-size:13px">{total} publicación(es) · <strong style="color:#e2b96f">{nuevos} nueva(s)</strong></p>
  </div>
  {tabla}
  <div style="padding:16px 30px;background:#f8f8f8;font-size:11px;color:#aaa;text-align:center">
    BOE Monitor · {datetime.now().strftime("%d/%m/%Y %H:%M")} · <a href="https://www.boe.es" style="color:#aaa">boe.es</a>
  </div>
</div></body></html>'''


def enviar_email(asunto, html, cfg):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[BOE Monitor] {asunto}"
    msg["From"]    = cfg["usuario"]
    msg["To"]      = ", ".join(cfg["destinatarios"])
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"]) as s:
        s.ehlo(); s.starttls()
        s.login(cfg["usuario"], cfg["password"])
        s.sendmail(cfg["usuario"], cfg["destinatarios"], msg.as_string())
    log.info(f"Email enviado a: {', '.join(cfg['destinatarios'])}")


def cargar_data() -> dict:
    if DATA_FILE.exists():
        try: return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except: pass
    return {"alertas": [], "stats": {}}


def actualizar_data(fecha_str, por_tematica, es_nuevo):
    data = cargar_data()
    items_planos = []
    for t, items in por_tematica.items():
        for item in items:
            items_planos.append({
                "id": item["id"], "titulo": item["titulo"],
                "departamento": item["departamento"], "tematica": t,
                "url_html": item["url_html"], "url_pdf": item.get("url_pdf",""),
                "extracto": item.get("extracto",""), "nuevo": es_nuevo.get(item["id"], True),
            })
    fecha_fmt = f"{fecha_str[:4]}-{fecha_str[4:6]}-{fecha_str[6:]}"
    entrada = {"fecha": fecha_fmt, "total": len(items_planos),
               "nuevos": sum(1 for i in items_planos if i["nuevo"]),
               "tematicas": list(por_tematica.keys()), "items": items_planos,
               "ejecutado": datetime.now().isoformat()}
    data["alertas"] = [a for a in data["alertas"] if a["fecha"] != fecha_fmt]
    data["alertas"].insert(0, entrada)
    data["alertas"] = sorted(data["alertas"], key=lambda x: x["fecha"], reverse=True)[:MAX_ALERTAS]
    stats = {}
    for a in data["alertas"]:
        for i in a.get("items", []):
            stats[i["tematica"]] = stats.get(i["tematica"], 0) + 1
    data["stats"] = stats
    data["ultima_update"] = datetime.now().isoformat()
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"data.json actualizado ({len(data['alertas'])} entradas)")


def main():
    hoy       = datetime.now()
    fecha_str = hoy.strftime("%Y%m%d")
    kw_map    = cargar_keywords()
    historial = cargar_historial(fecha_str)
    cfg       = get_email_cfg()

    items = obtener_sumario(fecha_str)
    if not items:
        log.info("Sin datos del BOE hoy.")
        actualizar_data(fecha_str, {}, {})
        return

    por_tematica = filtrar(items, kw_map)
    if not por_tematica:
        log.info("Sin coincidencias hoy.")
        guardar_historial(fecha_str, {i["id"] for i in items})
        actualizar_data(fecha_str, {}, {})
        return

    es_nuevo = {i["id"]: i["id"] not in historial
                for t_items in por_tematica.values() for i in t_items}

    if any(es_nuevo.values()) and cfg["usuario"] and cfg["password"]:
        for t_items in por_tematica.values():
            for item in t_items:
                if es_nuevo.get(item["id"]):
                    item["extracto"] = obtener_extracto(item["id"])
        nuevos_n = sum(1 for v in es_nuevo.values() if v)
        html = generar_email_html(fecha_str, por_tematica, es_nuevo)
        try:
            enviar_email(f"BOE {hoy.strftime('%d/%m/%Y')} — {nuevos_n} novedad(es)", html, cfg)
        except Exception as e:
            log.error(f"Error email: {e}")

    todos_ids = historial | {i["id"] for t in por_tematica.values() for i in t}
    guardar_historial(fecha_str, todos_ids)
    actualizar_data(fecha_str, por_tematica, es_nuevo)


if __name__ == "__main__":
    main()
