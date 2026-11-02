"""Optional public sources, invoked only by the API-owned research engine.

Registry records and patent disclosures are source documents, not journal papers.
Snapshots retain provider responses; deterministic rendering supplies citable text.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Literal, TypedDict
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

import httpx

SourceName = Literal['clinicaltrials', 'epo', 'osf']
NAMES = ('clinicaltrials', 'epo', 'osf')
KINDS = {'clinicaltrials': 'clinical_trial_registry', 'epo': 'patent_disclosure', 'osf': 'study_registration'}
LABELS = {'clinicaltrials': 'ClinicalTrials.gov', 'epo': 'EPO OPS', 'osf': 'OSF Registrations'}
LIMITATIONS = {
    'clinicaltrials': 'Registered studies and posted aggregate results only; missing results are not null findings. No participant-level data. Coverage is limited to ClinicalTrials.gov.',
    'epo': 'OPS searches bibliographic fields, not descriptions/claims; at most 2,000 hits per query. Machine-readable description/claim coverage varies by authority. Patent disclosures do not establish experimental efficacy or legal status.',
    'osf': 'OSF public registration metadata search; acquired registration responses and available form schema only. Linked papers, files and datasets are not automatically read. Registration timing must be checked before calling a plan prospective; plans do not establish results.',
}
GUIDANCE = """
Optional sources are explicitly enabled: {sources}. OpenAlex remains the paper source.
Every query must declare its source. Include at least one query for OpenAlex and each enabled source.
Use provider-specific queries: clinicaltrials uses ClinicalTrials.gov query.term (plain words, quoted phrases, AND/OR and AREA fields);
epo uses OPS CQL, e.g. ta=\"continuous glucose\" with AND/OR, applicant, publication dates or CPC; never claim description/claims search;
osf uses OSF Trove registration metadata search (plain words or quoted phrases; | for OR, + for AND).
Preserve query IDs and sources when adding searches. Read additional-sources.json for per-source coverage and failures.
Candidate source_kind and source_metadata identify the actual document type. Acquire and inspect relevant records from each enabled source;
record explicit irrelevance/access limitations if a source cannot contribute. Never silently omit an enabled source.
Clinical registry protocol fields are plans; only posted resultsSection contains aggregate results. Preserve arm IDs, denominators,
time points, endpoint definitions and adverse-event thresholds. Registration, a trial report and a patent may describe the same study.
OSF registrations are plans, not completed experiments; inspect response labels, registration/revision dates and withdrawn status.
EPO descriptions and claims are disclosures, not independent empirical confirmation; report the retrieved and missing sections.
These additional documents do not satisfy the minimum-paper target. Count full journal/preprint publications separately.
Use them for typed, attributed evidence with exact canonical passages; do not invent findings when a source provides only intentions.
"""


class SourceStatus(TypedDict):
    source: SourceName
    label: str
    status: Literal['not_searched', 'complete', 'incomplete', 'failed']
    query_ids: list[str]
    candidates: int
    retrieved: int
    limitations: str
    failures: list[str]


def validate_names(value):
    if not isinstance(value, list) or any(not isinstance(x, str) or x not in NAMES for x in value):
        raise ValueError('additional_sources must be a list containing only clinicaltrials, epo, osf')
    if len(value) != len(set(value)):
        raise ValueError('additional_sources must not contain duplicates')
    return list(value)


def configured(root, sources):
    if 'epo' in sources:
        from .coverage_runtime import _operator_setting
        if not all(_operator_setting(root, name) for name in ('EPO_OPS_KEY', 'EPO_OPS_SECRET')):
            raise ValueError('EPO OPS requires EPO_OPS_KEY and EPO_OPS_SECRET in the service environment or data-directory .env')


def identifier(provider, native_id):
    return 'S' + hashlib.sha256((provider + ':' + native_id).encode()).hexdigest()[:24]


def _work(provider, native_id, title, abstract, date, url, metadata, authors=()):
    return {'id': identifier(provider, native_id), 'display_name': title, 'abstract': abstract,
            'publication_year': int(date[:4]) if date and re.match(r'^\d{4}', date) else None,
            'doi': None, 'authorships': [{'author': {'display_name': a}} for a in authors],
            'adapter': provider, 'source_kind': KINDS[provider], 'record_url': url,
            'source_metadata': dict(metadata, native_id=native_id, record_date=date),
            'open_access': {'is_oa': None}}


def clinical_record(data):
    protocol = data['protocolSection']
    identification = protocol['identificationModule']
    native_id = identification['nctId']
    if not re.fullmatch(r'NCT\d{8}', native_id):
        raise ValueError('Invalid ClinicalTrials.gov identifier')
    status = protocol.get('statusModule', {})
    date = status.get('lastUpdatePostDateStruct', {}).get('date')
    return _work('clinicaltrials', native_id, identification.get('briefTitle') or identification.get('officialTitle'),
                 protocol.get('descriptionModule', {}).get('briefSummary', ''), date,
                 'https://clinicaltrials.gov/study/' + native_id,
                 {'has_results': bool(data.get('hasResults') or data.get('resultsSection')),
                  'study_status': status.get('overallStatus'), 'last_update_post_date': date,
                  'first_post_date': status.get('studyFirstPostDateStruct', {}).get('date'),
                  'related_publications': protocol.get('referencesModule', {}).get('references', [])})


def _values(record, key, field='@value'):
    return [v[field] for v in record.get(key, []) if isinstance(v, dict) and isinstance(v.get(field), str)]


def osf_record(data):
    url = data.get('@id', '')
    match = re.fullmatch(r'https://osf\.io/([a-z0-9]{5,16})/?', url)
    if not match or 'Registration' not in _values(data, 'resourceType', '@id'):
        raise ValueError('OSF search returned an invalid registration')
    return _work('osf', match[1], ' '.join(_values(data, 'title')), '\n'.join(_values(data, 'description')),
                 next(iter(_values(data, 'dateModified') or _values(data, 'dateCreated')), None), url,
                 {'resource_type': 'Registration', 'identifiers': _values(data, 'identifier')},
                 [name for person in data.get('creator', []) for name in _values(person, 'name')])


def _tag(element):
    return element.tag.rsplit('}', 1)[-1]


def _text(element):
    return ' '.join(''.join(element.itertext()).split())


def _elements(element, name):
    return [e for e in element.iter() if _tag(e) == name]


def epo_records(raw):
    root = ET.fromstring(raw)
    records = []
    for document in _elements(root, 'exchange-document'):
        country, number, kind = (document.get(k, '') for k in ('country', 'doc-number', 'kind'))
        if not re.fullmatch(r'[A-Z]{2}', country) or not re.fullmatch(r'[A-Za-z0-9]+', number) or not re.fullmatch(r'[A-Z][0-9]?', kind):
            raise ValueError('Invalid OPS publication identifier')
        native_id = '.'.join((country, number, kind))
        titles = _elements(document, 'invention-title')
        title = next((_text(x) for x in titles if x.get('lang') == 'en'), _text(titles[0]) if titles else native_id)
        abstracts = _elements(document, 'abstract')
        abstract = next((_text(x) for x in abstracts if x.get('lang') == 'en'), _text(abstracts[0]) if abstracts else '')
        publications = _elements(document, 'publication-reference')
        dates = _elements(publications[0], 'date') if publications else []
        date = _text(dates[0]) if dates else None
        if date and re.fullmatch(r'\d{8}', date):
            date = date[:4] + '-' + date[4:6] + '-' + date[6:]
        records.append(_work('epo', native_id, title, abstract, date,
            f'https://worldwide.espacenet.com/publicationDetails/biblio?CC={country}&NR={number}{kind}',
            {'publication_number': native_id, 'family_id': document.get('family-id'), 'publication_date': date}))
    searches = _elements(root, 'biblio-search')
    if not searches: raise ValueError('OPS response has no bibliographic search result')
    total = int(searches[0].attrib['total-result-count'])
    return records, total


_TOKENS, _TOKEN_LOCK = {}, threading.Lock()


class Client:
    def __init__(self, http, root):
        self.http, self.root = http, root

    def _headers(self):
        from .coverage_runtime import _operator_setting
        configured(self.root, ['epo'])
        key, secret = (_operator_setting(self.root, name) for name in ('EPO_OPS_KEY', 'EPO_OPS_SECRET'))
        identity = hashlib.sha256((key + ':' + secret).encode()).hexdigest()
        with _TOKEN_LOCK:
            token, expires = _TOKENS.get(identity, ('', 0))
            if expires <= time.monotonic():
                response = self.http.post('https://ops.epo.org/3.2/auth/accesstoken',
                    auth=(key, secret), data={'grant_type': 'client_credentials'})
                response.raise_for_status()
                body = response.json()
                token = body['access_token']
                _TOKENS[identity] = token, time.monotonic() + max(0, int(body.get('expires_in', 1200)) - 30)
        return {'Authorization': 'Bearer ' + token, 'Accept': 'application/xml'}

    def search(self, provider, search, cursor):
        if provider == 'clinicaltrials':
            params = {'query.term': search, 'pageSize': 100, 'countTotal': 'true', 'format': 'json'}
            if cursor != '*': params['pageToken'] = cursor
            response = self.http.get('https://clinicaltrials.gov/api/v2/studies', params=params)
            response.raise_for_status()
            body = response.json()
            total = body.get('totalCount')
            if ((total is not None and (type(total) is not int or total < 0))
                    or (cursor == '*' and total is None)
                    or not isinstance(body.get('studies', []), list) or total and not body.get('studies')):
                raise ValueError('Invalid ClinicalTrials.gov search response')
            return response, [clinical_record(d) for d in body.get('studies', [])], body.get('nextPageToken'), body.get('totalCount'), None
        if provider == 'osf':
            params = {'cardSearchText': search, 'cardSearchFilter[resourceType]': 'Registration',
                      'page[size]': 100, 'acceptMediatype': 'application/json'}
            if cursor != '*': params['page[cursor]'] = cursor
            response = self.http.get('https://share.osf.io/trove/index-card-search', params=params)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body.get('data'), list): raise ValueError('Invalid OSF search response')
            next_url = body.get('links', {}).get('next')
            next_cursor = None
            if next_url:
                parsed = urlsplit(next_url)
                if parsed.scheme != 'https' or parsed.netloc != 'share.osf.io' or parsed.path != '/trove/index-card-search':
                    raise ValueError('Invalid OSF pagination link')
                next_cursor = parse_qs(parsed.query).get('page[cursor]', [None])[0]
                if not next_cursor: raise ValueError('OSF pagination link has no cursor')
            return response, [osf_record(d) for d in body.get('data', [])], next_cursor, body.get('meta', {}).get('total'), None
        if provider != 'epo': raise ValueError('Unsupported source')
        start = 1 if cursor == '*' else int(cursor)
        if not 1 <= start <= 2000: raise ValueError('OPS result range exceeds 2,000; partition the query')
        end = min(start + 99, 2000)
        response = self.http.get('https://ops.epo.org/3.2/rest-services/published-data/search/biblio',
            params={'q': search, 'Range': f'{start}-{end}'}, headers=self._headers())
        response.raise_for_status()
        rows, total = epo_records(response.content)
        more = bool(rows) and end < total
        limitation = 'OPS 2,000-result limit reached; partition this query' if more and end == 2000 else None
        return response, rows, str(end + 1) if more and not limitation else None, total, limitation

    def fetch(self, work):
        provider, native_id = work['adapter'], work['source_metadata']['native_id']
        if work['id'] != identifier(provider, native_id): raise ValueError('Source identity changed')
        if provider == 'clinicaltrials':
            if not re.fullmatch(r'NCT\d{8}', native_id): raise ValueError('Invalid trial identifier')
            response = self.http.get('https://clinicaltrials.gov/api/v2/studies/' + native_id)
            response.raise_for_status()
            fresh = clinical_record(response.json())
            if fresh['id'] != work['id']: raise ValueError('Retrieved trial identity differs')
            work['source_metadata'] = fresh['source_metadata']
            return (response.content, 'application/json', str(response.request.url)), False
        documents, missing = [], []
        if provider == 'osf':
            if not re.fullmatch(r'[a-z0-9]{5,16}', native_id): raise ValueError('Invalid registration identifier')
            response = self.http.get('https://api.osf.io/v2/registrations/' + native_id + '/')
            response.raise_for_status()
            record = response.json()['data']
            if record['id'] != native_id or record['attributes'].get('public') is not True:
                raise ValueError('OSF registration identity/access mismatch')
            documents.append(_document('registration', response))
            attributes = record['attributes']
            work['source_metadata'].update(date_registered=attributes.get('date_registered'),
                date_modified=attributes.get('date_modified'), withdrawn=attributes.get('withdrawn'),
                record_date=attributes.get('date_modified'),
                registration_supplement=attributes.get('registration_supplement'),
                associated_article_doi=attributes.get('article_doi'))
            schema_id = (record.get('relationships', {}).get('registration_schema', {}).get('data') or {}).get('id')
            if schema_id:
                if not re.fullmatch(r'[a-f0-9]{24}', schema_id): raise ValueError('Invalid OSF schema identifier')
                schema = self.http.get('https://api.osf.io/v2/schemas/registrations/' + schema_id + '/')
                if schema.status_code == 200:
                    if schema.json().get('data', {}).get('id') != schema_id: raise ValueError('OSF schema identity mismatch')
                    documents.append(_document('registration_schema', schema))
                else: missing.append('Registration form schema unavailable: HTTP ' + str(schema.status_code))
        elif provider == 'epo':
            if not re.fullmatch(r'[A-Z]{2}\.[A-Za-z0-9]+\.[A-Z][0-9]?', native_id): raise ValueError('Invalid patent identifier')
            url = 'https://ops.epo.org/3.2/rest-services/published-data/publication/docdb/' + native_id
            for section in ('description', 'claims'):
                response = self.http.get(url + '/' + section, headers=self._headers())
                if response.status_code == 404:
                    missing.append(section + ': not available from OPS')
                    continue
                response.raise_for_status()
                tree = ET.fromstring(response.content)
                expected = native_id.split('.')
                for document in _elements(tree, 'fulltext-document'):
                    if any(document.get(field) and document.get(field) != value
                           for field, value in zip(('country', 'doc-number', 'kind'), expected)):
                        raise ValueError('OPS full-text publication identity differs')
                if not _elements(tree, section):
                    missing.append(section + ': no readable section returned')
                    continue
                documents.append(_document(section, response))
            if not documents: raise ValueError('OPS has no machine-readable description or claims for this publication')
            work['source_metadata']['retrieved_sections'] = [d['section'] for d in documents]
        else:
            raise ValueError('Unsupported source')
        work['source_metadata']['content_limitations'] = missing
        snapshot = {'format': 'canary.source-snapshot', 'version': 1, 'provider': provider,
                    'documents': documents, 'missing': missing}
        return (json.dumps(snapshot, ensure_ascii=False).encode(), 'application/json', work['record_url']), False


def _document(section, response):
    return {'section': section, 'url': str(response.request.url),
            'body_sha256': hashlib.sha256(response.content).hexdigest(), 'body': response.content.decode('utf-8')}


def extract(raw, kind, work):
    if kind != 'application/json': raise ValueError('Unexpected optional source representation')
    data = json.loads(raw)
    heading = f"{LABELS[work['adapter']]} — {work['source_kind']}\n{work['record_url']}"
    blocks = [heading, LIMITATIONS[work['adapter']]]
    if work['adapter'] == 'clinicaltrials':
        blocks += [name + ':\n' + json.dumps(value, ensure_ascii=False, indent=2) for name, value in data.items()]
    else:
        if data.get('format') != 'canary.source-snapshot' or data.get('provider') != work['adapter']:
            raise ValueError('Invalid provider response snapshot')
        for document in data['documents']:
            body = document['body'].encode('utf-8')
            if hashlib.sha256(body).hexdigest() != document['body_sha256']:
                raise ValueError('Provider response hash mismatch')
            if work['adapter'] == 'epo':
                root = ET.fromstring(body)
                sections = _elements(root, document['section'])
                content = '\n\n'.join(ET.tostring(s, encoding='unicode') for s in sections)
            else:
                content = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
            blocks.append(document['section'] + '\n' + document['url'] + '\n' + content)
        blocks += data.get('missing', [])
    return '\n\n'.join(blocks), 0, [], 'json'


def status(workspace, sources, progress):
    from .coverage_runtime import catalog, write
    from .candidate_index import CandidateIndex
    with CandidateIndex(workspace) as index:
        counts = {name: 0 for name in sources}
        for work in index.records():
            if work.get('adapter') in counts: counts[work['adapter']] += 1
    references = catalog(workspace)
    result = []
    for name in sources:
        queries = {q: s for q, s in progress.get('queries', {}).items() if s.get('source') == name}
        failures = [s['failure'] for s in queries.values() if s.get('failure')]
        state = ('not_searched' if not queries else 'complete' if all(s.get('complete') for s in queries.values())
                 else 'failed' if failures and not counts[name] else 'incomplete')
        result.append({'source': name, 'label': LABELS[name], 'status': state, 'query_ids': list(queries),
            'candidates': counts[name], 'retrieved': sum(r.get('adapter') == name for r in references),
            'limitations': LIMITATIONS[name], 'failures': failures})
    write(workspace / 'additional-sources.json', result)
    return result


def _rate_headers(response):
    return {k: v for k, v in response.headers.items()
            if k.lower().startswith(('x-throttling', 'x-individualquota', 'x-registeredquota',
                                     'x-registeredpayingquota'))
            or k.lower() in ('retry-after', 'x-rejection-reason')}


def discover_query(workspace, root, http, index, query, state, pages):
    from .coverage_runtime import append, event, now
    provider = query['source']
    source = Client(http, root)
    seen = set()
    for _ in range(pages):
        cursor = state['cursor']
        if cursor is None: break  # A provider result cap remains explicitly incomplete.
        if cursor in seen:
            state.update(complete=False, truncated=True, failure='Provider repeated a pagination cursor')
            break
        seen.add(cursor)
        started = time.monotonic()
        receipt = {'source': provider, 'query_id': query['id'], 'search': query['search'],
                   'page': state['pages'] + 1, 'requested_at': now(), 'status': None}
        try:
            response, rows, next_cursor, matched, limitation = source.search(provider, query['search'], cursor)
            receipt.update(status=response.status_code, url=str(response.request.url),
                rate_headers=_rate_headers(response))
            if next_cursor == cursor: raise ValueError('Provider repeated a pagination cursor')
            extension = 'xml' if provider == 'epo' else 'json'
            path = workspace / 'search-responses' / f'{query["id"]}-{state["pages"] + 1:04}.{extension}'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(response.content)
            index.add_page(rows, query['id'])
            state.update(pages=state['pages'] + 1, cursor=next_cursor, matched=matched if matched is not None else state.get('matched'),
                         complete=next_cursor is None and not limitation, truncated=bool(next_cursor or limitation))
            state.pop('failure', None)
            if limitation: state['failure'] = limitation
            event(workspace, 'search_page', source=provider, query_id=query['id'], page=state['pages'],
                  candidates=index.count(), complete=state['complete'])
        except (httpx.HTTPError, ValueError, KeyError, TypeError, ET.ParseError) as error:
            code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            # Never persist authorization headers, tokens or response bodies from auth errors.
            reason = f'HTTP {code}' if code is not None else type(error).__name__ + ': ' + str(error)
            receipt.update(status=code, error=reason)
            if isinstance(error, httpx.HTTPStatusError):
                receipt['rate_headers'] = _rate_headers(error.response)
            state.update(complete=False, truncated=True, failure=reason)
            event(workspace, 'search_failed', source=provider, query_id=query['id'], error=reason)
        finally:
            receipt['seconds'] = round(time.monotonic() - started, 3)
            append(workspace / 'additional-source-requests.jsonl', receipt)
        if state.get('complete') or state.get('failure'): break
