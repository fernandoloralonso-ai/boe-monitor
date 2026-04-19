#!/usr/bin/env python3
"""Diagnóstico v2 — imprime XML crudo para ver estructura real de la API"""
import requests, xml.etree.ElementTree as ET

HEADERS = {"Accept": "application/xml", "User-Agent": "BOEMonitor/3.0"}

fechas = ["20260112", "20260113", "20260114", "20260115"]

for fecha in fechas:
    url = f"https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}"
    print(f"\n{'='*50}")
    print(f"Fecha: {fecha} | URL: {url}")
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        print(f"Status: {r.status_code} | Bytes: {len(r.content)}")

        if r.status_code == 200 and r.content:
            print("XML RAW (primeros 2000 chars):")
            print(r.text[:2000])

            try:
                root = ET.fromstring(r.content)
                print(f"\nRoot tag: {root.tag}")
                tags = set(el.tag for el in root.iter())
                print(f"Tags encontrados: {tags}")

                items = list(root.iter("item"))
                print(f"Items (tag 'item'): {len(items)}")

                if items:
                    print("\nPrimer item XML completo:")
                    print(ET.tostring(items[0], encoding='unicode'))
                    break

                for tag in ["Item", "ITEM", "entrada", "disposicion", "anuncio"]:
                    found = list(root.iter(tag))
                    if found:
                        print(f"Tag alternativo '{tag}': {len(found)} encontrados")
                        print(ET.tostring(found[0], encoding='unicode'))

            except ET.ParseError as e:
                print(f"Error XML parse: {e}")
        else:
            print(f"Respuesta: {r.text[:500]}")

    except Exception as e:
        print(f"Error: {e}")
