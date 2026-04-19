#!/usr/bin/env python3
"""
BOE Monitor v6 — Vigilancia autónoma de legislación
=====================================================
Doble sistema:
1. Sumario diario: nuevas publicaciones filtradas por keywords
2. Legislación consolidada: búsqueda semanal por materias,
   detecta normas nuevas, cambios y derogaciones automáticamente.
   Genera resumen de cambios con Claude AI.
"""

import os, json, smtplib, logging, xml.etree.ElementTree as ET, hashlib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE          = Path(__file__).parent
USER_CFG      = BASE / "user_config.json"
HIST_DIR      = BASE / "historial"
DATA_FILE     = BASE / "data.json"
NORMAS_FILE   = BASE / "normas_estado.json"

BOE_API       = "https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}"
BOE_CONS_API  = "https://www.boe.es/datosabiertos/api/legislacion-consolidada"
BOE_META_API  = "https://www.boe.es/datosabiertos/api/legislacion-consolidada/id/{id}/metadatos"
BOE_TEXT_API  = "https://www.boe.es/datosabiertos/api/legislacion-consolidada/id/{id}"
CLAUDE_API    = "https://api.anthropic.com/v1/messages"
BOE_BASE      = "https://www.boe.es"

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MAX_ALERTAS   = 400
HEADERS_XML   = {"Accept": "application/xml",  "User-Agent": "BOEMonitor/6.0"}
HEADERS_JSON  = {"Accept": "application/json", "User-Agent": "BOEMonitor/6.0"}

# Materias a vigilar en la API consolidada
# Estas son las queries que se lanzan semanalmente
MATERIAS_BUSQUEDA = [
    {"query": "tráfico circulación vehículos seguridad vial", "grupo": "Tráfico y Autoescuelas"},
    {"query": "autoescuela formación vial conductor permiso conducción", "grupo": "Tráfico y Autoescuelas"},
    {"query": "certificado aptitud profesional CAP conductor profesional", "grupo": "CAP"},
    {"query": "mercancías peligrosas ADR transporte peligroso", "grupo": "ADR"},
    {"query": "ordenación transporte terrestre LOTT", "grupo": "Transporte"},
    {"query": "tacógrafo tiempos conducción descanso", "grupo": "Transporte"},
    {"query": "inspección técnica vehículos ITV", "grupo": "Transporte"},
]

# Días de la semana en que se hace la búsqueda consolidada (0=lunes, 6=domingo)
DIA_BUSQUEDA_SEMANAL = 0  # lunes


# ── Config y keywords ─────────────────────────────────────────────────────────
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


# ── Sumario diario ─────────────────────────────────────────────────────────────
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


def filtrar_sumario(items, kw_map):
    res = {}
    for item in items:
        texto = f"{item['titulo']} {item['seccion']} {item['departamento']}".lower()
        for kw, t in kw_map.items():
            if kw in texto:
                res.setdefault(t, []).append(item)
                break
    return res


# ── Legislación consolidada: búsqueda autónoma ────────────────────────────────
def buscar_normas_por_materia(query, limite=50):
    """Busca normas consolidadas por texto. Devuelve lista de normas."""
    try:
        r = requests.get(BOE_CONS_API,
            params={"query": query, "limit": limite},
            headers=HEADERS_JSON, timeout=30)
        if r.status_code != 200:
            log.warning(f"  API consolidada {r.status_code} para '{query}'")
            return []
        data = r.json()
        # La respuesta puede ser lista directa o dentro de un campo
        if isinstance(data, list):
            return data
        for key in ["data", "normas", "items", "results"]:
            if key in data and isinstance(data[key], list):
                return data[key]
        return []
    except Exception as e:
        log.warning(f"  Error búsqueda '{query}': {e}")
        return []


def extraer_campos_norma(norma):
    """Extrae campos clave de una norma independientemente del formato."""
    nid   = ""
    titulo = ""
    fecha  = ""
    estado = ""

    if isinstance(norma, dict):
        # Intentar distintas claves posibles
        for k in ["id", "identificador", "ID"]:
            if k in norma: nid = str(norma[k]); break
        for k in ["titulo", "title", "nombre"]:
            if k in norma: titulo = str(norma[k]); break
        for k in ["fechaActualizacion", "fecha_actualizacion", "fechaModificacion", "fecha"]:
            if k in norma: fecha = str(norma[k]); break
        for k in ["estado", "estadoConsolidacion", "vigencia"]:
            if k in norma: estado = str(norma[k]); break

    return {"id": nid, "titulo": titulo, "fecha": fecha, "estado": estado}


