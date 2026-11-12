"""Read-only replay of retained candidate responses; never dispatch provider calls.

Historical pre-witness schema outputs remain expected rejections. Current-schema
outputs must traverse binding, unchanged evidence floor, and C1 export. Raw
artifacts are never amended or exported as live products.
"""
import argparse
import hashlib
import json
import re
import socket
import sys
from pathlib import Path
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.travel_agent import supply_candidates as sc
from packages.travel_agent.supply_evidence_floor import evidence_floor, apply_evidence_floor


def replay(root):
    rows = []
    for batch, sub in [('c4-paste-rerun-2026-10-26', 'product-evidence'),
                       ('c4-schema-2026-10-26', 'live-evidence')]:
        base = root / batch / sub
        enriched_path = base / 'cases/singapore_parents/enriched.json'
        enriched = json.loads(enriched_path.read_text())
        for path in sorted((base / 'model-artifacts').glob('*candidates*response.json')):
            receipt = json.loads(path.read_text())
            body = json.loads(receipt['body_utf8'])
            for tool in body['choices'][0]['message']['tool_calls']:
                raw = json.loads(tool['function']['arguments'])
                row = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'enriched_sha256': hashlib.sha256(enriched_path.read_bytes()).hexdigest(),
                       'expected_legacy_rejection': batch.startswith('c4-paste'), 'bindings': []}
                request_path = path.with_name(path.name.replace('-response.json', '-request.json'))
                row['request_sha256'] = hashlib.sha256(request_path.read_bytes()).hexdigest()
                row['raw_arguments_sha256'] = hashlib.sha256(tool['function']['arguments'].encode()).hexdigest()
                # Enumerate independently so an early failing claim cannot hide later ones.
                sources = {s['id']: s['content'] for s in enriched['sources']}
                dates = set(re.findall(r'\d{4}-\d{2}-\d{2}', json.dumps(enriched.get('dates'))))
                for candidate in raw['candidates']:
                    for index, claim in enumerate(candidate['claims']):
                        check = {'candidate': candidate['id'], 'claim': index, 'errors': []}
                        for witness in claim.get('source_witnesses', []) + claim.get('date_exception_witnesses', []):
                            original = sources.get(witness['source_id'], '')
                            if witness['quote'] not in original:
                                check.setdefault('nonliteral_quotes', []).append({
                                    'source_id': witness['source_id'],
                                    'whitespace_equivalent': ' '.join(witness['quote'].split()) in ' '.join(original.split())})
                        try:
                            sc._validate_claim_binding(sc.CandidateClaim.model_validate(claim), sources, dates)
                        except ValueError as error:
                            check['errors'].append(str(error))
                        row['bindings'].append(check)
                try:
                    parsed = sc.validate_candidate_sources(enriched, raw)
                    assert evidence_floor(enriched)['status'] == 'qualified'
                    projected, audit = apply_evidence_floor(enriched, parsed.model_dump())
                    exported = sc.to_comparison_seed(enriched, projected)
                    assert len(exported['options']) == 2
                    row.update(chain='PASS', downgraded=audit['downgraded_claim_count'],
                               exported_options=len(exported['options']), semantic_truth_verified=False)
                except (ValueError, AssertionError) as error:
                    row.update(chain='REJECT', error=str(error))
                    if isinstance(error, ValidationError):
                        row['schema_errors'] = [{'type': item['type'], 'loc': item['loc']}
                                                for item in error.errors()]
                row['pass'] = (row['chain'] == 'REJECT' and bool(row.get('schema_errors'))
                               and all(item['type'] in {'missing', 'extra_forbidden'}
                                       for item in row['schema_errors'])
                               if row['expected_legacy_rejection'] else row['chain'] == 'PASS')
                rows.append(row)
    assert len(rows) == 3, 'Retained output inventory changed; reconcile before live dispatch'
    return {'pass': all(r['pass'] for r in rows), 'rows': rows, 'product_calls': 0,
            'note': 'Offline replay only. Legacy rejection is required, not an accepted candidate.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--research-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    def forbidden(*args, **kwargs):
        raise AssertionError('Network forbidden in offline replay')
    socket.socket.connect = forbidden
    result = replay(args.research_root)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({'pass': result['pass'], 'outputs': len(result['rows']), 'claims': sum(len(r['bindings']) for r in result['rows'])}))
    sys.exit(0 if result['pass'] else 1)
