# BOE Monitor 📋

Monitor automático de cambios normativos en el BOE.
Dashboard en GitHub Pages · Alertas por email · Backfill del año en curso.

---

## Archivos del proyecto

| Archivo | Qué es |
|---|---|
| `boe_monitor.py` | Script principal (no tocar) |
| `boe_backfill.py` | Script de backfill histórico (no tocar) |
| `keywords.json` | Keywords base (no tocar) |
| `user_config.json` | ⭐ Tu configuración — se edita desde el dashboard |
| `data.json` | Historial del dashboard — lo genera el script |
| `index.html` | Dashboard web — se sirve desde GitHub Pages |
| `WORKFLOW_monitor.yml` | → Subir como `.github/workflows/monitor.yml` |
| `WORKFLOW_backfill.yml` | → Subir como `.github/workflows/backfill.yml` |

---

## Instalación desde el móvil (paso a paso)

### 1 — Crear repositorio en GitHub

1. Abre [github.com](https://github.com) en el navegador del móvil
2. Toca el **+** → **New repository**
3. Nombre: `boe-monitor`
4. Marca **Private**
5. **NO** marques "Add a README" (debe quedar vacío)
6. Toca **Create repository**

---

### 2 — Subir los archivos normales (uno a uno)

En el repositorio vacío toca **Add file → Upload files** y sube estos archivos:

- `boe_monitor.py`
- `boe_backfill.py`
- `keywords.json`
- `user_config.json`
- `data.json`
- `index.html`
- `README.md`

Toca **Commit changes** al terminar.

---

### 3 — Subir los workflows (con nombre especial)

Los workflows DEBEN estar en la carpeta `.github/workflows/`.
Desde el móvil, créalos así:

**Workflow del monitor diario:**
1. Toca **Add file → Create new file**
2. En el campo del nombre escribe exactamente:
   `.github/workflows/monitor.yml`
   *(al escribir la `/` GitHub crea la carpeta automáticamente)*
3. En el contenido, pega el contenido del archivo `WORKFLOW_monitor.yml`
4. Toca **Commit new file**

**Workflow del backfill:**
1. Toca **Add file → Create new file**
2. Nombre: `.github/workflows/backfill.yml`
3. Contenido: pega el contenido de `WORKFLOW_backfill.yml`
4. Toca **Commit new file**

---

### 4 — Configurar Secrets (datos de email)

**Settings → Secrets and variables → Actions → New repository secret**

| Nombre | Valor |
|---|---|
| `EMAIL_SMTP_SERVER` | `smtp.gmail.com` |
| `EMAIL_SMTP_PORT` | `587` |
| `EMAIL_USUARIO` | `tuemail@gmail.com` |
| `EMAIL_PASSWORD` | Tu App Password de Gmail |
| `EMAIL_DESTINATARIOS` | `tuemail@gmail.com` |

> **Gmail App Password**: myaccount.google.com → Seguridad → Contraseñas de aplicaciones

---

### 5 — Activar GitHub Pages (dashboard)

1. **Settings → Pages**
2. Source: **Deploy from a branch**
3. Branch: `main` · Folder: `/ (root)`
4. Guardar

El dashboard estará en: `https://TU-USUARIO.github.io/boe-monitor/`

---

### 6 — Primera ejecución (backfill del año)

1. Ve a **Actions → BOE Backfill**
2. Toca **Run workflow → Run workflow**
3. Tarda ~1-2 horas (procesa todos los días del año)
4. Cuando termine, el dashboard tendrá todo el historial

---

### 7 — Activar el monitor diario

Ya está programado para correr cada día a las 9:00h automáticamente.
Para probarlo manualmente: **Actions → BOE Monitor Diario → Run workflow**