def obtener_fragmento_texto(nid):
    """Obtiene un fragmento del texto consolidado para detectar cambios."""
    if not nid: return ""
    try:
        r = requests.get(BOE_TEXT_API.format(id=nid), timeout=20, headers=HEADERS_XML)
        if r.status_code != 200: return ""
        root = ET.fromstring(r.content)
        parrafos = [el.text.strip() for el in root.iter()
                    if el.text and len(el.text.strip()) > 40]
        texto = " ".join(parrafos[:40])
        return texto[:2500]
    except Exception:
        return ""


def hash_texto(texto):
    return hashlib.md5(texto.encode("utf-8", errors="ignore")).hexdigest()


def cargar_normas_estado():
    if NORMAS_FILE.exists():
        try: return json.loads(NORMAS_FILE.read_text(encoding="utf-8"))
        except: pass
    return {"normas": {}, "ultima_busqueda": ""}


def guardar_normas_estado(estado):
    NORMAS_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


def generar_resumen_claude(tipo, nombre, grupo, texto_anterior, texto_actual):
    """Genera resumen del cambio usando Claude API."""
    if not ANTHROPIC_KEY:
        return "ℹ️ Añade ANTHROPIC_API_KEY en los Secrets de GitHub para obtener resúmenes automáticos."

    if tipo == "nueva":
        prompt = f"""Eres un experto en legislación española de tráfico, transporte y autoescuelas.

Se ha detectado una NUEVA norma en el BOE relacionada con: {grupo}

Norma: {nombre}

Fragmento del texto:
{texto_actual[:2000]}

Por favor proporciona:
1. Resumen ejecutivo (2-3 párrafos): qué regula esta norma
2. Puntos clave que debe conocer un profesor de autoescuela, instructor CAP o formador ADR
3. Si deroga o modifica alguna norma anterior conocida, indícalo"""

    elif tipo == "cambio":
        prompt = f"""Eres un experto en legislación española de tráfico, transporte y autoescuelas.

Se ha detectado una ACTUALIZACIÓN en la siguiente norma: {nombre}
Área: {grupo}

TEXTO ANTERIOR (fragmento):
{texto_anterior[:1200] if texto_anterior else "No disponible"}

TEXTO ACTUAL (fragmento):
{texto_actual[:1200]}

Por favor proporciona:
1. Resumen de los cambios detectados (qué ha cambiado, qué artículos o apartados)
2. Impacto práctico para profesores de autoescuela, instructores CAP o formadores ADR
3. Si el cambio es relevante o es solo una corrección menor, indícalo"""

    elif tipo == "derogada":
        prompt = f"""Eres un experto en legislación española de tráfico y transporte.

La siguiente norma aparece como DEROGADA o sin vigencia:
{nombre} (área: {grupo})

¿Por qué norma suele ser sustituida habitualmente este tipo de regulación?
¿Qué impacto tiene para profesores de autoescuela, instructores CAP o formadores ADR?
Responde brevemente en 2-3 párrafos."""

    else:
        return ""

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


