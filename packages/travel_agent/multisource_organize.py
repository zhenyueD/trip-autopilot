"""Opt-in admission of local, document-bound staging into the real organizer.

No acquisition, model invocation or browser-provided trust fields. The local
manifest is operator-owned evidence, not an upload format or a trust certificate.
"""
import copy
import hashlib
import json
import os
from pathlib import Path

from . import source_registry
from .source_adapters import (SourceDocument, RecordedExtraction, TransportAuthorityAdapter,
                              HotelOfficialAdapter, TripAdvisorForumAdapter, GoogleMapsReviewAdapter)
from .source_adapters.google_maps_review import review_documents

ADAPTERS = {'transport_authority': TransportAuthorityAdapter, 'hotel_official': HotelOfficialAdapter,
            'tripadvisor_forum': TripAdvisorForumAdapter, 'google_maps_review': GoogleMapsReviewAdapter}
MAX_MANIFEST_BYTES = 2_000_000


def _admit(entry, materials):
    source = entry.get('adapter')
    definition = next((s for s in source_registry.SOURCE_REGISTRY if s.source_id == source), None)
    if source not in ADAPTERS or not definition or not definition.allowed_acquisition_methods:
        raise ValueError('source_not_allowed')
    method = entry.get('acquisition_method')
    if method not in definition.allowed_acquisition_methods:
        raise ValueError('acquisition_method_not_allowed')
    document = SourceDocument(**entry['document'])
    digest = hashlib.sha256(document.content.encode()).hexdigest()
    if document.fingerprint() != entry.get('document_fingerprint') or digest != entry.get('content_sha256'):
        raise ValueError('document_binding_mismatch')
    matches = [m for m in materials if m.get('kind') in ('document_extracted', 'user_text', 'web_link')
               and m.get('status') == 'read' and m.get('content') == document.content]
    if not matches:
        raise ValueError('matching_user_material_required')
    if source == 'tripadvisor_forum' and not any(m['kind'] in ('document_extracted', 'user_text') for m in matches):
        raise ValueError('user_shared_content_required')
    if source == 'google_maps_review':
        # A web excerpt cannot claim to have taken the permitted Places path.
        documents = review_documents(entry.get('places_payload'), captured_at=document.captured_at)
        if document.fingerprint() not in {d.fingerprint() for d in documents}:
            raise ValueError('places_document_binding_mismatch')
    if not isinstance(entry.get('extraction_origin'), str) or not entry['extraction_origin'].strip():
        raise ValueError('extraction_origin_required')
    replay = RecordedExtraction({document.fingerprint(): entry['extraction']})
    report = ADAPTERS[source](replay).adapt_with_report(document)
    return report, document, matches[0]['id']


def admit_staging(context, usage):
    """Return unchanged context while off; record every enabled rejection reason."""
    if os.environ.get('MULTISOURCE_ORGANIZE_ENABLED', '').strip().lower() not in ('1', 'true'):
        return context
    audit = usage['multisource_organize'] = {'accepted': 0, 'entries': [], 'status': 'enabled'}
    try:
        path = os.environ.get('MULTISOURCE_STAGING_PATH', '')
        if not path:
            raise ValueError('staging_path_not_configured')
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError('staging_manifest_too_large')
        manifest = json.loads(raw)
        entries = manifest.get('entries')
        if manifest.get('version') != 1 or not isinstance(entries, list) or len(entries) > 100:
            raise ValueError('invalid_staging_manifest')
    except (OSError, ValueError, TypeError, AttributeError) as error:
        audit.update(status='unavailable', reason=str(error) if isinstance(error, ValueError) else type(error).__name__)
        return context
    result = copy.deepcopy(context)
    supplies = result.setdefault('previous_supplies', [])
    evidence = result.setdefault('previous_evidence', [])
    ids = {s['id']: s for s in supplies}
    for index, entry in enumerate(entries):
        record = {'index': index, 'status': 'rejected'}
        audit['entries'].append(record)
        try:
            if not isinstance(entry, dict):
                raise ValueError('invalid_staging_entry')
            report, document, material_id = _admit(entry, result.get('materials', []))
            record.update(reason=report.reason, issues=report.issues, candidate_ids=[])
            for candidate in report.candidates:
                if candidate['id'] in ids:
                    record['reason'] = 'candidate_id_already_present'
                    continue
                supplies.append(candidate)
                ids[candidate['id']] = candidate
                record['candidate_ids'].append(candidate['id'])
                audit['accepted'] += 1
                evidence.append({'id': 'staging-' + candidate['id'], 'method': 'multisource_staging',
                    'status': 'source_stated_unverified', 'source_url': document.url,
                    'supply_ids': [candidate['id']], 'material_id': material_id,
                    'text': document.content, 'verified_fields': [], 'untrusted_external_content': True,
                    'document_fingerprint': document.fingerprint(), 'acquisition_method': entry['acquisition_method'],
                    'extraction_origin': entry['extraction_origin'],
                    'source_provenance': copy.deepcopy(candidate['source_provenance'])})
            if record['candidate_ids']:
                record['status'] = 'accepted'
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            record['reason'] = str(error) if isinstance(error, ValueError) else 'invalid_staging_entry'
    return result


