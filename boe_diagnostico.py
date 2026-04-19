#!/usr/bin/env python3
"""
Script de diagnóstico — ejecutar desde GitHub Actions para ver
qué devuelve exactamente la API del BOE y si las keywords detectan algo.
"""
import requests, xml.etree.ElementTree as ET, json
from pathlib import Path

BASE = Path(__file__).parent

# Probar varios endpoints y fechas
FECHAS_PRUEBA = ['20260113', '20260114', '20260115', '20260116', '20260117']
ENDPOINTS = [
    "https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}",
    "https://www.boe.es/boe/dias/{year}/{month}/{day}/index.php?lang=es",
]

print("=" * 60)
print("DIAGNÓSTICO API BOE")
print("=" * 60)

for fecha in FECHAS_PRUEBA:
    url = f"https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}"
    try:
        r = requests.get(url, timeout=20,
            headers={"Accept": "application/xml", "User-Agent": "BOEMonitor/2.0"})
        print(f"\n{fecha}: status={r.status_code}, bytes={len(r.content)}")

        if r.status_code == 200 and r.content:
            try:
                root = ET.fromstring(r.content)
                items = list(root.iter('item'))
                print(f"  Items totales: {len(items)}")

                if items:
                    item = items[0]
                    print(f"  Tags del item: {[c.tag for c in item]}")
                    print(f"  titulo: {item.findtext('titulo', 'N/A')[:80]}")
                    print(f"  urlHtml: {item.findtext('urlHtml', 'N/A')}")

                    # Mostrar XML crudo del primer item
                    raw = ET.tostring(item, encoding='unicode')
                    print(f"  XML crudo (500 chars): {raw[:500]}")

                    # Probar keywords básicas
                    kws = ['dgt', 'tráfico', 'transporte', 'conducir', 'adr', 'cap']
                    for kw in kws:
                        hits = [i for i in items
                                if kw in (i.findtext('titulo','') or '').lower()]
                        if hits:
                            print(f"  Keyword '{kw}': {len(hits)} hits")
                            print(f"    Ejemplo: {hits[0].findtext('titulo','')[:80]}")

            except ET.ParseError as e:
                print(f"  Error XML: {e}")
                print(f"  Respuesta raw (200 chars): {r.text[:200]}")
        elif r.status_code != 200:
            print(f"  Respuesta: {r.text[:200]}")

    except Exception as e:
        print(f"\n{fecha}: ERROR - {e}")

# Mostrar keywords cargadas
print("\n" + "=" * 60)
print("KEYWORDS CARGADAS")
print("=" * 60)
cfg_file = BASE / "user_config.json"
kw_file  = BASE / "keywords.json"
src = cfg_file if cfg_file.exists() else kw_file
with open(src, encoding="utf-8") as f:
    data = json.load(f)

total_kw = 0
for tematica, val in data.get("tematicas", {}).items():
    kws = val.get("keywords", val) if isinstance(val, dict) else val
    activas = [k.get("texto",k) if isinstance(k,dict) else k for k in kws
               if (k.get("activa",True) if isinstance(k,dict) else True)]
    print(f"  {tematica}: {len(activas)} keywords")
    for k in activas[:3]:
        print(f"    - '{k}'")
    total_kw += len(activas)
print(f"Total: {total_kw} keywords")
