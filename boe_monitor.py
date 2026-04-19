#!/usr/bin/env python3
"""
BOE Monitor v4 — Estructura XML corregida según API real del BOE
Tags reales: identificador, titulo (en control), url_pdf, url_html
"""

import os, json, smtplib, logging, xml.etree.ElementTree as ET
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE         = Path(__file__).parent
USER_CFG     = BASE / "user_config.json"
HIST_DIR     = BASE / "historial"
DATA_FILE    = BASE / "data.json"

BOE_API      = "https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}"
BOE_ITEM_API = "https://www.boe.es/datosabiertos/api/boe/id/{id}"
BOE_BASE     = "https://www.boe.es"
MAX_ALERTAS  = 400
HEADERS      = {"Accept": "application/xml", "User-Agent": "BOEMonitor/4.0"}


def get_email_cfg():
    return {
        "smtp_server":   os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com"),
        "smtp_port":     int(os.environ.get("EMAIL_SMTP_PORT", "587")),
        "usuario":       os.environ.get("EMAIL_USUARIO", ""),
        "password":      os.environ.get("EMAIL_PASSWORD", ""),
        "destinatarios": [d.strip() for d in os.environ.get("EMAIL_DESTINATARIOS","").split(",") if d.strip()],
    }


def cargar_config():
    with open(USER_CFG, encoding="utf-8") as f:
        return json.load(f)


def build_kw_map(cfg):
    mapa = {}
    for tematica, val in cfg.get("tematicas", {}).items():
        if isinstance(val, dict) and val.get("activa", True) is False:
            continue
        kws = val.get("keywords", []) if isinstance(val, dict) else val
        for k in kws:
            if isinstance(k, dict):
                if k.get("activa", True) and not k.get("texto","").startswith("_"):
                    mapa[k["texto"].lower()] = tematica
            elif isinstance(k, str) and not k.startswith("_"):
                mapa[k.lower()] = tematica
    for k in cfg.get("extras", []):
        txt = k.get("texto", k) if isinstance(k, dict) else k
        act = k.get("activa", True) if isinstance(k, dict) else True
        if act and not txt.startswith("_"):
            mapa[txt.lower()] = "Extras"
    return mapa


def build_dep_list(cfg):
    return [d.lower() for d in cfg.get("departamentos_vigilados", [])]


def es_relevante(item, kw_map, dep_list):
    texto = f"{item['titulo']} {item['seccion']}".lower()
    for kw, t in kw_map.items():
        if kw in texto:
            return t
    dep = item.get("departamento", "").lower()
    for d in dep_list:
        if d in dep:
            return "Departamento vigilado"
    return None


def obtener_sumario(fecha):
    try:
        r = requests.get(BOE_API.format(fecha=fecha), timeout=30, headers=HEADERS)
        if r.status_code == 404: return []
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.warning(f"Error sumario {fecha}: {e}"); return []

    items = []
    # Estructura real: response > data > sumario > diario > seccion > departamento > epigrafe > item
    # El tag item contiene: identificador, control (con titulo), url_pdf, url_html, url_xml
    for seccion in root.iter("seccion"):
        nom_sec = seccion.get("nombre", seccion.get("codigo", ""))
        for dep_el in seccion.iter("departamento"):
            nom_dep = dep_el.get("nombre", dep_el.get("codigo", ""))
            for item in dep_el.iter("item"):
                bid  = item.findtext("identificador", "").strip()
                # titulo está dentro de <control><titulo>
                ctrl = item.find("control")
                tit  = ctrl.findtext("titulo", "").strip() if ctrl is not None else item.findtext("titulo","").strip()
                uh   = item.findtext("url_html", "").strip()
                up   = item.findtext("url_pdf",  "").strip()
                if not bid or not tit:
                    continue
                items.append({
                    "id":           bid,
                    "titulo":       tit,
                    "url_html":     BOE_BASE + uh if uh.startswith("/") else uh,
                    "url_pdf":      BOE_BASE + up if up.startswith("/") else up,
                    "departamento": nom_dep,
                    "seccion":      nom_sec,
                    "extracto":     "",
                })

    log.info(f"Sumario {fecha}: {len(items)} ítems totales")
    return items


def obtener_extracto(bid):
    try:
        r = requests.get(BOE_ITEM_API.format(id=bid), timeout=15, headers=HEADERS)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        parrafos = [el.text.strip() for el in root.iter()
                    if el.text and len(el.text.strip()) > 60]
        if parrafos:
            txt = " ".join(parrafos[:3])
            return txt[:500] + ("…" if len(txt) > 500 else "")
    except Exception:
        pass
    return ""


def filtrar(items, kw_map, dep_list):
    res = {}
    for item in items:
        t = es_relevante(item, kw_map, dep_list)
        if t:
            res.setdefault(t, []).append(item)
    return res


