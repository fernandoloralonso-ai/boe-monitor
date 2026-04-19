#!/usr/bin/env python3
"""
BOE Monitor v5 — Doble vigilancia:
1. Sumario diario filtrado por keywords (disposiciones nuevas)
2. Legislación consolidada: detecta normas actualizadas del código de Tráfico
   y genera resumen de cambios con Claude AI
"""

import os, json, smtplib, logging, xml.etree.ElementTree as ET
from datetime import datetime, timedelta
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
NORMAS_FILE  = BASE / "normas_estado.json"  # guarda hash/fecha de cada norma vigilada

BOE_API      = "https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}"
BOE_CONS_API = "https://www.boe.es/datosabiertos/api/legislacion-consolidada"
BOE_ITEM_API = "https://www.boe.es/datosabiertos/api/boe/id/{id}"
BOE_BASE     = "https://www.boe.es"
CLAUDE_API   = "https://api.anthropic.com/v1/messages"
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MAX_ALERTAS  = 400
HEADERS_XML  = {"Accept": "application/xml", "User-Agent": "BOEMonitor/5.0"}
HEADERS_JSON = {"Accept": "application/json", "User-Agent": "BOEMonitor/5.0"}


# ── Normas del Código de Tráfico y Seguridad Vial a vigilar ──────────────────
# ID oficial BOE de cada norma clave
NORMAS_TRAFICO = [
    {"id": "BOE-A-2015-11722", "nombre": "RDL 6/2015 - Ley de Tráfico y Seguridad Vial"},
    {"id": "BOE-A-2003-23514", "nombre": "RD 1428/2003 - Reglamento General de Circulación"},
    {"id": "BOE-A-1998-28156", "nombre": "RD 2822/1998 - Reglamento General de Vehículos"},
    {"id": "BOE-A-2009-5914",  "nombre": "RD 818/2009 - Reglamento General de Conductores"},
    {"id": "BOE-A-2003-21341", "nombre": "RD 1295/2003 - Autoescuelas"},
    {"id": "BOE-A-2021-3821",  "nombre": "RD 174/2021 - Profesor de Formación Vial"},
    {"id": "BOE-A-2010-3239",  "nombre": "RD 170/2010 - Centros de Reconocimiento"},
    {"id": "BOE-A-2007-15781", "nombre": "RD 1032/2007 - CAP"},
    {"id": "BOE-A-2021-3186",  "nombre": "RD 284/2021 - CAP (modificación)"},
    {"id": "BOE-A-2014-1725",  "nombre": "RD 97/2014 - ADR Mercancías Peligrosas"},
    {"id": "BOE-A-1987-15801", "nombre": "Ley 16/1987 - Ordenación Transportes Terrestres"},
]


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
    return mapa


# ── Sumario diario ────────────────────────────────────────────────────────────
def obtener_sumario(fecha):
    try:
        r = requests.get(BOE_API.format(fecha=fecha), timeout=30, headers=HEADERS_XML)
        if r.status_code == 404: return []
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.warning(f"Error sumario {fecha}: {e}"); return []

    items = []
    for seccion in root.iter("seccion"):
        nom_sec = seccion.get("nombre", seccion.get("codigo", ""))
        for dep_el in seccion.iter("departamento"):
            nom_dep = dep_el.get("nombre", dep_el.get("codigo", ""))
            for item in dep_el.iter("item"):
                bid  = item.findtext("identificador", "").strip()
                ctrl = item.find("control")
                tit  = ctrl.findtext("titulo", "").strip() if ctrl is not None else item.findtext("titulo","").strip()
                uh   = item.findtext("url_html", "").strip()
                up   = item.findtext("url_pdf",  "").strip()
                if not bid or not tit: continue
                items.append({
                    "id": bid, "titulo": tit,
                    "url_html": BOE_BASE + uh if uh.startswith("/") else uh,
                    "url_pdf":  BOE_BASE + up if up.startswith("/") else up,
                    "departamento": nom_dep, "seccion": nom_sec, "extracto": "",
                })
    log.info(f"Sumario {fecha}: {len(items)} ítems")
    return items


def filtrar(items, kw_map):
    res = {}
    for item in items:
        texto = f"{item['titulo']} {item['seccion']} {item['departamento']}".lower()
        for kw, t in kw_map.items():
            if kw in texto:
                res.setdefault(t, []).append(item)
                break
    return res


