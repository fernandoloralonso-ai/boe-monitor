#!/usr/bin/env python3
"""
BOE Monitor v8 — Vigilancia exclusiva por Códigos Electrónicos
==============================================================
Vigila únicamente las normas de:
- Código 020: Tráfico y Seguridad Vial
- Código 327: Transporte de Mercancías por Carretera (CAP + ADR)

Cada lunes consulta la fecha_actualizacion de cada norma via API consolidada.
Si cambia → genera resumen con Claude y envía email.
Sin sumario diario. Sin keywords. Sin ruido.
"""

import os, json, smtplib, logging, xml.etree.ElementTree as ET, hashlib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE         = Path(__file__).parent
DATA_FILE    = BASE / "data.json"
NORMAS_FILE  = BASE / "normas_estado.json"

BOE_META_API = "https://www.boe.es/datosabiertos/api/legislacion-consolidada/id/{id}/metadatos"
BOE_TEXT_API = "https://www.boe.es/datosabiertos/api/legislacion-consolidada/id/{id}"
CLAUDE_API   = "https://api.anthropic.com/v1/messages"

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MAX_ALERTAS   = 400
HEADERS_XML   = {"Accept": "application/xml",  "User-Agent": "BOEMonitor/8.0"}
HEADERS_JSON  = {"Accept": "application/json", "User-Agent": "BOEMonitor/8.0"}

# ── NORMAS DE LOS CÓDIGOS ELECTRÓNICOS ───────────────────────────────────────

CODIGOS_NORMAS = [
    # ── CÓDIGO 020: TRÁFICO Y SEGURIDAD VIAL ──
    {"id": "BOE-A-2015-11722", "nombre": "RDL 6/2015 — Ley sobre Tráfico, Circulación y Seguridad Vial",          "codigo": "020"},
    {"id": "BOE-A-2003-23514", "nombre": "RD 1428/2003 — Reglamento General de Circulación",                      "codigo": "020"},
    {"id": "BOE-A-1999-1826",  "nombre": "RD 2822/1998 — Reglamento General de Vehículos",                        "codigo": "020"},
    {"id": "BOE-A-2009-9481",  "nombre": "RD 818/2009 — Reglamento General de Conductores",                       "codigo": "020"},
    {"id": "BOE-A-2003-19801", "nombre": "RD 1295/2003 — Reglamento de Autoescuelas",                             "codigo": "020"},
    {"id": "BOE-A-2010-5038",  "nombre": "RD 369/2010 — Modifica RD 1295/2003 Autoescuelas",                      "codigo": "020"},
    {"id": "BOE-A-2023-24843", "nombre": "RD 1010/2023 — Modifica RD 1295/2003 Autoescuelas",                     "codigo": "020"},
    {"id": "BOE-A-2021-3821",  "nombre": "RD 174/2021 — Profesor de Formación Vial",                              "codigo": "020"},
    {"id": "BOE-A-2010-3471",  "nombre": "RD 170/2010 — Centros de Reconocimiento de Conductores",                "codigo": "020"},
    {"id": "BOE-A-2011-13099", "nombre": "Orden INT/2323/2011 — Formación acceso progresivo permiso A",           "codigo": "020"},

    # ── CÓDIGO 327: TRANSPORTE / CAP / ADR ──
    {"id": "BOE-A-1987-15801", "nombre": "Ley 16/1987 — Ordenación de los Transportes Terrestres (LOTT)",        "codigo": "327"},
    {"id": "BOE-A-1990-28440", "nombre": "RD 1211/1990 — Reglamento LOTT",                                        "codigo": "327"},
    {"id": "BOE-A-2007-15270", "nombre": "RD 1032/2007 — CAP: Cualificación conductores profesionales",           "codigo": "327"},
    {"id": "BOE-A-2021-3186",  "nombre": "RD 284/2021 — Modifica RD 1032/2007 CAP",                              "codigo": "327"},
    {"id": "BOE-A-2010-18444", "nombre": "Orden FOM/2607/2010 — Centros de formación CAP",                        "codigo": "327"},
    {"id": "BOE-A-2014-1725",  "nombre": "RD 97/2014 — Transporte de Mercancías Peligrosas por Carretera (ADR)", "codigo": "327"},
    {"id": "BOE-A-2019-9661",  "nombre": "ADR 2019 — Acuerdo Europeo Mercancías Peligrosas por Carretera",       "codigo": "327"},
    {"id": "BOE-A-2019-2494",  "nombre": "RD 70/2019 — Modifica Reglamento LOTT y formación conductores",        "codigo": "327"},
    {"id": "BOE-A-2006-9974",  "nombre": "RD 640/2006 — Tiempos de conducción y descanso",                       "codigo": "327"},
]