def cargar_historial(fecha):
    HIST_DIR.mkdir(exist_ok=True)
    p = HIST_DIR / f"{fecha}.json"
    return set(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else set()


def guardar_historial(fecha, ids):
    HIST_DIR.mkdir(exist_ok=True)
    (HIST_DIR / f"{fecha}.json").write_text(
        json.dumps(list(ids), ensure_ascii=False), encoding="utf-8")


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
            ext  = f'<div style="color:#888;font-size:11px;font-style:italic;margin-top:3px">{item["extracto"]}</div>' if item.get("extracto") else ""
            pdf  = f' · <a href="{item["url_pdf"]}" style="color:#888;font-size:11px">PDF</a>' if item.get("url_pdf") else ""
            filas += f'''<tr style="background:{bg}">
              <td style="padding:10px 14px;border-bottom:1px solid #eee;max-width:400px">
                <a href="{item["url_html"]}" style="color:#1a1a2e;font-weight:600;text-decoration:none">{item["titulo"]}</a>{ext}
              </td>
              <td style="padding:10px 14px;border-bottom:1px solid #eee;color:#555;font-size:12px">{item["departamento"] or "—"}</td>
              <td style="padding:10px 14px;border-bottom:1px solid #eee;text-align:center">{badge}</td>
              <td style="padding:10px 14px;border-bottom:1px solid #eee;white-space:nowrap">
                <a href="{item["url_html"]}" style="color:#2471a3;font-size:12px">HTML</a>{pdf}
              </td>
            </tr>'''
    tabla = (f'<table style="width:100%;border-collapse:collapse;font-size:13px">'
             f'<tr style="background:#f8f8f8">'
             f'<th style="padding:10px 14px;text-align:left;color:#666;border-bottom:2px solid #eee">Título</th>'
             f'<th style="padding:10px 14px;text-align:left;color:#666;border-bottom:2px solid #eee">Organismo</th>'
             f'<th style="padding:10px 14px;text-align:center;color:#666;border-bottom:2px solid #eee">Estado</th>'
             f'<th style="padding:10px 14px;color:#666;border-bottom:2px solid #eee">Ver</th>'
             f'</tr>{filas}</table>') if por_tematica else '<p style="padding:20px;color:#aaa;font-style:italic">Sin publicaciones relevantes hoy.</p>'
    return (f'<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"></head>'
            f'<body style="margin:0;padding:20px;background:#f0f0f0;font-family:Georgia,serif">'
            f'<div style="max-width:860px;margin:0 auto;background:#fff;border-radius:4px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.12)">'
            f'<div style="background:#1a1a2e;padding:28px 30px">'
            f'<div style="color:#e2b96f;font-size:11px;letter-spacing:3px;text-transform:uppercase;margin-bottom:8px">Monitor Normativo</div>'
            f'<h1 style="margin:0;color:#fff;font-size:24px;font-weight:400">BOE · {fecha_fmt}</h1>'
            f'<p style="margin:8px 0 0;color:#aaa;font-size:13px">{total} publicación(es) · <strong style="color:#e2b96f">{nuevos} nueva(s)</strong></p>'
            f'</div>{tabla}'
            f'<div style="padding:16px 30px;background:#f8f8f8;font-size:11px;color:#aaa;text-align:center">'
            f'BOE Monitor · {datetime.now().strftime("%d/%m/%Y %H:%M")} · <a href="https://www.boe.es" style="color:#aaa">boe.es</a>'
            f'</div></div></body></html>')


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


def cargar_data():
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
    fd = f"{fecha_str[:4]}-{fecha_str[4:6]}-{fecha_str[6:]}"
    entrada = {"fecha": fd, "total": len(items_planos),
               "nuevos": sum(1 for i in items_planos if i["nuevo"]),
               "tematicas": list(por_tematica.keys()), "items": items_planos,
               "ejecutado": datetime.now().isoformat()}
    data["alertas"] = [a for a in data["alertas"] if a["fecha"] != fd]
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
    cfg       = cargar_config()
    kw_map    = build_kw_map(cfg)
    dep_list  = build_dep_list(cfg)
    historial = cargar_historial(fecha_str)
    email_cfg = get_email_cfg()

    log.info(f"Keywords: {len(kw_map)} | Departamentos: {len(dep_list)}")

    items = obtener_sumario(fecha_str)
    if not items:
        log.info("Sin datos del BOE hoy.")
        actualizar_data(fecha_str, {}, {})
        return

    por_tematica = filtrar(items, kw_map, dep_list)
    log.info(f"Relevantes: {sum(len(v) for v in por_tematica.values())} en {len(por_tematica)} temáticas")

    if not por_tematica:
        log.info("Sin coincidencias hoy.")
        guardar_historial(fecha_str, {i["id"] for i in items})
        actualizar_data(fecha_str, {}, {})
        return

    es_nuevo = {i["id"]: i["id"] not in historial
                for t_items in por_tematica.values() for i in t_items}

    if any(es_nuevo.values()) and email_cfg["usuario"] and email_cfg["password"]:
        for t_items in por_tematica.values():
            for item in t_items:
                if es_nuevo.get(item["id"]):
                    item["extracto"] = obtener_extracto(item["id"])
        nuevos_n = sum(1 for v in es_nuevo.values() if v)
        html = generar_email_html(fecha_str, por_tematica, es_nuevo)
        try:
            enviar_email(f"BOE {hoy.strftime('%d/%m/%Y')} — {nuevos_n} novedad(es)", html, email_cfg)
        except Exception as e:
            log.error(f"Error email: {e}")

    todos_ids = historial | {i["id"] for t in por_tematica.values() for i in t}
    guardar_historial(fecha_str, todos_ids)
    actualizar_data(fecha_str, por_tematica, es_nuevo)


if __name__ == "__main__":
    main()
