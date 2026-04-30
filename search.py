#!/usr/bin/env python3
"""
Radar EU — Búsqueda automática por hora
Llama a la API de Anthropic con web_search para encontrar
licitaciones y open calls europeos de movilidad/transporte/formación.
Guarda los resultados en data.json sin duplicados.
"""

import anthropic
import json
import os
import re
import uuid
from datetime import datetime, timezone

TOPICS_MAP = {
    "mobility": "Movilidad urbana",
    "transport": "Transporte",
    "training": "Formación",
    "infrastr": "Infraestructuras",
    "smart": "Smart cities",
    "green": "Movilidad verde",
    "other": "Otras",
}

PROMPT = """Busca las licitaciones, concursos y open calls europeos MÁS RECIENTES (publicados en los últimos 7 días) relacionados con:
- Movilidad urbana y transporte sostenible
- Formación profesional en transporte y movilidad
- Smart cities y movilidad inteligente
- Infraestructuras de transporte
- Movilidad verde (eléctrica, hidrógeno, etc.)

Busca en estas plataformas europeas oficiales:
- TED / SIMAP (ted.europa.eu)
- EU Funding & Tenders Portal (ec.europa.eu/info/funding-tenders)
- CINEA (cinea.ec.europa.eu)
- EACEA (eacea.ec.europa.eu)
- EIT Urban Mobility (eiturbanmobility.eu)
- Eltis (eltis.org)
- Horizon Europe
- CEF Transport
- EU Rail (rail-research.europa.eu)
- INEA

Devuelve ÚNICAMENTE un JSON array válido, sin texto adicional, sin bloques de código markdown, sin comentarios:
[
  {
    "title": "Nombre completo de la convocatoria",
    "platform": "Nombre exacto de la plataforma",
    "url": "URL directa a la convocatoria específica",
    "date": "YYYY-MM-DD fecha de publicación",
    "deadline": "YYYY-MM-DD fecha límite o null",
    "topic": "mobility|transport|training|infrastr|smart|green|other",
    "budget": "importe o null",
    "description": "Resumen en español de máximo 80 palabras"
  }
]

Devuelve mínimo 5 convocatorias reales y verificadas. Solo JSON puro."""


def run_search(client: anthropic.Anthropic) -> list[dict]:
    """Run AI search and return list of tender dicts."""
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": PROMPT}],
    )

    full_text = ""
    for block in response.content:
        if block.type == "text":
            full_text += block.text

    # Extract JSON array from the response
    match = re.search(r"\[[\s\S]*\]", full_text)
    if not match:
        print("⚠️  No JSON array found in response")
        print("Response text:", full_text[:500])
        return []

    try:
        items = json.loads(match.group())
        print(f"✓ Parsed {len(items)} items from AI response")
        return items
    except json.JSONDecodeError as e:
        print(f"⚠️  JSON parse error: {e}")
        return []


def load_data() -> dict:
    """Load existing data.json."""
    try:
        with open("data.json", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"tenders": [], "last_updated": None, "last_reset": None}


def save_data(data: dict) -> None:
    """Save data.json with updated timestamp."""
    data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✓ Saved data.json ({len(data['tenders'])} tenders total)")


def merge_tenders(existing: list[dict], new_items: list[dict]) -> tuple[list[dict], int]:
    """Merge new tenders into existing list, avoiding duplicates. Returns (merged, added_count)."""
    existing_urls = {t.get("url", "").strip().lower() for t in existing if t.get("url")}
    existing_titles = {t.get("title", "").strip().lower() for t in existing}

    added = 0
    for item in new_items:
        title = item.get("title", "").strip()
        url = item.get("url", "").strip()

        if not title:
            continue

        # Check for duplicates by URL or title
        is_dup = (
            (url and url.lower() in existing_urls)
            or title.lower() in existing_titles
        )

        if not is_dup:
            item["id"] = item.get("id") or str(uuid.uuid4())[:12]
            item["added_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            existing.insert(0, item)
            if url:
                existing_urls.add(url.lower())
            existing_titles.add(title.lower())
            added += 1
            print(f"  + Added: {title[:70]}")
        else:
            print(f"  ~ Skipped (dup): {title[:70]}")

    return existing, added


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("⚠️  ANTHROPIC_API_KEY not set. Exiting.")
        return

    client = anthropic.Anthropic(api_key=api_key)

    print(f"🔍 Starting search at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    data = load_data()
    existing = data.get("tenders", [])
    print(f"   Existing tenders: {len(existing)}")

    new_items = run_search(client)

    if new_items:
        merged, added = merge_tenders(existing, new_items)
        data["tenders"] = merged
        save_data(data)
        print(f"\n✅ Done. Added {added} new tenders. Total: {len(merged)}")
    else:
        # Still update timestamp even if no new results
        save_data(data)
        print("ℹ️  No new tenders found this run.")


if __name__ == "__main__":
    main()