GRUPO_CODIGO = {
    "020": "Código 020 · Tráfico y Seguridad Vial",
    "327": "Código 327 · Transporte / CAP / ADR",
}


# ── Email ─────────────────────────────────────────────────────────────────────
def get_email_cfg():
    return {
        "smtp_server":   os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com"),
        "smtp_port":     int(os.environ.get("EMAIL_SMTP_PORT", "587")),
        "usuario":       os.environ.get("EMAIL_USUARIO", ""),
        "password":      os.environ.get("EMAIL_PASSWORD", ""),
        "destinatarios": [d.strip() for d in os.environ.get("EMAIL_DESTINATARIOS","").split(",") if d.strip()],
    }


# ── API BOE Consolidada ───────────────────────────────────────────────────────
def obtener_metadatos(nid):
    try:
        r = requests.get(BOE_META_API.format(id=nid), timeout=20, headers=HEADERS_XML)
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.content)
        meta = root.find(".//metadatos")
        if meta is None:
            return None
        return {
            "fecha_actualizacion": meta.findtext("fecha_actualizacion", ""),
            "titulo":              meta.findtext("titulo", ""),
            "derogada":            meta.findtext("estatus_derogacion", "N"),
        }
    except Exception as e:
        log.warning(f"  Error metadatos {nid}: {e}")
        return None


def obtener_fragmento_texto(nid):
    try:
        r = requests.get(BOE_TEXT_API.format(id=nid), timeout=25, headers=HEADERS_XML)
        if r.status_code != 200:
            return ""
        root = ET.fromstring(r.content)
        parrafos = [el.text.strip() for el in root.iter("p")
                    if el.text and len(el.text.strip()) > 40]
        return " ".join(parrafos[:40])[:2500]
    except Exception:
        return ""


def hash_texto(texto):
    return hashlib.md5(texto.encode("utf-8", errors="ignore")).hexdigest()