# ── Legislación consolidada: vigilar normas clave ────────────────────────────
def cargar_normas_estado():
    if NORMAS_FILE.exists():
        return json.loads(NORMAS_FILE.read_text(encoding="utf-8"))
    return {}


def guardar_normas_estado(estado):
    NORMAS_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


def obtener_metadatos_norma(id_norma):
    url = f"https://www.boe.es/datosabiertos/api/legislacion-consolidada/id/{id_norma}/metadatos"
    try:
        r = requests.get(url, timeout=20, headers=HEADERS_JSON)
        if r.status_code == 404: return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Error metadatos {id_norma}: {e}")
        return None


def obtener_texto_norma(id_norma):
    """Obtiene fragmento del texto consolidado para comparar cambios"""
    url = f"https://www.boe.es/datosabiertos/api/legislacion-consolidada/id/{id_norma}"
    try:
        r = requests.get(url, timeout=30, headers=HEADERS_XML)
        if r.status_code != 200: return ""
        root = ET.fromstring(r.content)
        parrafos = [el.text.strip() for el in root.iter()
                    if el.text and len(el.text.strip()) > 30]
        return " ".join(parrafos[:50])[:3000]
    except Exception:
        return ""


def generar_resumen_cambios(nombre_norma, texto_actual, texto_anterior):
    """Usa Claude para generar resumen de cambios entre versiones"""
    if not ANTHROPIC_KEY:
        return "⚠️ Configura ANTHROPIC_API_KEY en los Secrets para obtener resúmenes automáticos de cambios."

    prompt = f"""Eres un experto en legislación de tráfico y transporte en España.
Se ha actualizado la siguiente norma: {nombre_norma}

TEXTO ANTERIOR (fragmento):
{texto_anterior[:1500] if texto_anterior else "No disponible (primera vez que se registra esta norma)"}

TEXTO ACTUAL (fragmento):
{texto_actual[:1500]}

Por favor, proporciona:
1. Un resumen ejecutivo de los cambios más importantes (máximo 3 párrafos)
2. Lista de los artículos o apartados modificados si los puedes identificar
3. Impacto práctico para profesores de autoescuela, instructores CAP o formadores ADR

Si no hay diferencias claras o es la primera vez que se registra, indícalo brevemente."""

    try:
        r = requests.post(CLAUDE_API,
            headers={"Content-Type": "application/json",
                     "x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-20250514",
                  "max_tokens": 800,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        r.raise_for_status()
        data = r.json()
        return data["content"][0]["text"]
    except Exception as e:
        log.error(f"Error Claude API: {e}")
        return "No se pudo generar el resumen automático."


def comprobar_normas_actualizadas():
    """Comprueba si alguna norma vigilada ha sido actualizada. Devuelve lista de cambios."""
    estado = cargar_normas_estado()
    cambios = []

    for norma in NORMAS_TRAFICO:
        nid    = norma["id"]
        nombre = norma["nombre"]
        log.info(f"Comprobando: {nombre}")

        meta = obtener_metadatos_norma(nid)
        if not meta:
            log.warning(f"  Sin metadatos para {nid}")
            continue

        # Extraer fecha de última actualización
        fecha_actual = ""
        try:
            # Intentar distintas rutas según estructura JSON
            if isinstance(meta, dict):
                for key in ["fechaActualizacion", "fecha_actualizacion", "ultimaModificacion"]:
                    if key in meta:
                        fecha_actual = str(meta[key]); break
                # Buscar recursivamente
                if not fecha_actual:
                    texto_meta = json.dumps(meta)
                    import re
                    fechas = re.findall(r'20\d{2}-\d{2}-\d{2}', texto_meta)
                    if fechas:
                        fecha_actual = max(fechas)
        except Exception:
            pass

        estado_previo = estado.get(nid, {})
        fecha_previa  = estado_previo.get("fecha_actualizacion", "")

        if fecha_actual and fecha_actual != fecha_previa:
            log.info(f"  ✅ CAMBIO DETECTADO: {fecha_previa} → {fecha_actual}")
            texto_actual   = obtener_texto_norma(nid)
            texto_anterior = estado_previo.get("texto_fragmento", "")
            resumen        = generar_resumen_cambios(nombre, texto_actual, texto_anterior)

            cambios.append({
                "id":              nid,
                "nombre":          nombre,
                "fecha_anterior":  fecha_previa,
                "fecha_actual":    fecha_actual,
                "resumen":         resumen,
                "url":             f"https://www.boe.es/buscar/act.php?id={nid}",
            })

            # Actualizar estado
            estado[nid] = {
                "fecha_actualizacion": fecha_actual,
                "texto_fragmento":     texto_actual[:2000],
                "ultima_comprobacion": datetime.now().isoformat(),
            }
        else:
            log.info(f"  Sin cambios ({fecha_actual or 'fecha no disponible'})")
            if nid not in estado:
                texto_actual = obtener_texto_norma(nid)
                estado[nid] = {
                    "fecha_actualizacion": fecha_actual,
                    "texto_fragmento":     texto_actual[:2000],
                    "ultima_comprobacion": datetime.now().isoformat(),
                }

    guardar_normas_estado(estado)
    return cambios


# ── Email ─────────────────────────────────────────────────────────────────────
def generar_email_html(fecha_str, por_tematica, es_nuevo, cambios_normas):
    fecha_fmt = datetime.strptime(fecha_str, "%Y%m%d").strftime("%d/%m/%Y")
    total  = sum(len(v) for v in por_tematica.values())
    nuevos = sum(1 for v in es_nuevo.values() if v)

    # Sección de normas actualizadas
    html_normas = ""
    if cambios_normas:
        html_normas = '<div style="background:#fff8e1;border-left:4px solid #c9973a;padding:20px 30px;margin-bottom:0">'
        html_normas += f'<h2 style="margin:0 0 16px;color:#1a1a2e;font-size:18px">⚖️ Normas actualizadas ({len(cambios_normas)})</h2>'
        for c in cambios_normas:
            html_normas += f'''
            <div style="background:#fff;border:1px solid #e0d5c5;border-radius:4px;padding:16px;margin-bottom:12px">
              <div style="font-weight:700;color:#1a1a2e;margin-bottom:4px">
                <a href="{c["url"]}" style="color:#1a1a2e;text-decoration:none">{c["nombre"]}</a>
              </div>
              <div style="font-size:11px;color:#888;font-family:monospace;margin-bottom:10px">
                {f'Actualizada: {c["fecha_anterior"]} → <strong>{c["fecha_actual"]}</strong>' if c["fecha_anterior"] else f'Primera detección: {c["fecha_actual"]}'}
              </div>
              <div style="font-size:13px;color:#444;line-height:1.6;white-space:pre-wrap">{c["resumen"]}</div>
            </div>'''
        html_normas += '</div>'

    # Sección sumario diario
    filas = ""
    if por_tematica:
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
                  <td style="padding:10px 14px;border-bottom:1px solid #eee;max-width:380px">
                    <a href="{item["url_html"]}" style="color:#1a1a2e;font-weight:600;text-decoration:none">{item["titulo"]}</a>{ext}
                  </td>
                  <td style="padding:10px 14px;border-bottom:1px solid #eee;color:#555;font-size:12px">{item["departamento"] or "—"}</td>
                  <td style="padding:10px 14px;border-bottom:1px solid #eee;text-align:center">{badge}</td>
                  <td style="padding:10px 14px;border-bottom:1px solid #eee;white-space:nowrap">
                    <a href="{item["url_html"]}" style="color:#2471a3;font-size:12px">HTML</a>{pdf}
                  </td>
                </tr>'''

    tabla_sumario = ""
    if por_tematica:
        tabla_sumario = f'''
        <div style="padding:20px 30px">
          <h2 style="margin:0 0 14px;color:#1a1a2e;font-size:18px">📋 Nuevas publicaciones en el BOE ({total})</h2>
          <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#f8f8f8">
              <th style="padding:8px 14px;text-align:left;color:#666;border-bottom:2px solid #eee">Título</th>
              <th style="padding:8px 14px;text-align:left;color:#666;border-bottom:2px solid #eee">Organismo</th>
              <th style="padding:8px 14px;text-align:center;color:#666;border-bottom:2px solid #eee">Estado</th>
              <th style="padding:8px 14px;color:#666;border-bottom:2px solid #eee">Ver</th>
            </tr>{filas}
          </table>
        </div>'''

    sin_novedades = ""
    if not por_tematica and not cambios_normas:
        sin_novedades = '<p style="padding:20px 30px;color:#aaa;font-style:italic">No hay publicaciones relevantes ni actualizaciones de normas hoy.</p>'

    return (f'<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"></head>'
            f'<body style="margin:0;padding:20px;background:#f0f0f0;font-family:Georgia,serif">'
            f'<div style="max-width:900px;margin:0 auto;background:#fff;border-radius:4px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.12)">'
            f'<div style="background:#1a1a2e;padding:28px 30px">'
            f'<div style="color:#e2b96f;font-size:11px;letter-spacing:3px;text-transform:uppercase;margin-bottom:8px">Monitor Normativo · Autoescuela · CAP · ADR</div>'
            f'<h1 style="margin:0;color:#fff;font-size:24px;font-weight:400">BOE · {fecha_fmt}</h1>'
            f'<p style="margin:8px 0 0;color:#aaa;font-size:13px">'
            f'{len(cambios_normas)} norma(s) actualizada(s) · {total} publicación(es) nueva(s) · <strong style="color:#e2b96f">{nuevos} sin ver</strong>'
            f'</p></div>'
            f'{html_normas}{tabla_sumario}{sin_novedades}'
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


def cargar_historial(fecha):
    HIST_DIR.mkdir(exist_ok=True)
    p = HIST_DIR / f"{fecha}.json"
    return set(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else set()


def guardar_historial(fecha, ids):
    HIST_DIR.mkdir(exist_ok=True)
    (HIST_DIR / f"{fecha}.json").write_text(
        json.dumps(list(ids), ensure_ascii=False), encoding="utf-8")


def cargar_data():
    if DATA_FILE.exists():
        try: return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except: pass
    return {"alertas": [], "stats": {}}


def actualizar_data(fecha_str, por_tematica, es_nuevo, cambios_normas):
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
               "normas_actualizadas": cambios_normas,
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


def main():
    hoy       = datetime.now()
    fecha_str = hoy.strftime("%Y%m%d")
    cfg       = cargar_config()
    kw_map    = build_kw_map(cfg)
    historial = cargar_historial(fecha_str)
    email_cfg = get_email_cfg()

    log.info(f"=== BOE Monitor v5 · {hoy.strftime('%d/%m/%Y')} ===")

    # 1. Comprobar normas consolidadas actualizadas
    log.info("Comprobando normas del Código de Tráfico...")
    cambios_normas = comprobar_normas_actualizadas()
    log.info(f"Normas con cambios: {len(cambios_normas)}")

    # 2. Sumario diario
    items = obtener_sumario(fecha_str)
    por_tematica = filtrar(items, kw_map) if items else {}
    log.info(f"Publicaciones relevantes hoy: {sum(len(v) for v in por_tematica.values())}")

    es_nuevo = {i["id"]: i["id"] not in historial
                for t_items in por_tematica.values() for i in t_items}

    hay_novedades = cambios_normas or any(es_nuevo.values())

    if hay_novedades and email_cfg["usuario"] and email_cfg["password"]:
        partes = []
        if cambios_normas:
            partes.append(f"{len(cambios_normas)} norma(s) actualizada(s)")
        nuevos_n = sum(1 for v in es_nuevo.values() if v)
        if nuevos_n:
            partes.append(f"{nuevos_n} publicación(es) nueva(s)")
        asunto = " · ".join(partes) if partes else "Resumen diario"
        html   = generar_email_html(fecha_str, por_tematica, es_nuevo, cambios_normas)
        try:
            enviar_email(f"BOE {hoy.strftime('%d/%m/%Y')} — {asunto}", html, email_cfg)
        except Exception as e:
            log.error(f"Error email: {e}")
    else:
        log.info("Sin novedades hoy, email omitido.")

    todos_ids = historial | {i["id"] for t in por_tematica.values() for i in t}
    guardar_historial(fecha_str, todos_ids)
    actualizar_data(fecha_str, por_tematica, es_nuevo, cambios_normas)
    log.info("✅ Completado")


if __name__ == "__main__":
    main()