def restrict_staging_reads(tools, context, usage):
    """Do not turn an offline candidate's reference into acquisition authority."""
    if 'multisource_organize' not in usage:
        return tools
    from .capability_tools import CapabilityTools
    protected = [s for s in context.get('previous_supplies', []) if s.get('source_provenance')]
    protected_ids = {s['id'] for s in protected}

    class StagingTools(CapabilityTools):
        def _inspect(self, action, supplies, evidence=None):
            # Resolve exactly the same effective URL as the underlying capability;
            # another ordinary candidate may contain an alias of a staged source.
            url = action.get('url')
            if url is None:
                item = next((s for s in supplies if s['id'] == action.get('supply_id')), {})
                product = item.get('product_url')
                url = product if isinstance(product, str) and product.startswith('https://') else item.get('source_url')
            keys = _protected_references(protected + supplies,
                                         context.get('previous_evidence', []) + list(evidence or []))
            if action.get('supply_id') in protected_ids or _reference_key(url) in keys:
                return self._blocked('staging_acquisition_disabled',
                                     '此来源仅使用已获得材料；未授权额外采集，内容仍未核实。')
            return super()._inspect(action, supplies, evidence)

    return StagingTools(tools.researcher, catalog=tools.catalog,
                        destinations=tools.destinations, place_hints=tools.place_hints)


def _reference_key(url):
    from urllib.parse import urlsplit, unquote, parse_qsl, urlencode
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower().rstrip('.')
        if parts.scheme.lower() not in ('http', 'https') or not host:
            return None
        host = host.removeprefix('www.')
        port = parts.port
        query = sorted((k, v) for k, v in parse_qsl(parts.query)
                       if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid'))
        return (host, port if port not in (80, 443) else None,
                unquote(parts.path or '/').rstrip('/'), urlencode(query))
    except ValueError:
        return None


def _protected_references(supplies, evidence):
    """One canonical reference/known-redirect closure for both acquisition gates."""
    from .capability_tools import _sources
    references = set(_sources([s for s in supplies if s.get('source_provenance')]))
    protected = {_reference_key(url) for url in references} - {None}
    # Alias closure comes from existing receipts, never from a new HTTP request.
    chains = [[row.get(k) for k in ('source_url', 'url', 'requested_url')]
              + list(row.get('redirect_chain') or []) for row in evidence if isinstance(row, dict)]
    changed = True
    while changed:
        before = len(protected)
        for chain in chains:
            keys = {_reference_key(url) for url in chain} - {None}
            if protected & keys:
                protected.update(keys)
        changed = len(protected) != before
    return protected


def restrict_staging_reader(reader, supplies, evidence, usage):
    """Keep preparation from acquiring admitted references, including known aliases."""
    if ('multisource_organize' not in usage and
            os.environ.get('MULTISOURCE_ORGANIZE_ENABLED', '').strip().lower() not in ('1', 'true')):
        return reader
    protected = _protected_references(supplies, evidence)

    def bounded_reader(url):
        if _reference_key(url) in protected:
            return {'url': url, 'status': 'blocked', 'method': 'multisource_staging',
                    'reason': 'staging_acquisition_disabled', 'verified_fields': [],
                    'text': '', 'message': '仅使用已获得材料；未授权额外读取，来源仍未核实。'}
        return reader(url)
    return bounded_reader
