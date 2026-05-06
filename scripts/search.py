#!/usr/bin/env python3
"""
Radar EU — Fetcher gratuito de licitaciones europeas (v6)
═══════════════════════════════════════════════════════════════════════════
3 fuentes finales (ya validadas con logs reales):

  1. TED Search API v3 (api.ted.europa.eu) — licitaciones procurement UE
  2. EU Funding & Tenders Portal (SEDIA):
     - 2a: RSS oficial general (estable)
     - 2b: Endpoint JSON interno (más cobertura, frágil)
  3. EIT Urban Mobility — scraping HTML de su página de calls

Sin API key, sin coste. Dependencias: requests + feedparser.
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
from html.parser import HTMLParser

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
# 1. PALABRAS CLAVE (filtro de F&T)
# ════════════════════════════════════════════════════════════════════════
KEYWORDS = [
    # Transporte y movilidad
    'transport', 'transporte', 'mobility', 'movilidad', 'mobilité', 'mobilität',
    'bus', 'autobús', 'rail', 'train', 'tren', 'ferroviario',
    'tram', 'tranvía', 'metro', 'subway', 'ferry',
    'bicycle', 'bike', 'cycling', 'logistic', 'logística', 'freight', 'cargo',
    'puerto', 'shipping', 'aviation',
    # Energía y sostenibilidad
    'electric', 'eléctrico', 'hydrogen', 'hidrógeno', 'zero emission',
    'clean energy', 'renewable', 'photovoltaic', 'solar', 'wind energy',
    'biofuel', 'sustainable', 'sostenible', 'green', 'verde',
    'climate', 'clima', 'circular econom', 'decarbon',
    'environment', 'medio ambiente', 'biodiversity',
    # Smart cities
    'smart city', 'smart cities', 'ciudad inteligente',
    'smart mob', 'autonomous', 'connected vehicle', 'CCAM', 'C-ITS',
    'MaaS', 'IoT', 'digital twin', 'artificial intelligence',
    'inteligencia artificial', 'urban', 'urbano',
    # Infraestructuras
    'infrastructure', 'infraestructura', 'road', 'highway', 'carretera',
    'bridge', 'puente', 'tunnel', 'túnel', 'charging', 'recarga',
    # Formación
    'training', 'formación', 'formation', 'education', 'educación',
    'fellowship', 'scholarship', 'beca', 'master programme',
    'doctorate', 'phd', 'erasmus', 'capacity building', 'vocational',
    # Convocatorias
    'open call', 'call for proposal', 'call for tender',
    'convocatoria', 'licitación', 'concurso', 'tender',
    'procurement', 'grant', 'funding', 'subvención',
    # Programas UE
    'CEF', 'connecting europe', 'horizon europe', 'horizon-cl', 'msca',
    'LIFE programme', 'LIFE 20', 'eit urban', 'eit climate', 'eit innoenergy',
    'interreg', 'cohesion fund', 'just transition',
]
KEYWORDS_RE = re.compile('|'.join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)


def matches(text: str) -> bool:
    return bool(KEYWORDS_RE.search(text or ''))


# ════════════════════════════════════════════════════════════════════════
# 2. DETECCIÓN DE TEMÁTICA
# ════════════════════════════════════════════════════════════════════════
def detect_topic(title: str, desc: str = '') -> str:
    s = (title + ' ' + desc).lower()
    if any(k in s for k in ['training', 'formación', 'education', 'master',
                             'fellowship', 'beca', 'erasmus', 'eacea',
                             'scholarship', 'vocational', 'phd']):
        return 'training'
    if any(k in s for k in ['electric', 'hydrogen', 'zero emission', 'clean energy',
                             'sustainable', 'renewable', 'solar', 'wind', 'biofuel',
                             'decarbon', 'circular', 'climate', 'environment']):
        return 'green'
    if any(k in s for k in ['smart city', 'eit urban', 'maas', 'digital',
                             'autonomous', 'ccam', 'connected', 'iot',
                             'artificial intelligence', 'digital twin']):
        return 'smart'
    if any(k in s for k in ['infrastructure', 'road', 'highway', 'bridge',
                             'tunnel', 'cef', 'charging']):
        return 'infrastr'
    if any(k in s for k in ['urban mob', 'movilidad urbana', 'cycling',
                             'pedestrian', 'metro', 'tram']):
        return 'mobility'
    if any(k in s for k in ['transport', 'bus', 'rail', 'train', 'ferry',
                             'aviation', 'logistics', 'freight']):
        return 'transport'
    return 'other'


# ════════════════════════════════════════════════════════════════════════
# 3. MODELO DE DATOS
# ════════════════════════════════════════════════════════════════════════
@dataclass
class Tender:
    title: str
    url: str
    platform: str       # Categoría principal (TED, F&T, EIT UM)
    sub_platform: str = ''   # Sub-programa (Horizon, CEF, UMX, RAPTOR, …)
    description: str = ''
    date: Optional[str] = None
    deadline: Optional[str] = None
    budget: Optional[str] = None
    topic: str = 'other'
    type: str = 'other'   # 'opencall' | 'licitacion' | 'beca' | 'other'

    def to_dict(self) -> dict:
        d = asdict(self)
        d['id']       = str(uuid.uuid4())[:12]
        d['added_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return d


# ════════════════════════════════════════════════════════════════════════
# 4. UTILIDADES
# ════════════════════════════════════════════════════════════════════════
USER_AGENT = 'Mozilla/5.0 (compatible; RadarEU-Bot/6.0; +https://github.com/)'

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
                '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y',
                '%d %B %Y', '%d %b %Y'):
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


# Preferencia de idioma para campos multilingües de TED
LANG_PREF = ('es', 'spa', 'en', 'eng', 'fr')


def first_lang(obj, prefer_spanish: bool = True) -> str:
    """Extrae texto de un campo multilingüe. Prioriza español si está disponible."""
    langs = LANG_PREF if prefer_spanish else ('en', 'eng', 'es', 'fr')
    if isinstance(obj, dict):
        for lg in langs:
            v = obj.get(lg)
            if v:
                return v if isinstance(v, str) else str(v)
        if obj:
            v = next(iter(obj.values()), '')
            return v if isinstance(v, str) else str(v)
    if isinstance(obj, list) and obj:
        return first_lang(obj[0], prefer_spanish)
    return str(obj or '')


# ════════════════════════════════════════════════════════════════════════
# 5. TED Search API v3 — solo SERVICIOS (no suministros ni obras)
# ════════════════════════════════════════════════════════════════════════
# Los CPVs (Common Procurement Vocabulary) tienen 8 dígitos + 1 dígito de control.
# La API se consulta sin el dígito de control (sin "-9", "-3", etc).
#
# Filtramos solo SERVICIOS para que las licitaciones traigan estudios,
# consultorías, asistencia técnica, formación, etc. (NO buses, NI obras).
TED_CPVS = [
    # ── Transporte y logística (60-63) ────────────────────────────────
    '60100000',  # Servicios de transporte por carretera
    '60200000',  # Servicios de transporte ferroviario
    '60400000',  # Servicios de transporte aéreo
    '60600000',  # Servicios de transporte marítimo / fluvial
    '63000000',  # Servicios anexos al transporte (logística)
    '63100000',  # Servicios de carga, descarga y almacenamiento

    # ── Ingeniería, urbanismo y consultoría técnica (71) ──────────────
    '71241000',  # Estudios de viabilidad, servicios de asesoramiento, análisis
    '71300000',  # Servicios de ingeniería
    '71311200',  # Servicios de consultoría en sistemas de transporte
    '71311210',  # Servicios de consultoría en materia de carreteras
    '71311220',  # Servicios de ingeniería de tráfico
    '71356000',  # Servicios técnicos
    '71356100',  # Servicios de control técnico
    '71356200',  # Servicios de asistencia técnica
    '71400000',  # Servicios de planificación urbana y arquitectura paisajística
    '71410000',  # Servicios de urbanismo / planificación urbana
    '71600000',  # Servicios técnicos de ensayo, análisis y consultoría
    '71800000',  # Servicios de consultoría para abastecimiento de agua y residuos

    # ── Tecnologías de la Información y datos (72) ────────────────────
    '72000000',  # Servicios TIC: consultoría, desarrollo, instalación
    '72224000',  # Servicios de consultoría en gestión de proyectos
    '72310000',  # Servicios de tratamiento de datos

    # ── Investigación y desarrollo (73) ───────────────────────────────
    '73000000',  # Servicios de I+D y servicios de consultoría conexos
    '73110000',  # Servicios de investigación
    '73100000',  # Servicios de investigación y desarrollo experimental
    '73200000',  # Servicios de consultoría en investigación y desarrollo

    # ── Servicios jurídicos y gestión (79) ────────────────────────────
    '79100000',  # Servicios jurídicos
    '79111000',  # Servicios de asesoría jurídica
    '79140000',  # Servicios de asesoría e información jurídica
    '79400000',  # Servicios de consultoría comercial y de gestión
    '79410000',  # Servicios de consultoría en gestión empresarial
    '79411000',  # Servicios generales de consultoría en gestión
    '79421000',  # Servicios de gestión de proyectos
    '79900000',  # Servicios empresariales diversos

    # ── Educación y formación (80) ────────────────────────────────────
    '80000000',  # Servicios de educación y formación
    '80500000',  # Servicios de formación
    '80520000',  # Instalaciones de formación
    '80540000',  # Servicios de formación medioambiental

    # ── Medio ambiente y sostenibilidad (90) ──────────────────────────
    '90000000',  # Servicios de saneamiento, medio ambiente
    '90700000',  # Servicios medioambientales
    '90710000',  # Gestión medioambiental
    '90711000',  # Evaluación de impacto medioambiental
    '90712000',  # Planificación medioambiental
    '90713000',  # Servicios de asesoramiento / consultoría en asuntos ambientales
    '90720000',  # Protección del medio ambiente
]


def fetch_ted() -> list[Tender]:
    log.info(f'🇪🇺 TED Search API v3 (solo SERVICIOS, {len(TED_CPVS)} CPVs)')
    cpv_query = ' OR '.join(f'classification-cpv={c}' for c in TED_CPVS)
    # NC=services es la sintaxis "expert search" de TED para "Nature of Contract = Services"
    # (excluye Supplies y Works automáticamente)
    full_query = f'({cpv_query}) AND contract-nature=services'

    # Paginación: TED admite hasta 250 por página. Pedimos hasta 3 páginas
    # (750 items máx) para cubrir el aumento de CPVs.
    PAGE_SIZE = 250
    MAX_PAGES = 3

    out: list[Tender] = []
    for page in range(1, MAX_PAGES + 1):
        payload = {
            'query':            full_query,
            'fields':           ['publication-number', 'notice-title',
                                 'publication-date', 'buyer-name',
                                 'buyer-country', 'links'],
            'limit':            PAGE_SIZE,
            'page':             page,
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
        except (requests.RequestException, ValueError) as e:
            log.warning(f'   ⚠ TED página {page}: {e}')
            break

        notices = data.get('notices') or data.get('results') or []
        log.info(f'   página {page}: {len(notices)} avisos brutos')

        for n in notices:
            t = _ted_notice_to_tender(n)
            if t:
                out.append(t)

        # Si la página vino con menos de PAGE_SIZE, ya no hay más
        if len(notices) < PAGE_SIZE:
            break

    log.info(f'   → {len(out)} licitaciones totales (todas las páginas)')
    return out


def _ted_notice_to_tender(n: dict) -> Optional[Tender]:
    """Convierte un aviso bruto de TED en un Tender. Devuelve None si inválido."""
    title = clean(first_lang(n.get('notice-title') or n.get('title')))[:240]
    if not title:
        return None
    pubn = n.get('publication-number', '')

    # FORZAR URL en español. TED admite /es/notice/-/detail/{pub-num} para
    # cualquier aviso. NO usamos n['links'] porque a veces solo trae 'en'.
    if pubn:
        link = f'https://ted.europa.eu/es/notice/-/detail/{pubn}'
    else:
        # Fallback excepcional: usar el link que venga (en cualquier idioma)
        links = n.get('links') or {}
        if isinstance(links, dict):
            html_links = links.get('html') or {}
            link = (html_links.get('es') or html_links.get('en') or
                    links.get('es') or links.get('en') or '')
        else:
            link = ''

    buyer = clean(first_lang(n.get('buyer-name')))
    country = clean(first_lang(n.get('buyer-country')))
    desc_parts = []
    if buyer:   desc_parts.append(f'Comprador: {buyer}')
    if country: desc_parts.append(country)
    desc = ' · '.join(desc_parts)

    return Tender(
        title=f'TED — {title}',
        url=link,
        platform='Tenders Electronic Daily (TED · DOUE Serie S)',
        sub_platform='TED',
        description=desc[:280],
        date=parse_date(n.get('publication-date', '')),
        topic=detect_topic(title, desc),
        type='licitacion',
    )


# ════════════════════════════════════════════════════════════════════════
# 6. F&T Portal — RSS oficial (Plan A, estable)
# ════════════════════════════════════════════════════════════════════════
PROGRAM_MAP = {
    'HORIZON': ('Horizon Europe (HE)',                              'opencall'),
    'LIFE':    ('Programa de Medio Ambiente y Clima (LIFE)',         'opencall'),
    'CEF':     ('Mecanismo Conectar Europa – Transporte (CEF Transport)', 'opencall'),
    'CEF2':    ('Mecanismo Conectar Europa – Transporte (CEF Transport)', 'opencall'),
    'CEFDIG':  ('Mecanismo Conectar Europa – Digital (CEF Digital)', 'opencall'),
    'ERASMUS': ('Erasmus+ (gestionado por EACEA)',                   'beca'),
    'EAC':     ('Erasmus+ (gestionado por EACEA)',                   'beca'),
    'CREA':    ('Europa Creativa (Creative Europe)',                 'opencall'),
    'CERV':    ('Ciudadanos, Igualdad, Derechos y Valores (CERV)',   'opencall'),
    'EU4H':    ('Programa UE por la Salud (EU4Health)',              'opencall'),
    'DIGITAL': ('Europa Digital (Digital Europe)',                   'opencall'),
    'EUI':     ('Iniciativa Urbana Europea (EUI)',                   'opencall'),
    'EITHE':   ('Instituto Europeo de Innovación y Tecnología (EIT)','opencall'),
    'EIT':     ('Instituto Europeo de Innovación y Tecnología (EIT)','opencall'),
    'IF':      ('Fondo de Innovación (Innovation Fund)',             'opencall'),
    'INTERREG':('Cooperación Territorial Europea (Interreg Europe)', 'opencall'),
    'MSCA':    ('Acciones Marie Skłodowska-Curie (MSCA)',           'beca'),
}


def map_program(prog: str) -> tuple[str, str]:
    """Devuelve (sub_platform, type) según programCode."""
    return PROGRAM_MAP.get(prog.upper(), (f'EU Funding & Tenders ({prog})', 'opencall'))


def fetch_funding_tenders_rss() -> list[Tender]:
    log.info('💶 F&T Portal · RSS oficial (Plan A)')
    url = 'https://ec.europa.eu/info/funding-tenders/opportunities/data/referenceData/grantTenders-rss.xml'
    try:
        parsed = feedparser.parse(url, agent=USER_AGENT)
    except Exception as e:
        log.warning(f'   ⚠ Error: {e}')
        return []

    if parsed.bozo and not parsed.entries:
        log.warning(f'   ⚠ feed inválido')
        return []

    out: list[Tender] = []
    for e in parsed.entries:
        title = clean(getattr(e, 'title', ''))
        desc  = clean(getattr(e, 'summary', '') or getattr(e, 'description', ''))
        link  = getattr(e, 'link', '')
        if not title or not matches(title + ' ' + desc):
            continue

        # Deadline en HTML del summary
        deadline = None
        m = re.search(r'Deadline[^:]*:\s*([^<\n]+?)(?:\s*\(|<|$)', desc, re.IGNORECASE)
        if m:
            deadline = parse_date(m.group(1))

        prog = 'EU'
        m_prog = re.search(r'programCode=([A-Z0-9]+)', link)
        if m_prog:
            prog = m_prog.group(1)
        sub, type_ = map_program(prog)

        out.append(Tender(
            title=title[:240],
            url=link,
            platform='Portal de Financiación y Licitaciones de la UE (F&T Portal · SEDIA)',
            sub_platform=sub,
            description=desc[:280],
            date=parse_date(getattr(e, 'published', '')),
            deadline=deadline,
            topic=detect_topic(title, desc),
            type=type_,
        ))
    log.info(f'   → {len(out)} convocatorias (RSS)')
    return out


# ════════════════════════════════════════════════════════════════════════
# 7. F&T Portal — SEDIA Search API (Plan B, frágil)
# ════════════════════════════════════════════════════════════════════════
def fetch_funding_tenders_api() -> list[Tender]:
    """
    Endpoint SEDIA: api.tech.ec.europa.eu/search-api/prod/rest/search
    Documentado en proyectos open-source (Apify, etc.). Sin clave.
    Si falla, simplemente devuelve [] sin romper.
    """
    log.info('💶 F&T Portal · SEDIA API (Plan B)')

    # Probamos varias variantes de payload — diferentes scrapers documentan
    # formatos ligeramente distintos. Cogemos el primero que funcione.
    base = 'https://api.tech.ec.europa.eu/search-api/prod/rest/search'

    # Form variant — el endpoint admite formdata
    form_payload = {
        'apiKey': 'SEDIA',
        'text':   '***',
        'pageSize': '100',
        'pageNumber': '1',
    }
    body = {
        'bool': {
            'must': [{
                'terms': {
                    'type': ['1', '2'],   # 1=Topic, 2=Tender
                },
            }, {
                'terms': {
                    'status': ['31094501', '31094502'],   # Forthcoming + Open
                },
            }],
        },
        'sort': {'field': 'sortStatus', 'order': 'ASC'},
    }

    out: list[Tender] = []
    try:
        r = SESSION.post(
            base,
            params=form_payload,
            data={'query': json.dumps(body), 'languages': '["en"]'},
            timeout=45,
            headers={'Accept': 'application/json'},
        )
        if r.status_code != 200:
            log.warning(f'   ⚠ SEDIA HTTP {r.status_code}: {r.text[:150]}')
            return []
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning(f'   ⚠ SEDIA error: {e}')
        return []

    results = data.get('results') or []
    log.info(f'   → {len(results)} resultados brutos del SEDIA')

    for r_ in results:
        meta = r_.get('metadata') or {}

        # Extraer título — puede estar en distintos campos según el tipo
        title = (clean(first_lang(r_.get('content') or '')) or
                 clean(first_lang(meta.get('title')))).strip()
        if isinstance(title, list) and title:
            title = title[0]
        title = str(title)[:240]
        if not title:
            continue

        # Identificador y URL
        ident = first_lang(meta.get('identifier') or '')
        if isinstance(ident, list) and ident:
            ident = ident[0]
        ident = str(ident).strip()
        url = (f'https://ec.europa.eu/info/funding-tenders/opportunities/'
               f'portal/screen/opportunities/topic-details/{ident.lower()}'
               if ident else r_.get('url') or '')

        # Programa
        prog_raw = meta.get('frameworkProgramme') or meta.get('callProgramme') or 'EU'
        if isinstance(prog_raw, list) and prog_raw:
            prog_raw = prog_raw[0]
        prog = str(prog_raw).strip().upper()
        sub, type_ = map_program(prog)

        # Fechas
        opening = first_lang(meta.get('plannedOpeningDate')) or \
                  first_lang(meta.get('startDate'))
        deadline_raw = first_lang(meta.get('deadlineDate'))
        if isinstance(deadline_raw, list) and deadline_raw:
            deadline_raw = deadline_raw[0]

        out.append(Tender(
            title=title,
            url=url,
            platform='Portal de Financiación y Licitaciones de la UE (F&T Portal · SEDIA)',
            sub_platform=sub,
            description=f'Programa: {prog}. ID: {ident}'[:280],
            date=parse_date(str(opening)),
            deadline=parse_date(str(deadline_raw)),
            topic=detect_topic(title, ''),
            type=type_,
        ))
    log.info(f'   → {len(out)} convocatorias filtradas')
    return out


# ════════════════════════════════════════════════════════════════════════
# 8. EIT Urban Mobility — scraping HTML de la página de calls
# ════════════════════════════════════════════════════════════════════════
class _EitCallsParser(HTMLParser):
    """
    Parser de la página /join-us/call-for-proposals/.
    Cada call sigue el patrón:
      "Open call" ó "Closed call"
      "DEADLINE: <fecha>"
      <h*>Título</h*>
      <p>Descripción</p>
      <a href="…">VIEW MORE</a>

    Usamos máquina de estados sencilla recorriendo el flujo de texto y enlaces.
    """
    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []
        self._current: dict = {}
        self._capture = None         # qué estamos capturando ahora
        self._last_link: str = ''
        self._href_pending: str = ''
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        # Nuevo enlace candidato
        if tag == 'a':
            href = attrs_d.get('href', '')
            if '/call-for-proposals/' in href and href.endswith('/'):
                self._href_pending = href
        # Headings → posibles títulos
        if tag in ('h2', 'h3', 'h4'):
            self._buffer = []
            self._capture = 'title'

    def handle_endtag(self, tag):
        if self._capture == 'title' and tag in ('h2', 'h3', 'h4'):
            title = ' '.join(self._buffer).strip()
            if title and self._current.get('status'):
                self._current['title'] = title
            self._buffer = []
            self._capture = None
        if tag == 'a' and self._href_pending and self._current.get('title') and 'url' not in self._current:
            self._current['url'] = self._href_pending
            self._href_pending = ''
            # Cerrar la call actual y guardar
            if self._current.get('title') and self._current.get('url'):
                self.calls.append(self._current.copy())
            self._current = {}

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return

        # Detectar inicio de bloque por marcador "Open call" / "Closed call"
        low = text.lower()
        if low in ('open call', 'closed call'):
            self._current = {'status': 'open' if low == 'open call' else 'closed'}
            return

        # Deadline
        m = re.match(r'DEADLINE:\s*(.+)', text, re.IGNORECASE)
        if m and self._current:
            self._current['deadline'] = m.group(1).strip()
            return

        # Título acumulado
        if self._capture == 'title':
            self._buffer.append(text)
            return

        # Descripción: si hay current con título pero sin descripción todavía
        if self._current.get('title') and 'desc' not in self._current and len(text) > 30:
            self._current['desc'] = text[:280]


def fetch_eit_urban_mobility() -> list[Tender]:
    log.info('🚲 EIT Urban Mobility · scraping calls page')
    url = 'https://www.eiturbanmobility.eu/join-us/call-for-proposals/'

    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        html = r.text
    except requests.RequestException as e:
        log.warning(f'   ⚠ HTTP error: {e}')
        return []

    parser = _EitCallsParser()
    try:
        parser.feed(html)
    except Exception as e:
        log.warning(f'   ⚠ Parse error: {e}')
        return []

    calls = parser.calls
    log.info(f'   → {len(calls)} bloques detectados')

    # Filtrar solo OPEN calls
    open_calls = [c for c in calls if c.get('status') == 'open']
    log.info(f'   → {len(open_calls)} OPEN calls')

    out: list[Tender] = []
    for c in open_calls:
        title = c.get('title', '').strip()
        if not title:
            continue

        # Detectar sub-programa por keywords del título
        title_l = title.lower()
        if 'umx' in title_l or 'urban mobility explained' in title_l:
            sub = 'Urban Mobility Explained (UMX)'
        elif 'raptor' in title_l:
            sub = 'Rapid Applications for Transport (RAPTOR)'
        elif 'segs' in title_l or 'student entrepreneur' in title_l:
            sub = 'Student Entrepreneur Grant Scheme (SEGS)'
        elif 'strategic innovation' in title_l:
            sub = 'Convocatoria de Innovación Estratégica (Strategic Innovation)'
        elif 'edtech' in title_l:
            sub = 'EdTech Conference Open Call'
        elif 'scaleup' in title_l:
            sub = 'Iniciativa de Promoción de Scaleups (Scaleup Promotion)'
        elif 'master school' in title_l or 'fellowship' in title_l:
            sub = 'Master School & Fellowship'
        elif 'flagship accelerator' in title_l:
            sub = 'Aceleradora Insignia (Flagship Accelerator)'
        elif 'sme market' in title_l:
            sub = 'Expansión de Mercado para PyMEs (SME Market Expansion)'
        elif 'citizens on the move' in title_l:
            sub = 'Citizens on the Move'
        elif 'startup' in title_l:
            sub = 'Programa de Apoyo a Startups'
        elif 'ris' in title_l:
            sub = 'Esquema Regional de Innovación (RIS)'
        else:
            sub = 'Otras convocatorias abiertas'

        # Tipo: si es formación/Master/PhD → beca, sino opencall
        type_ = 'beca' if any(k in title_l for k in
                              ['master', 'fellowship', 'phd', 'student', 'school',
                               'edtech', 'citizens']) else 'opencall'

        out.append(Tender(
            title=title[:240],
            url=c.get('url') or url,
            platform='Instituto Europeo de Innovación y Tecnología – Movilidad Urbana (EIT Urban Mobility)',
            sub_platform=sub,
            description=c.get('desc', '')[:280],
            deadline=parse_date(c.get('deadline', '')),
            topic=detect_topic(title, c.get('desc', '')),
            type=type_,
        ))

    log.info(f'   → {len(out)} EIT UM calls procesadas')
    return out


# ════════════════════════════════════════════════════════════════════════
# 8b. CAF — Banco de Desarrollo de América Latina y el Caribe
# ════════════════════════════════════════════════════════════════════════
# CAF publica DOS listas separadas:
#   - Convocatorias abiertas (research grants, expresiones de interés, etc.)
#     URL: /es/trabaja-con-nosotros/convocatorias/?enrollment=open
#   - Licitaciones abiertas (procurement de servicios consultoría)
#     URL: /es/trabaja-con-nosotros/licitaciones/?status=open
#
# Cada item de la lista enlaza a una página de detalle con título,
# descripción, fechas y país. El listado HTML usa cards con clases
# CSS estables — parseamos con HTMLParser custom igual que EIT UM.

class _CafListParser(HTMLParser):
    """
    Parser robusto de las páginas /convocatorias/ y /licitaciones/ de CAF.

    Estrategia: detecta cualquier <a href="/es/trabaja-con-nosotros/{base_path}/{slug}/">
    y acumula TODO el texto dentro de ese enlace (sin importar las etiquetas internas
    — pueden ser <div>, <span>, <h3>, <p>, lo que sea). Después, en handle_endtag
    cuando se cierra el </a>, limpia y separa título vs descripción.

    Este enfoque resiste cambios de maquetación porque solo necesita:
      1. Que CAF use <a href="..."> para enlazar a cada detalle (estándar HTML)
      2. Que el slug del detalle empiece por /trabaja-con-nosotros/{base_path}/
    """
    def __init__(self, base_path: str):
        super().__init__()
        self.base_path = base_path  # 'convocatorias' o 'licitaciones'
        self.items: list[dict] = []
        self._depth = 0          # profundidad de anidación de <a> activos
        self._current_url = ''
        self._current_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        attrs_d = dict(attrs)
        href = attrs_d.get('href', '')

        # ¿Apunta a un detalle de convocatoria/licitación?
        target = f'/trabaja-con-nosotros/{self.base_path}/'
        if target not in href:
            return

        # Excluir el link a la lista padre (que también contiene el target)
        # Detalles tienen formato: /trabaja-con-nosotros/{base_path}/{slug}/
        # Lista padre es:           /trabaja-con-nosotros/{base_path}/
        # Saltamos el padre comprobando que después de target hay un slug.
        idx = href.find(target)
        rest = href[idx + len(target):].strip('/').strip()
        if not rest or '/' in rest.split('?')[0].rstrip('/'):
            # rest vacío = es la página padre. Si tiene '/' interior, podría
            # ser un sub-recurso raro — lo dejamos pasar igualmente.
            if not rest:
                return

        # Empezamos a capturar
        self._depth += 1
        if self._depth == 1:  # solo el enlace exterior nos importa
            url = href if href.startswith('http') else f'https://www.caf.com{href}'
            self._current_url = url
            self._current_text = []

    def handle_endtag(self, tag):
        if tag != 'a' or self._depth == 0:
            return
        self._depth -= 1
        if self._depth != 0:
            return

        # Cerrar item: hemos terminado de leer todo el texto del enlace exterior
        full_text = ' '.join(' '.join(self._current_text).split()).strip()
        if not full_text or not self._current_url:
            self._current_url = ''
            self._current_text = []
            return

        # Heurística para extraer título y campos
        # Texto típico que vimos en muestras:
        #   "Cierre: 15 junio 2026 Convocatoria abierta Agua"
        #   "Asunción, Paraguay Cierre: 30 abril 2026 Convocatoria abierta casaIntegracion Paraguay Desarrollo institucional"
        #   "Cierre: 06 mayo 2026 Convocatoria abierta Colombia Infraestructura vial"
        info = self._parse_text(full_text)
        info['url'] = self._current_url
        self.items.append(info)

        # Reset
        self._current_url = ''
        self._current_text = []

    def handle_data(self, data):
        if self._depth >= 1:
            txt = data.strip()
            if txt:
                self._current_text.append(txt)

    @staticmethod
    def _parse_text(text: str) -> dict:
        """
        Separa título / país / fecha / status desde un texto plano de card.
        Ejemplo: "Asunción, Paraguay Cierre: 30 abril 2026 Convocatoria abierta casaIntegracion Paraguay Desarrollo institucional"
        """
        info = {'raw': text}

        # 1) Status (Convocatoria abierta/cerrada, Licitación abierta/cerrada)
        m_status = re.search(
            r'(Convocatoria|Licitaci[oó]n)\s+(abierta|cerrada|pendiente)',
            text, re.IGNORECASE
        )
        if m_status:
            info['status'] = m_status.group(2).lower()
            info['kind'] = m_status.group(1).lower()

        # 2) Fecha de cierre
        m_close = re.search(
            r'Cierre[:\s]+(\d{1,2}\s+\w+\s+\d{4})', text, re.IGNORECASE
        )
        if m_close:
            info['deadline_raw'] = m_close.group(1).strip()

        # 3) Título: lo que queda quitando las pistas anteriores
        title = text
        if m_status:
            title = title.replace(m_status.group(0), ' ')
        if m_close:
            title = title.replace(m_close.group(0), ' ')
        # Limpiar espacios múltiples
        title = ' '.join(title.split()).strip(' ·-·,')
        info['title'] = title or text  # fallback al texto crudo si vaciamos todo

        return info


# Meses en español → número (para parse_date local de CAF)
_MES_ES = {
    'enero': '01', 'febrero': '02', 'marzo': '03', 'abril': '04',
    'mayo': '05', 'junio': '06', 'julio': '07', 'agosto': '08',
    'septiembre': '09', 'setiembre': '09', 'octubre': '10',
    'noviembre': '11', 'diciembre': '12',
}


def _parse_caf_date(s: str) -> Optional[str]:
    """Convierte '15 junio 2026' → '2026-06-15'."""
    if not s:
        return None
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', s.strip(), re.IGNORECASE)
    if not m:
        return parse_date(s)  # fallback a parser genérico
    day, month_es, year = m.groups()
    month_num = _MES_ES.get(month_es.lower())
    if not month_num:
        return None
    return f'{year}-{month_num}-{day.zfill(2)}'


def _detect_caf_country(text: str) -> str:
    """Detecta país latinoamericano en el texto."""
    t = text.lower()
    countries = [
        ('argentina', '🇦🇷 Argentina'), ('bolivia', '🇧🇴 Bolivia'),
        ('brasil', '🇧🇷 Brasil'), ('brazil', '🇧🇷 Brasil'),
        ('chile', '🇨🇱 Chile'), ('colombia', '🇨🇴 Colombia'),
        ('costa rica', '🇨🇷 Costa Rica'), ('cuba', '🇨🇺 Cuba'),
        ('ecuador', '🇪🇨 Ecuador'), ('el salvador', '🇸🇻 El Salvador'),
        ('guatemala', '🇬🇹 Guatemala'), ('honduras', '🇭🇳 Honduras'),
        ('méxico', '🇲🇽 México'), ('mexico', '🇲🇽 México'),
        ('nicaragua', '🇳🇮 Nicaragua'), ('panamá', '🇵🇦 Panamá'),
        ('panama', '🇵🇦 Panamá'), ('paraguay', '🇵🇾 Paraguay'),
        ('perú', '🇵🇪 Perú'), ('peru', '🇵🇪 Perú'),
        ('república dominicana', '🇩🇴 R. Dominicana'),
        ('uruguay', '🇺🇾 Uruguay'), ('venezuela', '🇻🇪 Venezuela'),
        ('caribe', '🌎 Caribe'),
        ('regional', '🌎 Regional LATAM'),
        ('américa latina', '🌎 Regional LATAM'),
    ]
    for needle, flag in countries:
        if needle in t:
            return flag
    return '🌎 Regional LATAM'


def _fetch_caf_url(url: str, sub_platform: str, type_: str) -> list[Tender]:
    """
    Scrapea una URL de CAF (convocatorias o licitaciones).

    CAF tiene un certificado SSL que GitHub Actions no valida por defecto
    (cadena incompleta). Estrategia de 3 intentos:
      1. verify=True con bundle de certifi (lo correcto)
      2. verify=False con warnings silenciados (fallback de emergencia)
    """
    base_path = 'convocatorias' if 'convocatorias' in url else 'licitaciones'

    # Intento 1: verificación SSL normal con certifi
    html = None
    try:
        import certifi
        r = SESSION.get(url, timeout=30, verify=certifi.where())
        r.raise_for_status()
        html = r.text
    except requests.exceptions.SSLError:
        log.info(f'   ℹ SSL falla con certifi; reintentando sin verificación...')
    except requests.RequestException as e:
        log.warning(f'   ⚠ HTTP error: {e}')
        return []
    except ImportError:
        # certifi no disponible — saltamos al fallback
        pass

    # Intento 2: sin verificación SSL (fallback)
    if html is None:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r = SESSION.get(url, timeout=30, verify=False)
            r.raise_for_status()
            html = r.text
            log.info(f'   ✓ Conectado sin verificación SSL')
        except requests.RequestException as e:
            log.warning(f'   ⚠ HTTP error (incluso sin SSL): {e}')
            return []

    parser = _CafListParser(base_path)
    try:
        parser.feed(html)
    except Exception as e:
        log.warning(f'   ⚠ Parse error: {e}')
        return []

    log.info(f'   → {len(parser.items)} items detectados en {sub_platform}')

    out: list[Tender] = []
    seen_urls = set()
    for item in parser.items:
        title = item.get('title', '').strip()
        item_url = item.get('url', '').strip()
        raw_text = item.get('raw', '').strip()

        if not title or not item_url:
            continue
        if item_url in seen_urls:
            continue
        seen_urls.add(item_url)

        # País a partir del texto completo (no solo del título)
        country = _detect_caf_country(raw_text or title)

        # Status (abierta/cerrada) — solo nos quedamos con abiertas si las hubiera
        # cerradas en la respuesta (la URL ya filtra por enrollment=open / status=open
        # pero CAF sirve todo y filtra por JS, así que filtramos aquí también)
        status = item.get('status', '')
        if status == 'cerrada':
            continue  # Saltar las cerradas

        # Deadline parseado a YYYY-MM-DD
        deadline = _parse_caf_date(item.get('deadline_raw', ''))

        # Descripción combinando país + status + deadline original legible
        desc_parts = [country]
        if item.get('deadline_raw'):
            desc_parts.append(f'Cierre: {item["deadline_raw"]}')
        full_desc = ' · '.join(desc_parts)

        out.append(Tender(
            title=title[:240],
            url=item_url,
            platform='Banco de Desarrollo de América Latina y el Caribe (CAF)',
            sub_platform=sub_platform,
            description=full_desc[:280],
            deadline=deadline,
            topic=detect_topic(title, raw_text),
            type=type_,
        ))

    return out


def fetch_caf() -> list[Tender]:
    log.info('🌎 CAF · Banco de Desarrollo de América Latina y el Caribe')

    URLS = [
        ('https://www.caf.com/es/trabaja-con-nosotros/convocatorias/?from=&to=&enrollment=open',
         'Convocatorias abiertas',
         'opencall'),
        ('https://www.caf.com/es/trabaja-con-nosotros/licitaciones/?from=&to=&status=open',
         'Licitaciones abiertas',
         'licitacion'),
    ]

    out: list[Tender] = []
    for url, sub_platform, type_ in URLS:
        try:
            items = _fetch_caf_url(url, sub_platform, type_)
            out.extend(items)
            log.info(f'   {sub_platform}: {len(items)} items procesados')
        except Exception as e:
            log.warning(f'   ⚠ Error en {sub_platform}: {e}')

    log.info(f'   → {len(out)} CAF total')
    return out


# ════════════════════════════════════════════════════════════════════════
# 9. MERGE
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
        sub = t.sub_platform or t.platform
        log.info(f'   + [{sub[:20]:20}] {title[:75]}')
    return existing, added


# ════════════════════════════════════════════════════════════════════════
# 10. MAIN
# ════════════════════════════════════════════════════════════════════════
def main():
    log.info(f'═══ Radar EU v6 — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} ═══')

    try:
        with open('data.json', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {'tenders': [], 'last_updated': None, 'last_reset': None}

    existing: list[dict] = data.get('tenders', [])
    log.info(f'Existentes: {len(existing)} convocatorias')

    # ─── Fix retroactivo: pasar URLs viejas de TED de /en/ a /es/ ───
    # (Las 693 antiguas se guardaron con /en/notice/. Las migramos in-place
    # para que al hacer clic se abran en español.)
    fixed_urls = 0
    for t in existing:
        u = t.get('url', '')
        if 'ted.europa.eu/en/notice/' in u:
            t['url'] = u.replace('/en/notice/', '/es/notice/')
            fixed_urls += 1
    if fixed_urls:
        log.info(f'🔧 Migradas {fixed_urls} URLs antiguas de TED a español')

    all_new: list[Tender] = []
    summary: list[tuple[str, str, int]] = []

    # 1. TED
    log.info('━━━━━ Fuente 1: TED / DOUE Serie S ━━━━━')
    try:
        items = fetch_ted()
        all_new.extend(items)
        summary.append(('TED / DOUE Serie S', '✅' if items else '⚠', len(items)))
    except Exception as e:
        log.error(f'TED explotó: {e}')
        summary.append(('TED / DOUE Serie S', '❌', 0))

    # 2a. F&T RSS
    log.info('━━━━━ Fuente 2a: F&T Portal · RSS ━━━━━')
    try:
        items = fetch_funding_tenders_rss()
        all_new.extend(items)
        summary.append(('F&T · RSS', '✅' if items else '⚠', len(items)))
    except Exception as e:
        log.error(f'F&T RSS explotó: {e}')
        summary.append(('F&T · RSS', '❌', 0))

    # 2b. F&T API SEDIA
    log.info('━━━━━ Fuente 2b: F&T Portal · SEDIA API ━━━━━')
    try:
        items = fetch_funding_tenders_api()
        all_new.extend(items)
        summary.append(('F&T · SEDIA API', '✅' if items else '⚠', len(items)))
    except Exception as e:
        log.error(f'F&T API explotó: {e}')
        summary.append(('F&T · SEDIA API', '❌', 0))

    # 3. EIT Urban Mobility
    log.info('━━━━━ Fuente 3: EIT Urban Mobility ━━━━━')
    try:
        items = fetch_eit_urban_mobility()
        all_new.extend(items)
        summary.append(('EIT Urban Mobility', '✅' if items else '⚠', len(items)))
    except Exception as e:
        log.error(f'EIT UM explotó: {e}')
        summary.append(('EIT Urban Mobility', '❌', 0))

    # 4. CAF · Banco de Desarrollo de América Latina y el Caribe
    log.info('━━━━━ Fuente 4: CAF · Banco de Desarrollo LATAM ━━━━━')
    try:
        items = fetch_caf()
        all_new.extend(items)
        summary.append(('CAF · Banco LATAM', '✅' if items else '⚠', len(items)))
    except Exception as e:
        log.error(f'CAF explotó: {e}')
        summary.append(('CAF · Banco LATAM', '❌', 0))

    # Merge
    log.info(f'━━━━━ Merge: {len(all_new)} candidatos ━━━━━')
    merged, added = merge_into(existing, all_new)

    data['tenders']      = merged
    data['last_updated'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Resumen
    log.info('═══ RESUMEN ═══')
    log.info(f'{"Fuente":<28} {"Estado":<8} {"Items":>6}')
    log.info(f'{"─"*28} {"─"*8} {"─"*6}')
    for plat, st, n in summary:
        log.info(f'{plat:<28} {st:<8} {n:>6}')
    log.info(f'{"─"*28} {"─"*8} {"─"*6}')
    log.info(f'{"TOTAL nuevas añadidas":<28} {"":<8} {added:>6}')
    log.info(f'{"TOTAL en data.json":<28} {"":<8} {len(merged):>6}')


if __name__ == '__main__':
    main()