def busqueda_semanal_normas():
    """
    Búsqueda autónoma semanal. Devuelve lista de eventos:
    - nuevas normas detectadas
    - normas con cambios
    - normas derogadas
    """
    estado     = cargar_normas_estado()
    normas_ant = estado.get("normas", {})
    eventos    = []
    normas_act = {}

    log.info("Iniciando búsqueda semanal de legislación consolidada...")

    for materia in MATERIAS_BUSQUEDA:
        query = materia["query"]
        grupo = materia["grupo"]
        log.info(f"  Buscando: '{query}'")

        normas = buscar_normas_por_materia(query)
        log.info(f"  → {len(normas)} normas encontradas")

        for norma_raw in normas:
            campos = extraer_campos_norma(norma_raw)
            nid    = campos["id"]
            titulo = campos["titulo"]
            fecha  = campos["fecha"]
            estado_norma = campos["estado"].lower()

            if not nid or not titulo:
                continue

            # Evitar duplicados entre queries
            if nid in normas_act:
                continue

            normas_act[nid] = {
                "titulo": titulo, "fecha": fecha,
                "grupo": grupo, "estado": estado_norma,
                "url": f"https://www.boe.es/buscar/act.php?id={nid}",
            }

            prev = normas_ant.get(nid)

            # ── Norma NUEVA ──
            if prev is None:
                log.info(f"  ✨ NUEVA: {titulo[:60]}")
                texto = obtener_fragmento_texto(nid)
                resumen = generar_resumen_claude("nueva", titulo, grupo, "", texto)
                normas_act[nid]["texto_hash"] = hash_texto(texto)
                normas_act[nid]["texto_frag"] = texto[:1500]
                eventos.append({
                    "tipo": "nueva", "id": nid, "titulo": titulo,
                    "grupo": grupo, "fecha": fecha, "resumen": resumen,
                    "url": normas_act[nid]["url"],
                })

            # ── Norma MODIFICADA ──
            elif fecha and fecha != prev.get("fecha", ""):
                log.info(f"  🔄 CAMBIO: {titulo[:60]} ({prev.get('fecha','')} → {fecha})")
                texto_actual   = obtener_fragmento_texto(nid)
                texto_anterior = prev.get("texto_frag", "")
                h_nuevo = hash_texto(texto_actual)
                h_viejo = prev.get("texto_hash", "")
                if h_nuevo != h_viejo:
                    resumen = generar_resumen_claude("cambio", titulo, grupo, texto_anterior, texto_actual)
                    normas_act[nid]["texto_hash"] = h_nuevo
                    normas_act[nid]["texto_frag"] = texto_actual[:1500]
                    eventos.append({
                        "tipo": "cambio", "id": nid, "titulo": titulo,
                        "grupo": grupo,
                        "fecha_anterior": prev.get("fecha",""),
                        "fecha_actual": fecha,
                        "resumen": resumen,
                        "url": normas_act[nid]["url"],
                    })
                else:
                    normas_act[nid]["texto_hash"] = h_nuevo
                    normas_act[nid]["texto_frag"] = texto_actual[:1500]

            else:
                # Sin cambios — conservar fragmento
                normas_act[nid]["texto_hash"] = prev.get("texto_hash","")
                normas_act[nid]["texto_frag"] = prev.get("texto_frag","")

    # ── Normas DEROGADAS (estaban antes, no aparecen ahora) ──
    for nid, prev in normas_ant.items():
        if nid not in normas_act:
            titulo = prev.get("titulo","")
            grupo  = prev.get("grupo","")
            log.info(f"  ⚠️ DESAPARECIDA: {titulo[:60]}")
            resumen = generar_resumen_claude("derogada", titulo, grupo, "", "")
            eventos.append({
                "tipo": "derogada", "id": nid, "titulo": titulo,
                "grupo": grupo, "resumen": resumen,
                "url": prev.get("url", f"https://www.boe.es/buscar/act.php?id={nid}"),
            })

    # Guardar estado actualizado
    estado["normas"] = normas_act
    estado["ultima_busqueda"] = datetime.now().isoformat()
    guardar_normas_estado(estado)

    log.info(f"Búsqueda completada: {len(normas_act)} normas vigentes, {len(eventos)} eventos")
    return eventos


# ── Email ──────────────────────────────────────────────────────────────────────
ICONOS = {"nueva": "✨", "cambio": "🔄", "derogada": "⚠️"}
COLORES = {"nueva": "#e8f5e9", "cambio": "#fff8e1", "derogada": "#fce4ec"}
ETIQUETAS = {"nueva": "NUEVA NORMA", "cambio": "ACTUALIZACIÓN", "derogada": "POSIBLE DEROGACIÓN"}
BADGE_COLORS = {"nueva": "#27ae60", "cambio": "#c9973a", "derogada": "#c0392b"}


