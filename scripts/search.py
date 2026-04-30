#!/usr/bin/env python3
"""
Radar EU — Fetcher gratuito de licitaciones europeas (v3)
═══════════════════════════════════════════════════════════════════════════
Sin API key. Sin coste. Dependencias: requests + feedparser (instaladas en CI).

ALCANCE: movilidad, transporte, formación, sostenibilidad, energía,
         smart cities, medio ambiente.

Arquitectura modular:
  ─ Cada fuente vive en su propia función fetch_<source>().
  ─ Todas devuelven list[Tender] con el mismo formato.
  ─ Si una falla, el resto siguen.
  ─ El main() recorre SOURCES y agrega.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Iterable, Optional

import requests
import feedparser


# ════════════════════════════════════════════════════════════════════════
# 0. LOGGING
# ════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s · %(levelname)-7s · %(message)s',
    datefmt='%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger('radar')


# ════════════════════════════════════════════════════════════════════════
# 1. PALABRAS CLAVE — multilingüe
# ════════════════════════════════════════════════════════════════════════
KEYWORDS = [
    # Transporte y movilidad
    'transport', 'transporte', 'mobility', 'movilidad', 'mobilité', 'mobilität', 'mobilità',
    'bus', 'autobús', 'autobus',
    'rail', 'train', 'tren', 'ferroviario', 'eisenbahn', 'ferrovia',
    'tram', 'tranvía', 'metro', 'underground', 'subway',
    'ferry', 'aviation', 'aéreo', 'aerial',
    'bicycle', 'bike', 'bicicleta', 'cycling', 'ciclismo', 'fahrrad', 'vélo',
    'logistic', 'logística', 'logistique', 'logistik',
    'freight', 'cargo', 'mercancías',
    'ports', 'puerto', 'shipping', 'maritim',
    'parking', 'estacionamiento',
    'traffic', 'tráfico', 'congestion', 'congestión',
    # Energía / sostenibilidad
    'electric', 'eléctrico', 'electrique', 'elektrisch',
    'hydrogen', 'hidrógeno', 'hydrogène', 'wasserstoff',
    'zero emission', 'cero emisión', 'zéro émission',
    'clean energy', 'energía limpia', 'énergie propre',
    'renewable', 'renovable', 'renouvelable', 'erneuerbar',
    'photovoltaic', 'fotovoltaic', 'solar',
    'wind energy', 'eólic', 'éolien',
    'biofuel', 'biocombustible',
    'sustainable', 'sostenible', 'durable', 'nachhaltig', 'sostenibile',
    'green', 'verde', 'grün',
    'climate', 'clima', 'klimat',
    'circular econom', 'economía circular',
    'decarbon', 'descarboni',
    'efficiency', 'eficiencia', 'efficacité',
    'environment', 'medio ambiente', 'environnement', 'umwelt', 'ambiente',
    'biodiversity', 'biodiversidad',
    'pollution', 'contaminación',
    # Smart cities
    'smart city', 'smart cities', 'ciudad inteligente', 'ville intelligente',
    'smart mob', 'smart transport',
    'autonomous', 'autónomo', 'autonome',
    'connected vehicle', 'conectado',
    'CCAM', 'C-ITS',
    'MaaS', 'mobility as a service',
    'IoT', 'internet of things',
    'digital twin', 'gemelo digital',
    '5G', 'artificial intelligence', 'inteligencia artificial',
    'data platform', 'plataforma de datos',
    'urban', 'urbano', 'urbain',
    # Infraestructuras
    'infrastructure', 'infraestructura', 'infrastruktur',
    'road', 'highway', 'carretera', 'autoroute', 'autopista',
    'bridge', 'puente', 'tunnel', 'túnel',
    'charging', 'recarga', 'recharge',
    # Formación
    'training', 'formación', 'formacion', 'formation', 'ausbildung', 'formazione',
    'education', 'educación', 'éducation', 'bildung', 'educazione',
    'fellowship', 'scholarship', 'beca', 'bourse', 'stipendium',
    'master programme', 'doctorate', 'phd', 'doctorado',
    'erasmus', 'mobility programme',
    'capacity building', 'desarrollo de capacidades',
    'vocational',
    # Convocatorias
    'open call', 'call for proposal', 'call for tender',
    'convocatoria', 'licitación', 'licitacion', 'concurso',
    'tender', 'procurement', 'contract notice',
    'grant', 'funding', 'subvención', 'subvention',
    # Programas UE
    'CEF', 'connecting europe',
    'horizon europe', 'horizon-cl', 'msca',
    'LIFE programme', 'LIFE 20',
    'eit urban', 'eit climate', 'eit innoenergy',
    'interreg', 'cohesion fund',
    'just transition',
]

KEYWORDS_RE = re.compile('|'.join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)


def matches(text: str) -> bool:
    return bool(KEYWORDS_RE.search(text or ''))


# ════════════════════════════════════════════════════════════════════════
# 2. DETECCIÓN DE TEMÁTICA (etiqueta secundaria)
# ════════════════════════════════════════════════════════════════════════
def detect_topic(title: str, desc: str = '') -> str:
    s = (title + ' ' + desc).lower()
    if any(k in s for k in ['training', 'formación', 'formacion', 'education',
                             'master', 'fellowship', 'beca', 'erasmus', 'eacea',
                             'scholarship', 'vocational', 'phd', 'doctorate']):
        return 'training'
    if any(k in s for k in ['electric', 'hydrogen', 'zero emission', 'eléctric',
                             'hidrógen', 'clean energy', 'verde', 'green mob',
                             'sustainable', 'zero-emission', 'renewable',
                             'solar', 'wind', 'biofuel', 'decarbon',
                             'circular', 'climate', 'environment', 'biodiv']):
        return 'green'
    if any(k in s for k in ['smart city', 'eit urban', 'eltis', 'maas',
                             'digital', 'data platform', 'autonomous', 'ccam',
                             'connected', 'automated mob', 'iot', '5g',
                             'artificial intelligence', 'digital twin']):
        return 'smart'
    if any(k in s for k in ['infrastructure', 'infraestructura', 'road',
                             'highway', 'bridge', 'tunnel', 'carretera', 'cef',
                             'charging', 'recarga']):
        return 'infrastr'
    if any(k in s for k in ['urban mob', 'movilidad urbana', 'cycling',
                             'pedestrian', 'walking', 'metro', 'tram',
                             'city transport', 'urban transport']):
        return 'mobility'
    if any(k in s for k in ['transport', 'bus', 'rail', 'train', 'ferry',
                             'aviation', 'logistics', 'freight', 'cargo',
                             'ferroviario', 'transporte']):
        return 'transport'
    return 'other'


# ════════════════════════════════════════════════════════════════════════
# 3. MODELO DE DATOS
# ════════════════════════════════════════════════════════════════════════
@dataclass
class Tender:
    title: str
    url: str
    platform: str
    description: str = ''
    date: Optional[str] = None
    deadline: Optional[str] = None
    budget: Optional[str] = None
    topic: str = 'other'

    def to_dict(self) -> dict:
        d = asdict(self)
        d['id']       = str(uuid.uuid4())[:12]
        d['added_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return d


# ════════════════════════════════════════════════════════════════════════
# 4. UTILIDADES HTTP / FECHAS / TEXTO
# ════════════════════════════════════════════════════════════════════════
USER_AGENT = 'RadarEU-Bot/3.0 (+https://github.com/)'

SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': USER_AGENT,
    'Accept-Language': 'en;q=0.9,es;q=0.8',
})


def http_get(url: str, timeout: int = 25, **kwargs) -> Optional[requests.Response]:
    try:
        r = SESSION.get(url, timeout=timeout, **kwargs)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        log.warning(f'GET {url[:70]} → {e}')
        return None


def http_post_json(url: str, payload: dict, timeout: int = 30) -> Optional[dict]:
    try:
        r = SESSION.post(url, json=payload, timeout=timeout,
                         headers={'Accept': 'application/json'})
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning(f'POST {url[:70]} → {e}')
        return None


def parse_date(s: str) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s).strftime('%Y-%m-%d')
    except Exception:
        pass
    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ',
                '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(s[:len(fmt)], fmt).strftime('%Y-%m-%d')
        except Exception:
            continue
    if re.match(r'^\d{8}$', s):
        try:
            return datetime.strptime(s, '%Y%m%d').strftime('%Y-%m-%d')
        except Exception:
            pass
    return None


def clean(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text or '').strip()


def first_lang(obj, langs=('en', 'eng', 'es', 'fr')) -> str:
    """Para campos multilingües tipo {'en': '…', 'fr': '…'}."""
    if isinstance(obj, dict):
        for lg in langs:
            v = obj.get(lg)
            if v:
                return v if isinstance(v, str) else str(v)
        if obj:
            v = next(iter(obj.values()), '')
            return v if isinstance(v, str) else str(v)
    if isinstance(obj, list) and obj:
        return first_lang(obj[0], langs)
    return str(obj or '')


# ════════════════════════════════════════════════════════════════════════
# 5. FETCHERS — uno por fuente
# ════════════════════════════════════════════════════════════════════════
def fetch_rss(platform: str, url: str) -> list[Tender]:
    """Fetcher genérico para RSS/Atom usando feedparser."""
    log.info(f'📡 {platform:24} ← {url[:60]}')
    parsed = feedparser.parse(url, agent=USER_AGENT,
                              request_headers={'Accept-Language': 'en;q=0.9,es;q=0.8'})
    if parsed.bozo and not parsed.entries:
        log.warning(f'   ⚠ feed inválido o vacío')
        return []

    out: list[Tender] = []
    for e in parsed.entries:
        title = clean(getattr(e, 'title', ''))
        desc  = clean(getattr(e, 'summary', '') or getattr(e, 'description', ''))
        link  = getattr(e, 'link', '')
        date  = (parse_date(getattr(e, 'published', '')) or
                 parse_date(getattr(e, 'updated', '')))

        if not title or not matches(title + ' ' + desc):
            continue

        out.append(Tender(
            title=title[:240],
            url=link,
            platform=platform,
            description=desc[:280],
            date=date,
            topic=detect_topic(title, desc),
        ))
    log.info(f'   → {len(out)} resultados relevantes')
    return out


# ── TED API v3 ─────────────────────────────────────────────────────────
TED_CPVS = [
    '60100000',  # Servicios transporte por carretera
    '34110000',  # Vehículos de motor
    '34121000',  # Autobuses y autocares
    '34622000',  # Vehículos ferroviarios
    '60200000',  # Servicios transporte ferroviario
    '63110000',  # Carga y descarga / logística
    '71300000',  # Servicios de ingeniería
    '45230000',  # Construcción carreteras / vías férreas
    '09310000',  # Electricidad
    '80500000',  # Servicios de formación
    '80520000',  # Instalaciones formación
    '73000000',  # I+D
    '90700000',  # Servicios medioambientales
    '73100000',  # I+D y consultoría científica
]

def fetch_ted() -> list[Tender]:
    log.info('🇪🇺 TED API v3 (CPV-filtered)')
    today = datetime.now(timezone.utc).date()
    fortnight = (today - timedelta(days=14)).strftime('%Y%m%d')
    today_str = today.strftime('%Y%m%d')

    cpv_q = ' OR '.join(f'classification-cpv={c}' for c in TED_CPVS)
    payload = {
        'query':  f'({cpv_q}) AND publication-date>={fortnight} AND publication-date<={today_str}',
        'fields': ['publication-number', 'title', 'publication-date',
                   'buyer-name', 'description-lot', 'links'],
        'limit':  100,
        'page':   1,
    }
    data = http_post_json('https://ted.europa.eu/api/v3.0/notices/search', payload)
    if not data:
        return []

    notices = data.get('notices') or data.get('results') or []
    out: list[Tender] = []
    for n in notices:
        title = clean(first_lang(n.get('title')))[:240]
        if not title:
            continue

        pubn  = n.get('publication-number', '')
        link  = ((n.get('links') or {}).get('html') or {}).get('en') or \
                f'https://ted.europa.eu/en/notice/-/detail/{pubn}'
        buyer = first_lang(n.get('buyer-name'))
        desc  = clean(first_lang(n.get('description-lot')))[:280] or f'Comprador: {buyer}'

        out.append(Tender(
            title=f'TED — {title}',
            url=link,
            platform='TED / SIMAP',
            description=desc,
            date=parse_date(n.get('publication-date', '')),
            topic=detect_topic(title, desc),
        ))
    log.info(f'   → {len(out)} licitaciones TED')
    return out


# ── EU Funding & Tenders Portal (scraping JSON interno) ─────────────────
def fetch_funding_tenders() -> list[Tender]:
    log.info('💶 EU Funding & Tenders Portal')
    # Endpoint usado por el buscador oficial — devuelve la lista de tópicos
    url = 'https://ec.europa.eu/info/funding-tenders/opportunities/api/screen/opportunities/topic-search.json'
    payload = {
        'languageCode': 'en',
        'sort': {'field': 'sortStatus', 'order': 'ASC'},
        'pageNumber': 1,
        'pageSize': 50,
    }
    data = http_post_json(url, payload)

    # Plan B: el endpoint reference-data más estable
    if not data:
        ref = http_get('https://ec.europa.eu/info/funding-tenders/opportunities/data/referenceData/grantTendersOps.json')
        if not ref:
            return []
        try:
            data = ref.json()
        except Exception as e:
            log.warning(f'   ⚠ JSON inválido: {e}')
            return []

    # Recorrer recursivamente buscando objetos call/topic
    def walk(obj, depth=0):
        if depth > 7:
            return
        if isinstance(obj, dict):
            if obj.get('topicIdentifier') or obj.get('callIdentifier') or obj.get('identifier'):
                yield obj
            for v in obj.values():
                yield from walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v, depth + 1)

    candidates = list(walk(data))
    log.info(f'   → {len(candidates)} entradas en JSON, filtrando por keyword…')

    out: list[Tender] = []
    for c in candidates[:300]:
        title = clean(first_lang(c.get('title') or c.get('topicTitle') or
                                  c.get('name') or ''))
        if not title or not matches(title):
            continue

        cid  = (c.get('topicIdentifier') or c.get('callIdentifier') or
                c.get('identifier') or '')
        link = (f'https://ec.europa.eu/info/funding-tenders/opportunities/'
                f'portal/screen/opportunities/topic-details/{cid}'
                if cid else
                'https://ec.europa.eu/info/funding-tenders/opportunities/portal/')
        prog = c.get('frameworkProgramme') or c.get('programme') or 'EU'
        platform = ('Horizon Europe' if 'HORIZON' in str(prog).upper()
                    else 'LIFE' if 'LIFE' in str(prog).upper()
                    else 'EU Funding & Tenders Portal')

        date = parse_date(c.get('plannedOpeningDate') or
                          c.get('startDate') or
                          c.get('publicationDate') or '')
        deadline = parse_date(c.get('deadlineDate') or
                              c.get('submissionDeadline') or '')

        out.append(Tender(
            title=title[:240],
            url=link,
            platform=platform,
            description=f'Programa: {prog}. ID: {cid}'[:280],
            date=date,
            deadline=deadline,
            topic=detect_topic(title, ''),
        ))
    log.info(f'   → {len(out)} convocatorias filtradas')
    return out


# ════════════════════════════════════════════════════════════════════════
# 6. CATÁLOGO DE FUENTES
# ════════════════════════════════════════════════════════════════════════
RSS_SOURCES = [
    ('EIT Urban Mobility',  'https://www.eiturbanmobility.eu/feed/'),
    ('Eltis',               'https://www.eltis.org/newsroom/rss'),
    ('CINEA',               'https://cinea.ec.europa.eu/news-events/news_en.rss'),
    ('EACEA',               'https://www.eacea.ec.europa.eu/news-events/news_en.rss'),
    ('Horizon Europe',      'https://cordis.europa.eu/news/rss/?lang=en'),
    ('POLIS Network',       'https://www.polisnetwork.eu/feed/'),
    ('UITP',                'https://www.uitp.org/feed/'),
    ('Global Mass Transit', 'https://globalmasstransit.net/feed/'),
    ('ICLEI Europe',        'https://iclei-europe.org/news/?tx_news_pi1[action]=rss'),
]


# ════════════════════════════════════════════════════════════════════════
# 7. MERGE SIN DUPLICADOS
# ════════════════════════════════════════════════════════════════════════
def merge_into(existing: list[dict], new_tenders: Iterable[Tender]) -> tuple[list[dict], int]:
    seen_urls   = {t.get('url',   '').strip().lower() for t in existing if t.get('url')}
    seen_titles = {t.get('title', '').strip().lower() for t in existing}
    added = 0

    for t in new_tenders:
        url   = (t.url or '').strip()
        title = (t.title or '').strip()
        if not title:
            continue
        if (url and url.lower() in seen_urls) or title.lower() in seen_titles:
            continue

        existing.insert(0, t.to_dict())
        if url:
            seen_urls.add(url.lower())
        seen_titles.add(title.lower())
        added += 1
        log.info(f'   + {t.platform[:24]:24} · {title[:80]}')

    return existing, added


# ════════════════════════════════════════════════════════════════════════
# 8. MAIN
# ════════════════════════════════════════════════════════════════════════
def main():
    log.info(f'═══ Radar EU v3 — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} ═══')

    try:
        with open('data.json', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {'tenders': [], 'last_updated': None, 'last_reset': None}

    existing: list[dict] = data.get('tenders', [])
    log.info(f'Existentes: {len(existing)} convocatorias')

    all_new: list[Tender] = []

    # 1) Fuentes RSS
    log.info('─── Fase 1: RSS feeds ────────────────────────')
    for platform, url in RSS_SOURCES:
        try:
            all_new.extend(fetch_rss(platform, url))
        except Exception as e:
            log.error(f'{platform} falló: {e}')

    # 2) TED API
    log.info('─── Fase 2: TED API v3 ───────────────────────')
    try:
        all_new.extend(fetch_ted())
    except Exception as e:
        log.error(f'TED falló: {e}')

    # 3) Funding & Tenders Portal
    log.info('─── Fase 3: EU Funding & Tenders Portal ──────')
    try:
        all_new.extend(fetch_funding_tenders())
    except Exception as e:
        log.error(f'Funding & Tenders falló: {e}')

    # 4) Merge
    log.info(f'─── Fase 4: merge ({len(all_new)} candidatos) ───')
    merged, added = merge_into(existing, all_new)

    # 5) Guardar
    data['tenders']      = merged
    data['last_updated'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log.info(f'═══ Añadidas: {added} nuevas. Total: {len(merged)} ═══')


if __name__ == '__main__':
    main()
