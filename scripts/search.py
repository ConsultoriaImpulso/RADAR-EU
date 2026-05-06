#!/usr/bin/env python3
"""
Radar EU — Fetcher gratuito de licitaciones europeas (v4)
═══════════════════════════════════════════════════════════════════════════
Sin API key. Sin coste. Dependencias: requests + feedparser.
 
CAMBIOS v4 (vs v3):
  ✓ TED API v3 corregida → api.ted.europa.eu/v3/notices/search (URL real)
  ✓ Funding & Tenders ahora usa el RSS oficial (mucho más fiable)
  ✓ Cada fuente reporta su estado individual: ✅ ok / ⚠️ error / 0 items
  ✓ Tabla resumen al final del log
  ✓ Tolerancia a fallos total — una fuente caída no rompe el resto
 
ALCANCE: movilidad, transporte, formación, sostenibilidad, energía,
         smart cities, medio ambiente.
"""
 
from __future__ import annotations
 
import json
import logging
import re
import sys
import uuid
from dataclasses import dataclass, asdict
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
    'ferry', 'aviation', 'aéreo',
    'bicycle', 'bike', 'bicicleta', 'cycling', 'ciclismo', 'fahrrad', 'vélo',
    'logistic', 'logística', 'logistique', 'logistik',
    'freight', 'cargo', 'mercancías',
    'ports', 'puerto', 'shipping', 'maritim',
    'parking', 'estacionamiento',
    'traffic', 'tráfico', 'congestion', 'congestión',
    # Energía / sostenibilidad
    'electric', 'eléctrico', 'electrique', 'elektrisch',
    'hydrogen', 'hidrógeno', 'hydrogène', 'wasserstoff',
    'zero emission', 'cero emisión',
    'clean energy', 'energía limpia',
    'renewable', 'renovable', 'renouvelable', 'erneuerbar',
    'photovoltaic', 'solar', 'wind energy', 'eólic',
    'biofuel', 'biocombustible',
    'sustainable', 'sostenible', 'durable', 'nachhaltig',
    'green', 'verde', 'grün',
    'climate', 'clima',
    'circular econom', 'economía circular',
    'decarbon', 'descarboni',
    'efficiency', 'eficiencia',
    'environment', 'medio ambiente', 'environnement', 'umwelt',
    'biodiversity', 'biodiversidad',
    'pollution', 'contaminación',
    # Smart cities
    'smart city', 'smart cities', 'ciudad inteligente',
    'smart mob', 'smart transport',
    'autonomous', 'autónomo',
    'connected vehicle',
    'CCAM', 'C-ITS',
    'MaaS', 'mobility as a service',
    'IoT', 'internet of things',
    'digital twin', 'gemelo digital',
    '5G', 'artificial intelligence', 'inteligencia artificial',
    'data platform', 'plataforma de datos',
    'urban', 'urbano',
    # Infraestructuras
    'infrastructure', 'infraestructura', 'infrastruktur',
    'road', 'highway', 'carretera', 'autopista',
    'bridge', 'puente', 'tunnel', 'túnel',
    'charging', 'recarga', 'recharge',
    # Formación
    'training', 'formación', 'formacion', 'formation', 'ausbildung',
    'education', 'educación', 'éducation', 'bildung',
    'fellowship', 'scholarship', 'beca', 'bourse', 'stipendium',
    'master programme', 'doctorate', 'phd', 'doctorado',
    'erasmus',
    'capacity building',
    'vocational',
    # Convocatorias
    'open call', 'call for proposal', 'call for tender',
    'convocatoria', 'licitación', 'licitacion', 'concurso',
    'tender', 'procurement',
    'grant', 'funding', 'subvención',
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
# 2. DETECCIÓN DE TEMÁTICA
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
# 4. UTILIDADES
# ════════════════════════════════════════════════════════════════════════
USER_AGENT = 'Mozilla/5.0 (compatible; RadarEU-Bot/4.0; +https://github.com/)'
 
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': USER_AGENT,
    'Accept-Language': 'en;q=0.9,es;q=0.8',
})
 
 
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
 
 
def first_lang(obj) -> str:
    """Extrae el primer texto de un campo multilingüe."""
    if isinstance(obj, dict):
        for lg in ('en', 'eng', 'es', 'fr'):
            v = obj.get(lg)
            if v:
                return v if isinstance(v, str) else str(v)
        if obj:
            v = next(iter(obj.values()), '')
            return v if isinstance(v, str) else str(v)
    if isinstance(obj, list) and obj:
        return first_lang(obj[0])
    return str(obj or '')
 
 
