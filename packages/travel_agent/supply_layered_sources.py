"""C4 acquisition adapters; structured values never certify official rules.

PlacesEvidence reuses the existing bounded Maps transport. Its tool receipt is
separate from the product-call budget. rendered_source consumes a real visible
browser capture; it does not fetch, authenticate from domains, or relax either
existing supply evidence gate. DNS safety belongs to the acquiring browser.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from . import comparison_maps, material_reader
from .comparison_card_budget import now, private_json
from .supply_evidence import content_backed_type, evidence_fields, _stamp


_FIELDS = {
    'name': ('displayName', 'text'), 'address': ('formattedAddress',),
    'address_components': ('addressComponents',),
    'opening_hours': ('regularOpeningHours',),
    'current_opening_hours': ('currentOpeningHours',),
    'rating': ('rating',), 'rating_count': ('userRatingCount',),
    'price_level': ('priceLevel',), 'website': ('websiteUri',),
}
_FIELD_MASK = ','.join('places.' + name for name in (
    'id', 'displayName', 'formattedAddress', 'addressComponents', 'regularOpeningHours',
    'currentOpeningHours', 'rating', 'userRatingCount', 'priceLevel',
    'websiteUri', 'attributions')) + ',nextPageToken'


def _identifier(prefix, value):
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:20]


def _normalized(value):
    return ' '.join(value.casefold().split()) if isinstance(value, str) else ''


def _redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, '[redacted]') if secret else value
    if isinstance(value, dict):
        return {_redact(key, secret): _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return value


def _public_target(value):
    """Validate before returning a navigation lead; never contact the host."""
    parsed = urlsplit(value) if isinstance(value, str) else None
    if parsed is None:
        raise ValueError('invalid_public_url')
    upgraded = value
    transitions = []
    if parsed.scheme == 'http':
        if parsed.port not in (None, 80) or parsed.username or parsed.password:
            raise ValueError('unsafe_http_upgrade')
        # Strip only a default port; hostname, path, query and credentials are
        # still checked by the existing strict public URL validator.
        netloc = parsed.netloc[:-3] if parsed.netloc.endswith(':80') else parsed.netloc
        upgraded = urlunsplit(('https', netloc, parsed.path, parsed.query, parsed.fragment))
    canonical = material_reader._validate_url(upgraded).geturl()
    if parsed.scheme == 'http':
        transitions.append({'from_url': value, 'to_url': canonical,
                            'kind': 'same_host_https_upgrade'})
    return canonical, transitions


def _destination_evidence(entry, index, destination):
    """Match complete geographic labels, never tokens in a street address."""
    components = entry.get('addressComponents')
    requested = [_normalized(part) for part in re.split(r'[,，]', destination)]
    if not isinstance(components, list) or not requested or not all(requested):
        return []
    candidates = []
    for position, component in enumerate(components):
        if not isinstance(component, dict):
            continue
        types = component.get('types')
        if (not isinstance(types, list) or not all(isinstance(kind, str) for kind in types)
                or not {'locality', 'postal_town', 'country'}.intersection(types)):
            continue
        for key in ('longText', 'shortText'):
            value = component.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append({'value': value, 'component_types': copy.deepcopy(types),
                                   'json_path': f'$.places[{index}].addressComponents[{position}].{key}',
                                   'source_layer': 'structured'})
    proofs = []
    for part in requested:
        proof = next((row for row in candidates if _normalized(row['value']) == part), None)
        if proof is None:
            return []
        proofs.append({**proof, 'requested_component': part})
    return proofs


def _match_entity(data, name, destination):
    entries = data.get('places', []) if isinstance(data, dict) else []
    matches, destination_proofs, assessments = [], {}, []
    forms = [{'value': name, 'basis': 'full_requested_canonical_name'}]
    for index, entry in enumerate(entries if isinstance(entries, list) else []):
        if not isinstance(entry, dict):
            assessments.append({'index': index, 'status': 'rejected', 'reasons': ['invalid_entry']})
            continue
        display = entry.get('displayName')
        actual = display.get('text') if isinstance(display, dict) else None
        destination_match = _destination_evidence(entry, index, destination)
        matching_forms = [form for form in forms if _normalized(actual)
                          and _normalized(actual) == _normalized(form['value'])]
        reasons = []
        if not matching_forms:
            reasons.append('no_exact_explicit_name_match')
        if not destination_match:
            reasons.append('destination_components_not_matched')
        if not isinstance(entry.get('id'), str) or not entry['id'].strip():
            reasons.append('missing_place_id')
        assessments.append({'index': index, 'display_name': actual,
                            'name_evidence': matching_forms,
                            'display_name_json_path': f'$.places[{index}].displayName.text',
                            'destination_component_evidence': destination_match,
                            'status': 'rejected' if reasons else 'eligible', 'reasons': reasons})
        if not reasons:
            matches.append(index)
            destination_proofs[index] = destination_match
    paginated = bool(data.get('nextPageToken')) if isinstance(data, dict) else False
    unique = len(matches) == 1 and not paginated
    for assessment in assessments:
        if assessment['status'] == 'eligible':
            assessment['status'] = 'selected' if unique else 'rejected'
            assessment['reasons'] = ['unique_structured_match'] if unique else [
                'paginated_results' if paginated else 'multiple_exact_structured_matches']
    return {'status': 'matched' if unique else 'unknown',
            'basis': 'exact_explicit_name_form_and_structured_city_or_country_components',
            'requested_name': name, 'explicit_name_forms': forms, 'entry_assessments': assessments,
            'selected_index': matches[0] if unique else None,
            'destination_component_evidence': destination_proofs[matches[0]] if unique else [],
            'matched_count': len(matches), 'paginated': paginated,
            'reason': 'unique_structured_match' if unique else 'entity_not_uniquely_matched',
            'semantic_identity_verified': False}


def _field_evidence(entry, index, source_id, fetched_at):
    values, evidence = {}, {}
    for name, path in _FIELDS.items():
        value = entry
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            continue
        values[name] = copy.deepcopy(value)
        evidence[name] = {'json_path': f'$.places[{index}].' + '.'.join(path),
                          'value': copy.deepcopy(value), 'source_layer': 'structured',
                          'source_id': source_id, 'fetched_at': fetched_at,
                          'factual_status': 'uncorroborated'}
    return values, evidence


def _website_leads(fields, field_evidence, source_id):
    value = fields.get('website')
    if value is None:
        return [], 'missing'
    try:
        target, transitions = _public_target(value)
    except (ValueError, TypeError, UnicodeError):
        return [], 'rejected_unsafe_url'
    return [{'url': target, 'raw_url': value, 'source_id': source_id,
             'json_path': field_evidence['website']['json_path'],
             'source_layer': 'structured', 'url_transitions': transitions,
             'official_identity_verified': False, 'status': 'website_lead_only'}], 'lead_only'


def _structured_result(data, name, destination, fetched_at):
    match = _match_entity(data, name, destination)
    source_id = _identifier('places-source-', json.dumps(data, sort_keys=True, ensure_ascii=False))
    document = {'status': 'unavailable', 'text': '', 'fetched_at': fetched_at,
                'reason': match['reason']}
    sources, leads, fields, proofs = [], [], {}, {}
    website_status = 'entity_unmatched'
    if match['status'] == 'matched':
        index = match['selected_index']
        entry = data['places'][index]
        fields, proofs = _field_evidence(entry, index, source_id, fetched_at)
        # Keep the full response as content so every JSON path refers to this
        # exact retained object, including a match at a nonzero result index.
        content = json.dumps(data, ensure_ascii=False, sort_keys=True)
        document.update(status='read', text=content, content_complete=True)
        leads, website_status = _website_leads(fields, proofs, source_id)
    evidence = evidence_fields(document, source_id=source_id, url=comparison_maps.PLACES_URL)
    row = {**evidence, 'source_layer': 'structured', 'provider': 'Google Places',
           'entity_match': match, 'fields': fields, 'field_evidence': proofs,
           'website_lead_status': website_status,
           'official_rules_verified': False, 'reader_invoked': True}
    if document['status'] == 'read':
        sources.append({**copy.deepcopy(row), 'id': source_id,
                        'title': 'Google Places: ' + name, 'content': document['text'],
                        'source_origin': 'structured_api', 'original_read_status': 'read',
                        'factual_status': 'uncorroborated',
                        'content_scope': 'returned structured response; rules require official evidence'})
    return {'sources': sources, 'ledger': [row], 'website_leads': leads,
            'official_urls': [lead['url'] for lead in leads], 'entity_match': match}


class PlacesEvidence:
    """One name/destination query; no retries, no first-result fallback.

    directory may be a Path or an existing budget with a directory attribute.
    This class never reserves or settles that product budget. Pass an explicit
    key or use the existing GOOGLE_MAPS_SERVER_KEY/GOOGLE_MAPS_API_KEY lookup.
    """

    def __init__(self, directory, *, api_key=None, fetcher=None, timeout=20.0):
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError('Invalid Places timeout')
        self.directory = Path(getattr(directory, 'directory', directory))
        self.api_key = api_key if api_key is not None else comparison_maps.server_key_from_environment()
        self.fetcher, self.timeout = fetcher or comparison_maps._fetch, timeout

    def __call__(self, entity_name, destination, *, request_id):
        for value in (entity_name, destination):
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
                raise ValueError('Places requires an explicit name and destination')
        attempt = uuid4().hex
        path = self.directory / 'layered-tool-ledger' / ('places-' + attempt + '.json')
        receipt = {'attempt_id': attempt, 'request_id': str(request_id), 'operation': 'maps_places',
                   'source_layer': 'structured', 'started_at': now(), 'status': 'not_attempted',
                   'product_call_count': 0, 'tool_call_count': 0, 'artifact_path': str(path)}
        payload = {'textQuery': f'{entity_name.strip()}, {destination.strip()}',
                   'pageSize': 5, 'languageCode': 'en'}
        receipt.update(query=payload, field_mask=_FIELD_MASK)
        private_json(path, receipt)

        def reserve(operation):
            receipt.update(status='unknown', tool_call_count=1, operation=operation)
            private_json(path, receipt)
            return attempt

        def record(metadata):
            receipt['transport'] = _redact(copy.deepcopy(metadata), self.api_key)
            private_json(path, receipt)

        key = self.api_key
        if (not isinstance(key, str) or not 1 <= len(key) <= 512
                or any(char.isspace() or not char.isascii() for char in key)):
            result = {'status': 'preflight_error', 'error_code': 'maps_key_unavailable'}
        else:
            try:
                result = self.fetcher('maps_places', comparison_maps.PLACES_URL, payload,
                                      _FIELD_MASK, api_key=key, reserve=reserve, record=record,
                                      timeout=self.timeout, expected_name=entity_name)
            except Exception as error:
                result = {'status': 'transport_error', 'error_code': type(error).__name__}
        return self._finish(result, receipt, path, entity_name, destination)

    def _finish(self, result, receipt, path, name, destination):
        # Provider errors and returned strings must never persist a credential.
        if not isinstance(result, dict):
            result = {'status': 'response_read_error', 'error_code': 'invalid_places_response'}
        if isinstance(self.api_key, str) and self.api_key:
            result = _redact(result, self.api_key)
        data = result.get('data')
        valid = result.get('status') == 'ok' and isinstance(data, dict)
        output = _structured_result(data if valid else {}, name, destination, now() if valid else None)
        output['ledger'][0]['reader_invoked'] = receipt['tool_call_count'] > 0
        if not valid:
            output['ledger'][0]['reason'] = result.get('error_code') or 'invalid_places_response'
        receipt.update(status=result.get('status', 'invalid_response'), finished_at=now(),
                       response=result, entity_match=output['entity_match'])
        private_json(path, receipt)
        output['ledger'][0]['tool_artifact'] = str(path)
        output['tool_receipt'] = copy.deepcopy(receipt)
        return output


def _capture_urls(capture):
    requested = capture.get('requested_url') or capture.get('url')
    start, transitions = _public_target(requested)
    final, final_upgrades = _public_target(capture.get('url'))
    # A browser capture must actually be HTTPS, rather than relabelling an HTTP
    # response as if an upgrade had already occurred.
    if final_upgrades:
        raise ValueError('capture_not_https')
    chain = capture.get('redirect_chain') or []
    if not isinstance(chain, list) or len(chain) > material_reader.MAX_REDIRECTS:
        raise ValueError('invalid_redirect_chain')
    cursor = start
    for hop in chain:
        if (not isinstance(hop, dict) or hop.get('observed') is not True
                or hop.get('http_status') not in (301, 302, 303, 307, 308)):
            raise ValueError('unobserved_redirect')
        origin, _ = _public_target(hop.get('from_url'))
        target, _ = _public_target(hop.get('to_url'))
        if origin != cursor or urlsplit(hop['to_url']).scheme != 'https':
            raise ValueError('invalid_redirect_target')
        transitions.append({**copy.deepcopy(hop), 'kind': 'observed_public_redirect'})
        cursor = target
    if chain and cursor != final:
        raise ValueError('redirect_final_mismatch')
    if not chain and urlsplit(start).hostname != urlsplit(final).hostname:
        raise ValueError('cross_host_redirect_unproven')
    if not chain and start != final:
        raise ValueError('redirect_chain_required')
    return final, transitions


def _exact_type_proof(document, text):
    proof = document.get('source_provenance', {}).get(
        'source_type_evidence', document.get('source_type_evidence'))
    quote = proof if isinstance(proof, str) else None
    if isinstance(proof, dict):
        quote = next((proof[key] for key in ('quote', 'direct_quote', 'excerpt')
                      if isinstance(proof.get(key), str)), None)
    return bool(isinstance(quote, str) and quote and quote in text
                and content_backed_type(document, text))


def rendered_source(capture):
    """Convert a real DOM capture to {source, ledger}; denied reads retain evidence.

    Required: url, title, text, timezone-aware fetched_at. Type evidence is an
    exact body quote, optionally with the existing content/body fingerprints.
    source_type must be an explicit reader assessment; matching a quote alone
    establishes traceability, not the publisher's identity or official status.
    Cross-host redirects require actual observed 3xx hops, never domain guesses.
    """
    if not isinstance(capture, dict):
        raise ValueError('Rendered capture must be an object')
    document = copy.deepcopy(capture)
    text = document.get('text')
    text = text if isinstance(text, str) else ''
    provenance = document.get('source_provenance')
    document['source_provenance'] = provenance if isinstance(provenance, dict) else {}
    declared = document['source_provenance'].get('source_type', document.get('source_type', 'unknown'))
    document['source_type'] = declared
    reason, transitions, url = None, [], document.get('url')
    try:
        url, transitions = _capture_urls(document)
    except (ValueError, TypeError, UnicodeError):
        reason = 'unsafe_or_unproven_capture_url'
    if document.get('status', 'read') != 'read' or document.get('blocked') is True:
        reason = reason or document.get('reason') or 'browser_read_unavailable'
    if material_reader._GATE.search(text):
        reason = reason or 'login_challenge_or_shell'
    if not text.strip():
        reason = reason or 'visible_text_missing'
    if _stamp(document.get('fetched_at')) is None:
        reason = reason or 'capture_timestamp_missing_or_invalid'
    qualified = _exact_type_proof(document, text)
    if not qualified:
        document['source_type'] = 'unknown'
        document['source_provenance']['source_type'] = 'unknown'
    document.update(status='unavailable' if reason else 'read', text=text, reason=reason)
    source_id = _identifier('rendered-source-', str(url) + '\n' + text)
    evidence = evidence_fields(document, source_id=source_id, url=url)
    ledger = {**evidence, 'requested_url': capture.get('requested_url') or capture.get('url'),
              'source_layer': 'official_rendered', 'source_origin': 'rendered_browser',
              'reader_invoked': True, 'declared_source_type': declared,
              'url_transitions': transitions, 'source_type_quote_exact': qualified,
              'field_evidence': {name: {'capture_path': path, 'source_layer': 'official_rendered',
                                       'source_id': source_id}
                                 for name, path in [('content', '$.text'), ('url', '$.url'),
                                                    ('title', '$.title'), ('retrieved_at', '$.fetched_at'),
                                                    ('source_type_evidence',
                                                     '$.source_provenance.source_type_evidence'
                                                     if 'source_type_evidence' in document['source_provenance']
                                                     else '$.source_type_evidence')]}}
    source = None
    if not reason:
        source = {**copy.deepcopy(ledger), 'id': source_id, 'title': document.get('title'),
                  'content': text, 'original_read_status': 'read',
                  'source_provenance': copy.deepcopy(document['source_provenance']),
                  'factual_status': 'uncorroborated',
                  'content_scope': 'captured visible browser text; not an exhaustive source inventory'}
    return {'source': source, 'ledger': ledger}