# ── Claude ────────────────────────────────────────────────────────────────────
def generar_resumen_claude(tipo, nombre, codigo, texto_anterior, texto_actual):
    if not ANTHROPIC_KEY:
        return "ℹ️ Añade ANTHROPIC_API_KEY en los Secrets de GitHub para obtener resúmenes automáticos."

    grupo = GRUPO_CODIGO.get(codigo, "")

    if tipo == "cambio":
        prompt = f"""Eres un experto en legislación española de tráfico, transporte y autoescuelas.

Se ha actualizado la norma: {nombre}
Área: {grupo}

TEXTO ANTERIOR (fragmento):
{texto_anterior[:1500] if texto_anterior else "Primera vez que se registra esta norma."}

TEXTO ACTUAL (fragmento):
{texto_actual[:1500]}

Proporciona en español:
1. Resumen ejecutivo de los cambios (2-3 párrafos)
2. Artículos o apartados afectados si los identificas
3. Impacto práctico para profesores de autoescuela, instructores CAP o formadores ADR"""

    else:
        prompt = f"""La norma {nombre} ha sido derogada o ha desaparecido del sistema consolidado del BOE.
¿Por qué norma suele sustituirse? ¿Qué impacto tiene para profesores de autoescuela, instructores CAP o formadores ADR?
Responde en 2-3 párrafos en español."""

    try:
        r = requests.post(CLAUDE_API,
            headers={"Content-Type": "application/json",
                     "x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 700,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40)
        r.raise_for_status()
        return r.json()["content"][0]["text"]
    except Exception as e:
        log.error(f"Error Claude: {e}")
        return "No se pudo generar el resumen automático."


# ── Estado de normas ──────────────────────────────────────────────────────────
def cargar_estado():
    if NORMAS_FILE.exists():
        try: return json.loads(NORMAS_FILE.read_text(encoding="utf-8"))
        except: pass
    return {}


def guardar_estado(estado):
    NORMAS_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Comprobación principal ────────────────────────────────────────────────────
def comprobar_codigos():
    estado  = cargar_estado()
    cambios = []

    log.info(f"Comprobando {len(CODIGOS_NORMAS)} normas de los códigos 020 y 327...")

    for norma in CODIGOS_NORMAS:
        nid    = norma["id"]
        nombre = norma["nombre"]
        codigo = norma["codigo"]
        prev   = estado.get(nid, {})

        log.info(f"  [{codigo}] {nombre[:65]}")
        meta = obtener_metadatos(nid)

        if meta is None:
            log.warning(f"    ⚠ Sin metadatos — ID puede ser incorrecto")
            continue

        fecha_actual = meta.get("fecha_actualizacion", "")
        fecha_previa = prev.get("fecha_actualizacion", "")
        derogada     = meta.get("derogada", "N") == "S"

        if derogada and not prev.get("derogada"):
            log.info(f"    ⚠️ DEROGADA")
            resumen = generar_resumen_claude("derogada", nombre, codigo, "", "")
            cambios.append({
                "tipo": "derogada", "id": nid, "nombre": nombre,
                "codigo": codigo, "grupo": GRUPO_CODIGO[codigo],
                "resumen": resumen,
                "url": f"https://www.boe.es/buscar/act.php?id={nid}",
            })
            estado[nid] = {**prev, "derogada": True,
                           "ultima_comprobacion": datetime.now().isoformat()}

        elif fecha_actual and fecha_actual != fecha_previa:
            log.info(f"    🔄 CAMBIO: {fecha_previa or 'primera vez'} → {fecha_actual}")
            texto_actual   = obtener_fragmento_texto(nid)
            texto_anterior = prev.get("texto_frag", "")
            h_nuevo        = hash_texto(texto_actual)

            if h_nuevo != prev.get("texto_hash", "") or not fecha_previa:
                resumen = generar_resumen_claude("cambio", nombre, codigo,
                                                  texto_anterior, texto_actual)
                cambios.append({
                    "tipo": "cambio", "id": nid, "nombre": nombre,
                    "codigo": codigo, "grupo": GRUPO_CODIGO[codigo],
                    "fecha_anterior": fecha_previa,
                    "fecha_actual":   fecha_actual,
                    "resumen": resumen,
                    "url": f"https://www.boe.es/buscar/act.php?id={nid}",
                })
                estado[nid] = {
                    "fecha_actualizacion": fecha_actual,
                    "texto_hash": h_nuevo,
                    "texto_frag": texto_actual[:1500],
                    "derogada":   derogada,
                    "ultima_comprobacion": datetime.now().isoformat(),
                }
            else:
                estado[nid] = {**prev, "fecha_actualizacion": fecha_actual,
                               "ultima_comprobacion": datetime.now().isoformat()}
        else:
            log.info(f"    ✓ Sin cambios ({fecha_actual[:10] if fecha_actual else 'sin fecha'})")
            if not fecha_previa and fecha_actual:
                # Primera vez — guardar estado inicial sin generar evento
                texto = obtener_fragmento_texto(nid)
                estado[nid] = {
                    "fecha_actualizacion": fecha_actual,
                    "texto_hash": hash_texto(texto),
                    "texto_frag": texto[:1500],
                    "derogada":   derogada,
                    "ultima_comprobacion": datetime.now().isoformat(),
                }
            else:
                estado[nid] = {**prev,
                               "ultima_comprobacion": datetime.now().isoformat()}

    guardar_estado(estado)
    log.info(f"Comprobación completada — {len(cambios)} cambio(s) detectado(s)")
    return cambios


# ── Email HTML ────────────────────────────────────────────────────────────────
def generar_email_html(fecha_str, cambios):
    fecha_fmt = datetime.strptime(fecha_str, "%Y%m%d").strftime("%d/%m/%Y")

    bloques = ""
    for c in cambios:
        tipo        = c.get("tipo", "cambio")
        badge_txt   = "ACTUALIZACIÓN" if tipo == "cambio" else "⚠️ DEROGADA"
        badge_color = "#c9973a"        if tipo == "cambio" else "#c0392b"
        bg          = "#fffbf2"        if tipo == "cambio" else "#fce4ec"
        fecha_extra = ""
        if tipo == "cambio":
            if c.get("fecha_anterior"):
                fecha_extra = (f'<span style="font-size:10px;color:#888;font-family:monospace;margin-left:8px">'
                               f'{c["fecha_anterior"][:10]} → {c["fecha_actual"][:10]}</span>')
            else:
                fecha_extra = (f'<span style="font-size:10px;color:#27ae60;font-family:monospace;margin-left:8px">'
                               f'Registrada por primera vez: {c["fecha_actual"][:10]}</span>')

        bloques += f'''
        <div style="background:{bg};border:1px solid #ddd;border-radius:4px;padding:18px;margin-bottom:16px">
          <div style="margin-bottom:10px;display:flex;align-items:center;flex-wrap:wrap;gap:8px">
            <span style="background:{badge_color};color:#fff;padding:3px 11px;border-radius:10px;
                         font-size:10px;font-weight:700;font-family:monospace">{badge_txt}</span>
            <span style="font-size:11px;color:#888">{c.get("grupo","")}</span>
            {fecha_extra}
          </div>
          <div style="font-weight:700;color:#1a1a2e;font-size:15px;margin-bottom:12px">
            <a href="{c.get("url","")}" style="color:#1a1a2e;text-decoration:none">{c.get("nombre","")}</a>
          </div>
          <div style="font-size:13px;color:#333;line-height:1.75;background:rgba(255,255,255,.75);
                      padding:14px;border-radius:3px;white-space:pre-wrap">{c.get("resumen","")}</div>
        </div>'''

    return (f'<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            f'<body style="margin:0;padding:16px;background:#f0f0f0;font-family:Georgia,serif">'
            f'<div style="max-width:860px;margin:0 auto;background:#fff;border-radius:4px;'
            f'overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.12)">'
            f'<div style="background:#1a1a2e;padding:26px 30px">'
            f'<div style="color:#e2b96f;font-size:10px;letter-spacing:3px;text-transform:uppercase;margin-bottom:6px">'
            f'Monitor Normativo · Códigos Electrónicos BOE 020 y 327</div>'
            f'<h1 style="margin:0;color:#fff;font-size:22px;font-weight:400">Novedades legislativas · {fecha_fmt}</h1>'
            f'<p style="margin:6px 0 0;color:#888;font-size:12px">'
            f'{len(cambios)} norma(s) con cambios en los códigos vigilados</p>'
            f'</div>'
            f'<div style="padding:24px 30px">{bloques}</div>'
            f'<div style="padding:14px 30px;background:#f8f8f8;font-size:10px;color:#aaa;text-align:center">'
            f'BOE Monitor v8 · <a href="https://www.boe.es/biblioteca_juridica/codigos/codigo.php?id=020_Codigo_de_Trafico_y_Seguridad_Vial" style="color:#aaa">Código 020</a>'
            f' · <a href="https://www.boe.es/biblioteca_juridica/codigos/codigo.php?id=327_Codigo_del_Transporte_de_Mercancias_por_Carretera" style="color:#aaa">Código 327</a>'
            f' · {datetime.now().strftime("%d/%m/%Y %H:%M")}'
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


# ── data.json para el dashboard ───────────────────────────────────────────────
def actualizar_data(fecha_str, cambios):
    data = {"alertas": [], "stats": {}}
    if DATA_FILE.exists():
        try: data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except: pass

    fd = f"{fecha_str[:4]}-{fecha_str[4:6]}-{fecha_str[6:]}"
    entrada = {
        "fecha":    fd,
        "total":    len(cambios),
        "nuevos":   len(cambios),
        "tematicas": list({c["grupo"] for c in cambios}),
        "items": [{
            "id":           c["id"],
            "titulo":       c["nombre"],
            "departamento": c["grupo"],
            "tematica":     c["grupo"],
            "url_html":     c["url"],
            "url_pdf":      "",
            "extracto":     c["resumen"][:300] + "…" if len(c.get("resumen","")) > 300 else c.get("resumen",""),
            "nuevo":        True,
        } for c in cambios],
        "ejecutado": datetime.now().isoformat(),
    }

    data["alertas"] = [a for a in data["alertas"] if a["fecha"] != fd]
    if cambios:
        data["alertas"].insert(0, entrada)
    data["alertas"] = sorted(data["alertas"], key=lambda x: x["fecha"], reverse=True)[:MAX_ALERTAS]

    stats = {}
    for a in data["alertas"]:
        for i in a.get("items", []):
            t = i["tematica"]
            stats[t] = stats.get(t, 0) + 1
    data["stats"] = stats
    data["ultima_update"] = datetime.now().isoformat()
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"data.json actualizado ({len(data['alertas'])} entradas)")


# ── Main ──────────────────────────────────────────────────────────────────────
DIA_COMPROBACION = 0  # lunes

def main():
    hoy       = datetime.now()
    fecha_str = hoy.strftime("%Y%m%d")
    email_cfg = get_email_cfg()
    forzar    = os.environ.get("FORZAR_BUSQUEDA_NORMAS","").lower() == "true"

    log.info(f"=== BOE Monitor v8 · Códigos 020 y 327 · {hoy.strftime('%d/%m/%Y')} ===")

    es_dia_comprobacion = hoy.weekday() == DIA_COMPROBACION or forzar
    if not es_dia_comprobacion:
        log.info(f"Hoy no es día de comprobación (se hace los lunes). Nada que hacer.")
        return

    log.info("Iniciando comprobación de códigos electrónicos..." + (" (forzada)" if forzar else ""))
    cambios = comprobar_codigos()

    if cambios:
        html   = generar_email_html(fecha_str, cambios)
        asunto = f"{len(cambios)} norma(s) actualizada(s) · Códigos 020 y 327"
        try:
            enviar_email(asunto, html, email_cfg)
        except Exception as e:
            log.error(f"Error enviando email: {e}")
    else:
        log.info("Sin cambios esta semana. Email omitido.")

    actualizar_data(fecha_str, cambios)
    log.info("✅ Completado")


if __name__ == "__main__":
    main()