# ════════════════════════════════════════════════════════════════════════
# 5. FETCHER GENÉRICO RSS
# ════════════════════════════════════════════════════════════════════════
def fetch_rss(platform: str, url: str, filter_by_keyword: bool = True) -> list[Tender]:
    """Descarga un feed RSS/Atom y devuelve los items relevantes."""
    log.info(f'📡 {platform:24} ← {url[:65]}')
    try:
        parsed = feedparser.parse(url, agent=USER_AGENT,
                                  request_headers={'Accept-Language': 'en'})
    except Exception as e:
        log.error(f'   ❌ {platform}: {e}')
        return []
 
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
 
        if not title:
            continue
        if filter_by_keyword and not matches(title + ' ' + desc):
            continue
 
        out.append(Tender(
            title=title[:240],
            url=link,
            platform=platform,
            description=desc[:280],
            date=date,
            topic=detect_topic(title, desc),
        ))
    log.info(f'   → {len(out)} resultados')
    return out
 
 
# ════════════════════════════════════════════════════════════════════════
# 6. TED API v3 — JSON oficial (URL CORREGIDA)
# ════════════════════════════════════════════════════════════════════════
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
]
 
 
def fetch_ted() -> list[Tender]:
    """
    TED Search API v3.
    Endpoint correcto: api.ted.europa.eu/v3/notices/search
    Sintaxis de query: expert search (NC, TD, etc.)
    """
    log.info('🇪🇺 TED API v3')
 
    # Construimos query con sintaxis expert-search.
    # Query: solo CPVs que nos interesan, en notices ACTIVE (no expirados)
    cpv_query = ' OR '.join(f'classification-cpv={c}' for c in TED_CPVS)
 
    payload = {
        'query':            cpv_query,
        'fields':           ['publication-number', 'notice-title', 'publication-date',
                             'buyer-name', 'links'],
        'limit':            100,
        'page':             1,
        'scope':            'ACTIVE',
        'checkQuerySyntax': False,
        'paginationMode':   'PAGE_NUMBER',
    }
 
    try:
        r = SESSION.post(
            'https://api.ted.europa.eu/v3/notices/search',
            json=payload, timeout=45,
            headers={'Accept': '*/*', 'Content-Type': 'application/json'},
        )
        r.raise_for_status()
        data = r.json()
    except requests.HTTPError as e:
        # Intentamos leer el body de error para diagnóstico
        try:
            err_body = r.text[:300] if r else ''
        except Exception:
            err_body = ''
        log.warning(f'   ⚠ TED HTTP {e.response.status_code}: {err_body[:200]}')
        return []
    except (requests.RequestException, ValueError) as e:
        log.warning(f'   ⚠ TED error: {e}')
        return []
 
    notices = data.get('notices') or data.get('results') or []
    log.info(f'   → {len(notices)} avisos brutos')
 
    out: list[Tender] = []
    for n in notices:
        title = clean(first_lang(n.get('notice-title') or n.get('title')))[:240]
        if not title:
            continue
 
        pubn = n.get('publication-number', '')
        # El link puede venir en distintas formas
        links = n.get('links') or {}
        if isinstance(links, dict):
            link = (links.get('html') or {}).get('en') or links.get('en') or ''
        else:
            link = ''
        if not link and pubn:
            link = f'https://ted.europa.eu/en/notice/-/detail/{pubn}'
 
        buyer = clean(first_lang(n.get('buyer-name')))
        desc  = f'Comprador: {buyer}' if buyer else ''
 
        out.append(Tender(
            title=f'TED — {title}',
            url=link,
            platform='TED / SIMAP',
            description=desc[:280],
            date=parse_date(n.get('publication-date', '')),
            topic=detect_topic(title, desc),
        ))
    log.info(f'   → {len(out)} licitaciones procesadas')
    return out
 
 