def generar_email_html(fecha_str, por_tematica, es_nuevo, eventos_normas):
    fecha_fmt = datetime.strptime(fecha_str, "%Y%m%d").strftime("%d/%m/%Y")
    total_pub  = sum(len(v) for v in por_tematica.values())
    nuevos_pub = sum(1 for v in es_nuevo.values() if v)

    # Sección eventos normas
    html_normas = ""
    if eventos_normas:
        html_normas = f'''
        <div style="padding:20px 30px;border-bottom:3px solid #1a1a2e">
          <h2 style="margin:0 0 16px;color:#1a1a2e;font-size:19px;font-family:Georgia,serif">
            ⚖️ Cambios en legislación consolidada ({len(eventos_normas)})
          </h2>'''
        for ev in eventos_normas:
            tipo   = ev.get("tipo","cambio")
            icono  = ICONOS.get(tipo,"🔄")
            color  = COLORES.get(tipo,"#fff8e1")
            badge  = ETIQUETAS.get(tipo,"CAMBIO")
            bcol   = BADGE_COLORS.get(tipo,"#c9973a")
            fecha_extra = ""
            if tipo == "cambio" and ev.get("fecha_anterior"):
                fecha_extra = f'<span style="font-size:10px;color:#888;font-family:monospace">{ev["fecha_anterior"]} → {ev["fecha_actual"]}</span>'
            elif tipo == "nueva" and ev.get("fecha"):
                fecha_extra = f'<span style="font-size:10px;color:#888;font-family:monospace">Publicada: {ev["fecha"]}</span>'

            html_normas += f'''
            <div style="background:{color};border:1px solid #ddd;border-radius:4px;padding:16px;margin-bottom:14px">
              <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
                <span style="background:{bcol};color:#fff;padding:2px 10px;border-radius:10px;font-size:10px;font-weight:700;font-family:monospace">{badge}</span>
                <span style="font-size:11px;color:#888">{ev.get("grupo","")}</span>
                {fecha_extra}
              </div>
              <div style="font-weight:700;color:#1a1a2e;margin-bottom:10px;font-size:14px">
                {icono} <a href="{ev.get('url','')}" style="color:#1a1a2e;text-decoration:none">{ev.get('titulo','')}</a>
              </div>
              <div style="font-size:13px;color:#444;line-height:1.7;white-space:pre-wrap;background:rgba(255,255,255,.6);padding:10px;border-radius:3px">{ev.get('resumen','')}</div>
            </div>'''
        html_normas += '</div>'

    # Sección sumario diario
    filas = ""
    if por_tematica:
        for t, items in sorted(por_tematica.items()):
            filas += f'<tr><td colspan="4" style="background:#1a1a2e;color:#e2b96f;padding:9px 14px;font-weight:700;font-size:12px">📌 {t}</td></tr>'
            for item in items:
                nuevo = es_nuevo.get(item["id"], True)
                bg    = "#fffbf2" if nuevo else "#fff"
                badge = ('<span style="background:#c9973a;color:#1a1a2e;padding:2px 7px;border-radius:8px;font-size:10px;font-weight:700">NUEVO</span>'
                         if nuevo else '<span style="color:#ccc;font-size:10px">—</span>')
                pdf = f' · <a href="{item["url_pdf"]}" style="color:#888;font-size:11px">PDF</a>' if item.get("url_pdf") else ""
                filas += f'''<tr style="background:{bg}">
                  <td style="padding:9px 14px;border-bottom:1px solid #eee;max-width:380px">
                    <a href="{item["url_html"]}" style="color:#1a1a2e;font-weight:500;text-decoration:none;font-size:13px">{item["titulo"]}</a>
                  </td>
                  <td style="padding:9px 14px;border-bottom:1px solid #eee;color:#666;font-size:11px">{item["departamento"] or "—"}</td>
                  <td style="padding:9px 14px;border-bottom:1px solid #eee;text-align:center">{badge}</td>
                  <td style="padding:9px 14px;border-bottom:1px solid #eee;white-space:nowrap;font-size:11px">
                    <a href="{item["url_html"]}" style="color:#2471a3">HTML</a>{pdf}
                  </td>
                </tr>'''

    tabla_sumario = ""
    if por_tematica:
        tabla_sumario = f'''
        <div style="padding:20px 30px">
          <h2 style="margin:0 0 14px;color:#1a1a2e;font-size:17px;font-family:Georgia,serif">
            📋 Nuevas publicaciones BOE hoy ({total_pub})
          </h2>
          <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#f5f4f0">
              <th style="padding:7px 14px;text-align:left;color:#666;border-bottom:2px solid #ddd;font-size:10px;letter-spacing:1px;text-transform:uppercase">Título</th>
              <th style="padding:7px 14px;text-align:left;color:#666;border-bottom:2px solid #ddd;font-size:10px;letter-spacing:1px;text-transform:uppercase">Organismo</th>
              <th style="padding:7px 14px;color:#666;border-bottom:2px solid #ddd;font-size:10px;text-align:center">Estado</th>
              <th style="padding:7px 14px;color:#666;border-bottom:2px solid #ddd;font-size:10px">Ver</th>
            </tr>{filas}
          </table>
        </div>'''

    sin_nada = ""
    if not por_tematica and not eventos_normas:
        sin_nada = '<p style="padding:24px 30px;color:#aaa;font-style:italic;font-size:13px">Sin novedades hoy.</p>'

    partes_resumen = []
    if eventos_normas: partes_resumen.append(f"{len(eventos_normas)} cambio(s) legislativo(s)")
    if nuevos_pub:     partes_resumen.append(f"{nuevos_pub} publicación(es) nueva(s)")
    resumen_cab = " · ".join(partes_resumen) if partes_resumen else "Sin novedades"

    return (f'<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            f'<body style="margin:0;padding:16px;background:#f0f0f0;font-family:Georgia,serif">'
            f'<div style="max-width:900px;margin:0 auto;background:#fff;border-radius:4px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.12)">'
            f'<div style="background:#1a1a2e;padding:24px 30px">'
            f'<div style="color:#e2b96f;font-size:10px;letter-spacing:3px;text-transform:uppercase;margin-bottom:6px">Monitor Normativo · Autoescuela · CAP · ADR</div>'
            f'<h1 style="margin:0;color:#fff;font-size:22px;font-weight:400">BOE · {fecha_fmt}</h1>'
            f'<p style="margin:6px 0 0;color:#888;font-size:12px">{resumen_cab}</p>'
            f'</div>'
            f'{html_normas}{tabla_sumario}{sin_nada}'
            f'<div style="padding:14px 30px;background:#f8f8f8;font-size:10px;color:#aaa;text-align:center">'
            f'BOE Monitor v6 · {datetime.now().strftime("%d/%m/%Y %H:%M")} · '
            f'<a href="https://www.boe.es" style="color:#aaa">boe.es</a>'
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


# ── Historial y data.json ──────────────────────────────────────────────────────
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


def actualizar_data(fecha_str, por_tematica, es_nuevo, eventos_normas):
    data = cargar_data()
    items_planos = []
    for t, items in por_tematica.items():
        for item in items:
            items_planos.append({
                "id": item["id"], "titulo": item["titulo"],
                "departamento": item["departamento"], "tematica": t,
                "url_html": item["url_html"], "url_pdf": item.get("url_pdf",""),
                "extracto": "", "nuevo": es_nuevo.get(item["id"], True),
            })
    fd = f"{fecha_str[:4]}-{fecha_str[4:6]}-{fecha_str[6:]}"
    entrada = {
        "fecha": fd, "total": len(items_planos),
        "nuevos": sum(1 for i in items_planos if i["nuevo"]),
        "tematicas": list(por_tematica.keys()), "items": items_planos,
        "eventos_normas": eventos_normas,
        "ejecutado": datetime.now().isoformat(),
    }
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


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    hoy       = datetime.now()
    fecha_str = hoy.strftime("%Y%m%d")
    cfg       = cargar_config()
    kw_map    = build_kw_map(cfg)
    historial = cargar_historial(fecha_str)
    email_cfg = get_email_cfg()
    forzar_busqueda = os.environ.get("FORZAR_BUSQUEDA_NORMAS", "").lower() == "true"

    log.info(f"=== BOE Monitor v6 · {hoy.strftime('%d/%m/%Y')} ===")

    # 1. Búsqueda semanal de legislación consolidada
    # Se ejecuta los lunes o si se fuerza manualmente
    eventos_normas = []
    es_lunes = hoy.weekday() == DIA_BUSQUEDA_SEMANAL
    if es_lunes or forzar_busqueda:
        log.info("Ejecutando búsqueda semanal de legislación..." + (" (forzada)" if forzar_busqueda else ""))
        eventos_normas = busqueda_semanal_normas()
    else:
        log.info(f"Búsqueda semanal omitida (se ejecuta los lunes, hoy es {hoy.strftime('%A')})")

    # 2. Sumario diario
    items        = obtener_sumario(fecha_str)
    por_tematica = filtrar_sumario(items, kw_map) if items else {}
    log.info(f"Publicaciones relevantes: {sum(len(v) for v in por_tematica.values())}")

    es_nuevo = {i["id"]: i["id"] not in historial
                for t_items in por_tematica.values() for i in t_items}

    hay_novedades = eventos_normas or any(es_nuevo.values())

    if hay_novedades and email_cfg["usuario"] and email_cfg["password"]:
        partes = []
        if eventos_normas: partes.append(f"{len(eventos_normas)} cambio(s) legislativo(s)")
        n = sum(1 for v in es_nuevo.values() if v)
        if n: partes.append(f"{n} publicación(es) nueva(s)")
        asunto = " · ".join(partes)
        html   = generar_email_html(fecha_str, por_tematica, es_nuevo, eventos_normas)
        try:
            enviar_email(f"BOE {hoy.strftime('%d/%m/%Y')} — {asunto}", html, email_cfg)
        except Exception as e:
            log.error(f"Error email: {e}")
    else:
        log.info("Sin novedades hoy, email omitido.")

    todos_ids = historial | {i["id"] for t in por_tematica.values() for i in t}
    guardar_historial(fecha_str, todos_ids)
    actualizar_data(fecha_str, por_tematica, es_nuevo, eventos_normas)
    log.info("✅ Completado")


if __name__ == "__main__":
    main()