# ════════════════════════════════════════════════════════════════════════
# 7. EU FUNDING & TENDERS PORTAL — RSS oficial
# ════════════════════════════════════════════════════════════════════════
def fetch_funding_tenders() -> list[Tender]:
    """
    Usa el RSS oficial de Funding & Tenders Portal.
    URL confirmada: ec.europa.eu/info/funding-tenders/.../grantTenders-rss.xml
    """
    log.info('💶 EU Funding & Tenders Portal (RSS oficial)')
    url = 'https://ec.europa.eu/info/funding-tenders/opportunities/data/referenceData/grantTenders-rss.xml'
 
    try:
        parsed = feedparser.parse(url, agent=USER_AGENT)
    except Exception as e:
        log.warning(f'   ⚠ Error: {e}')
        return []
 
    if parsed.bozo and not parsed.entries:
        log.warning(f'   ⚠ feed vacío o inválido')
        return []
 
    out: list[Tender] = []
    for e in parsed.entries:
        title = clean(getattr(e, 'title', ''))
        desc  = clean(getattr(e, 'summary', '') or getattr(e, 'description', ''))
        link  = getattr(e, 'link', '')
 
        if not title or not matches(title + ' ' + desc):
            continue
 
        # El RSS pone el deadline dentro del HTML de la descripción
        # Ejemplo: "Deadline: Thu, 28 Nov 2024 17:00:00 (Brussels local time)"
        deadline = None
        m = re.search(r'Deadline[^:]*:\s*([^<]+?)(?:\s*\(|<|$)', desc, re.IGNORECASE)
        if m:
            deadline = parse_date(m.group(1))
 
        # Programa detectado por el callCode/programCode en la URL
        prog = 'EU'
        m_prog = re.search(r'programCode=(\w+)', link)
        if m_prog:
            prog = m_prog.group(1)
 
        platform_name = ('Horizon Europe' if prog == 'HORIZON'
                         else 'LIFE' if prog == 'LIFE'
                         else 'CEF Transport' if prog in ('CEF', 'CEF2')
                         else 'EACEA' if prog in ('ERASMUS', 'EAC', 'CREA')
                         else 'EU Funding & Tenders Portal')
 
        out.append(Tender(
            title=title[:240],
            url=link,
            platform=platform_name,
            description=clean(desc)[:280],
            date=parse_date(getattr(e, 'published', '')),
            deadline=deadline,
            topic=detect_topic(title, desc),
        ))
    log.info(f'   → {len(out)} convocatorias filtradas')
    return out
 
 
# ════════════════════════════════════════════════════════════════════════
# 8. CATÁLOGO DE FUENTES RSS
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
# 9. MERGE SIN DUPLICADOS
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
# 10. MAIN
# ════════════════════════════════════════════════════════════════════════
def main():
    log.info(f'═══ Radar EU v4 — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} ═══')
 
    try:
        with open('data.json', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {'tenders': [], 'last_updated': None, 'last_reset': None}
 
    existing: list[dict] = data.get('tenders', [])
    log.info(f'Existentes: {len(existing)} convocatorias')
 
    all_new: list[Tender] = []
    summary: list[tuple[str, str, int]] = []   # (fuente, status, items)
 
    # ── Fase 1: RSS feeds ───────────────────────────────────────────
    log.info('━━━ Fase 1: Fuentes RSS ━━━')
    for platform, url in RSS_SOURCES:
        try:
            items = fetch_rss(platform, url)
            all_new.extend(items)
            summary.append((platform, '✅', len(items)))
        except Exception as e:
            log.error(f'{platform} explotó: {e}')
            summary.append((platform, '❌', 0))
 
    # ── Fase 2: TED API ──────────────────────────────────────────────
    log.info('━━━ Fase 2: TED API v3 ━━━')
    try:
        items = fetch_ted()
        all_new.extend(items)
        summary.append(('TED / SIMAP', '✅' if items else '⚠', len(items)))
    except Exception as e:
        log.error(f'TED explotó: {e}')
        summary.append(('TED / SIMAP', '❌', 0))
 
    # ── Fase 3: Funding & Tenders RSS ────────────────────────────────
    log.info('━━━ Fase 3: EU Funding & Tenders Portal ━━━')
    try:
        items = fetch_funding_tenders()
        all_new.extend(items)
        summary.append(('Funding & Tenders', '✅' if items else '⚠', len(items)))
    except Exception as e:
        log.error(f'Funding & Tenders explotó: {e}')
        summary.append(('Funding & Tenders', '❌', 0))
 
    # ── Fase 4: Merge ────────────────────────────────────────────────
    log.info(f'━━━ Fase 4: Fusión sin duplicados ━━━')
    log.info(f'Candidatos brutos: {len(all_new)}')
    merged, added = merge_into(existing, all_new)
 
    # ── Fase 5: Persistir ────────────────────────────────────────────
    data['tenders']      = merged
    data['last_updated'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
 
    # ── Resumen final ───────────────────────────────────────────────
    log.info('═══ RESUMEN ═══')
    log.info(f'{"Fuente":<28} {"Estado":<8} {"Items":>6}')
    log.info(f'{"─"*28} {"─"*8} {"─"*6}')
    for plat, st, n in summary:
        log.info(f'{plat:<28} {st:<8} {n:>6}')
    log.info(f'{"─"*28} {"─"*8} {"─"*6}')
    log.info(f'{"TOTAL nuevas añadidas":<28} {"":<8} {added:>6}')
    log.info(f'{"TOTAL acumulado en data.json":<28} {"":<8} {len(merged):>6}')
 
 
if __name__ == '__main__':
    main()
 
