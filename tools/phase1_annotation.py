#!/usr/bin/env python3
"""Offline human annotation packets. No server, model call, DB write or implicit freeze."""
from copy import deepcopy
from datetime import datetime
import argparse
import hashlib
import json
import zlib
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.travel_agent.uncertainty_evaluation import (
    RISK_LEVELS, _claim_judgments, fingerprint, freeze_cases, validate_real_case,
)

LABEL_FIELDS = {'required_conditions', 'must_check', 'expected', 'reason', 'expected_action',
                'error_consequences', 'outcome', 'claim_judgments', 'annotation'}
OUTCOMES = {'restaurant_l2_complete', 'no_data', 'no_match', 'abandoned', 'safety_stop'}
SEED = 'rfc001-2026-10-11'


def verified_file(root, relative, sha256):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('evidence path must be a file inside snapshot root')
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != sha256:
        raise ValueError('evidence hash changed')
    return body


def validate_candidate(case, root):
    for key in ('id', 'task_id', 'family', 'source_family', 'entity_id', 'observed_at'):
        if not isinstance(case.get(key), str) or not case[key].strip():
            raise ValueError('candidate missing identity, source family or observation')
    observed = datetime.fromisoformat(case['observed_at'])
    if observed.tzinfo is None:
        raise ValueError('observation must have timezone')
    conditions = case.get('request_conditions', {})
    if not all(conditions.get(k) for k in ('branch_id', 'date', 'time', 'timezone')):
        raise ValueError('candidate missing request conditions')
    if type(conditions.get('party_size')) is not int or conditions['party_size'] < 1:
        raise ValueError('party size must be a positive integer')
    if case['entity_id'] != conditions['branch_id']:
        raise ValueError('entity id must match the requested branch')
    verified_file(root, case['snapshot_path'], case['snapshot_sha256'])
    response_identity = _verify_receipts(case, root)
    if case.get('kind') == 'real_source_probe':
        if case.get('main_corpus_eligible') is not False:
            raise ValueError('source probes cannot enter main corpus')
    elif case.get('kind') == 'real':
        origin = case.get('task_origin')
        if origin not in {'real_user_task', 'real_channel_probe_approved'} or case.get('main_corpus_eligible') is not True:
            raise ValueError('real user task provenance required')
        if origin == 'real_user_task':
            verified_file(root, case['user_request_snapshot_path'], case['user_request_snapshot_sha256'])
        elif case.get('cohort') != origin or case.get('metadata', {}).get('accepted_by_user') is not True:
            raise ValueError('approved real channel probe provenance required; not a traveler outcome')
        if origin == 'real_channel_probe_approved' and any(not isinstance(case.get('metadata', {}).get(k), str)
                or not case['metadata'][k].strip() for k in ('provider_family', 'merchant_source_family')):
            raise ValueError('provider and merchant source family must be declared separately')
    else:
        raise ValueError('unsupported candidate kind')
    return response_identity


def _verify_receipts(case, root):
    matched = False
    primary = None
    for ref in case.get('receipts', []):
        receipt = json.loads(verified_file(root, ref['path'], ref['sha256']))
        if receipt.get('response_path'):
            verified_file(root, receipt['response_path'], receipt['sha256'])
        if receipt.get('response_path') == case['snapshot_path']:
            matched = (receipt.get('method') == 'GET' and receipt.get('http_status') == 200
                       and receipt.get('truncated') is False
                       and receipt.get('sha256') == case['snapshot_sha256']
                       and receipt.get('observed_at') == case['observed_at'])
            if matched:
                _verify_request_binding(case['request_conditions'], receipt)
                primary = receipt
    if not matched:
        raise ValueError('original snapshot needs a matching successful request receipt')
    return fingerprint({k: primary.get(k) for k in (
        'method', 'requested_url', 'observed_at', 'sha256', 'request_conditions')})


def _verify_request_binding(conditions, receipt):
    if receipt.get('request_conditions') == conditions:
        return
    url = urlsplit(receipt.get('requested_url', ''))
    if url.hostname != 'www.sevenrooms.com' or url.path != '/api-yoa/availability/widget/range':
        raise ValueError('actual request conditions must be bound in receipt')
    params = parse_qs(url.query)
    expected = {'venue': conditions['branch_id'], 'time_slot': conditions['time'],
                'party_size': str(conditions['party_size']),
                'start_date': datetime.fromisoformat(conditions['date']).strftime('%m-%d-%Y'),
                'num_days': '1'}
    if any(params.get(k) != [v] for k, v in expected.items()):
        raise ValueError('actual request parameters differ from case conditions')


def grouped_cases(cases):
    """Merchant source and entity groups stay together; a shared host is not a merchant."""
    remaining, result = deepcopy(cases), []
    while remaining:
        group = [remaining.pop(0)]
        changed = True
        while changed:
            sources = {_merchant_family(c) for c in group}
            entities = {c['entity_id'] for c in group}
            linked = [c for c in remaining if _merchant_family(c) in sources or c['entity_id'] in entities]
            changed = bool(linked)
            group.extend(linked)
            remaining = [c for c in remaining if c not in linked]
        family = 'group-' + fingerprint(sorted({_merchant_family(c) for c in group}) +
                                        sorted({c['entity_id'] for c in group}))[:20]
        result.extend({**c, 'family': family} for c in group)
    return result


def _merchant_family(case):
    return case.get('metadata', {}).get('merchant_source_family') or case['source_family']


def make_packet(cases, root, split):
    if split not in {'dev', 'held_out'} or not cases:
        raise ValueError('nonempty candidates and valid split required')
    for field in ('id', 'task_id'):
        if len({c[field] for c in cases}) != len(cases):
            raise ValueError('duplicate case or task id')
    if len({(c['snapshot_path'], c['snapshot_sha256']) for c in cases}) != len(cases):
        raise ValueError('one captured response cannot become multiple cases')
    response_ids = [validate_candidate(case, root) for case in cases]
    if len(set(response_ids)) != len(response_ids):
        raise ValueError('one actual response cannot become multiple cases through copied paths')
    cases = grouped_cases(cases)
    families = sorted({c['family'] for c in cases}, key=lambda x: fingerprint([SEED, x]))
    dev, count, target = set(), 0, round(len(cases) * .6)
    for family in families[:-1] if len(families) > 1 else families:
        if count >= target:
            break
        dev.add(family)
        count += sum(c['family'] == family for c in cases)
    selected = [c for c in cases if ('dev' if c['family'] in dev else 'held_out') == split]
    clean = [{**c, **dict.fromkeys(LABEL_FIELDS),
              'ai_suggestion': c.get('ai_suggestion') if split == 'dev' else None} for c in selected]
    packet = {'version': 1, 'split': split, 'seed': SEED, 'frozen': False,
              'source_case_count': len(cases), 'family_count': len(families),
              'group_split_ready': len(families) > 1, 'cases': clean}
    packet['packet_sha256'] = fingerprint(packet)
    return packet


def _immutable(case):
    return {k: v for k, v in case.items() if k not in LABEL_FIELDS}


def _validate_label(case, split, implementation_actors):
    annotation = case.get('annotation') or {}
    if (not isinstance(annotation.get('actor'), str) or not annotation['actor'].strip()
            or annotation['actor'] in implementation_actors
            or annotation.get('kind') != 'human'):
        raise ValueError('independent human annotator required')
    for key in ('confirmed_independent', 'confirmed_read_original', 'confirmed_each_case'):
        if annotation.get(key) is not True:
            raise ValueError('each case requires explicit human declarations')
    try:
        timestamp = datetime.fromisoformat(annotation.get('at', ''))
    except (TypeError, ValueError):
        raise ValueError('human annotation timestamp is invalid') from None
    if timestamp.tzinfo is None:
        raise ValueError('human annotation timestamp with timezone required')
    if split == 'held_out' and (annotation.get('saw_ai_suggestion') is not False
                                or annotation.get('saw_system_prediction') is not False):
        raise ValueError('held-out cases require blind human judgment')
    if case.get('ai_suggestion') is not None and annotation.get('saw_ai_suggestion') is not True:
        raise ValueError('provided AI suggestions must be disclosed')
    conditions = case.get('required_conditions')
    if not isinstance(conditions, list) or not conditions or any(
            not isinstance(c, dict) or not isinstance(c.get('id'), str) or not c['id'].strip()
            or c.get('risk') not in RISK_LEVELS for c in conditions):
        raise ValueError('must-check conditions need unique ids and risk levels')
    ids = [c['id'] for c in conditions]
    if len(set(ids)) != len(ids) or case.get('must_check') != ids:
        raise ValueError('must_check must match required_conditions in order')
    if case.get('expected') not in {'verified', 'conditional', 'unknown'}:
        raise ValueError('expected must be verified, conditional or unknown')
    for key in ('reason', 'expected_action', 'error_consequences'):
        if not isinstance(case.get(key), str) or not case[key].strip():
            raise ValueError('reason, expected action and error consequences required')
    if case.get('outcome') is not None and case['outcome'] not in OUTCOMES:
        raise ValueError('invalid outcome; leave unobserved outcome null')
    _claim_judgments(case.get('claim_judgments'))


def validate_labels(labeled, original, root, implementation_actors):
    if not implementation_actors:
        raise ValueError('all implementation actors must be supplied')
    original_hash = fingerprint({k: v for k, v in original.items() if k != 'packet_sha256'})
    if original.get('packet_sha256') != original_hash:
        raise ValueError('original packet hash changed')
    if {k: v for k, v in labeled.items() if k != 'cases'} != {
            k: v for k, v in original.items() if k != 'cases'}:
        raise ValueError('packet metadata changed')
    if len(labeled['cases']) != len(original['cases']) or not labeled['cases']:
        raise ValueError('all packet cases must be finalized; empty packets cannot enter corpus')
    for case, base in zip(labeled['cases'], original['cases']):
        if _immutable(case) != _immutable(base):
            raise ValueError('candidate metadata changed')
        validate_candidate(case, root)
        _validate_label(case, original['split'], implementation_actors)
    result = deepcopy(labeled['cases'])
    for case in result:
        case['annotation']['split'] = original['split']
        case['annotation']['split_plan_sha256'] = original.get('split_plan_sha256')
        case['annotation']['packet_sha256'] = original['packet_sha256']
    return result


def validate_freeze_ready(cases, root, implementation_actors, plan=None, *, evaluation_scope='task_outcome'):
    if not implementation_actors:
        raise ValueError('all implementation actors must be supplied')
    if evaluation_scope not in {'task_outcome', 'claim_calibration'}:
        raise ValueError('unsupported evaluation scope')
    if not 200 <= len(cases) <= 300:
        raise ValueError('200-300 independently human labeled real cases required')
    if not plan or fingerprint({k: v for k, v in plan.items() if k != 'sha256'}) != plan.get('sha256'):
        raise ValueError('original complete split plan with verified hash required')
    entries = {entry['id']: entry for entry in plan['cases']}
    if len(cases) != len(entries) or {c['id'] for c in cases} != set(entries):
        raise ValueError('final cases must exactly match every preassigned case; no omissions or additions')
    if plan.get('seed') != SEED:
        raise ValueError('frozen split seed changed')
    for case in cases:
        validate_candidate(case, root)
        if case['kind'] != 'real' or (evaluation_scope == 'task_outcome' and case.get('outcome') is None):
            raise ValueError('probe or unobserved task cannot enter the main frozen corpus')
        if evaluation_scope == 'claim_calibration' and (
                case.get('cohort') != 'real_channel_probe_approved'
                or case.get('task_origin') != 'real_channel_probe_approved'
                or case.get('metadata', {}).get('accepted_by_user') is not True):
            raise ValueError('claim calibration freeze is only for the approved real channel cohort')
        entry = entries[case['id']]
        label = case.get('annotation') or {}
        if (fingerprint(_immutable(case)) != entry['base_case_sha256']
                or label.get('split') != entry['split'] or label.get('split_plan_sha256') != plan['sha256']):
            raise ValueError('case content, family, split or original plan changed before freeze')
        _validate_label(case, entry['split'], implementation_actors)
        validate_real_case(case, root)
    if _expected_splits(cases) != {e['id']: e['split'] for e in plan['cases']}:
        raise ValueError('existing freeze would change the preassigned split')
    return True


def freeze_calibration(cases, root, output, plan, *, annotator, implementation_actors):
    validate_freeze_ready(cases, root, implementation_actors, plan,
                          evaluation_scope='claim_calibration')
    if (not annotator or annotator in implementation_actors
            or any(c['annotation']['actor'] != annotator for c in cases)):
        raise ValueError('all cases need the declared independent human annotator')
    entries = {c['id']: c for c in plan['cases']}
    manifest = {'protocol_version': 'rfc001-claim-calibration-v1',
        'evaluation_scope': 'claim_calibration', 'cohort': 'real_channel_probe_approved',
        'annotator': annotator, 'seed': SEED, 'case_count': len(cases),
        'original_split_plan_sha256': plan['sha256'],
        'cases': [{'id': c['id'], 'task_id': c['task_id'], 'family': c['family'],
                   'sha256': fingerprint(c), 'split': entries[c['id']]['split']} for c in cases],
        'task_metrics_eligible': False, 'traveler_completion_rate': None,
        'outcome_policy': 'Unobserved outcomes remain null. Channel query calibration is not traveler task completion, booking, or L3 evidence.',
        'policy': 'Frozen labels and split are immutable; no held-out tuning. Any changed cohort or revealed labels need a new version.'}
    manifest['sha256'] = fingerprint(manifest)
    write_private(output, json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def _expected_splits(cases):
    families = sorted({c['family'] for c in cases}, key=lambda x: fingerprint([SEED, x]))
    if len(families) < 2:
        raise ValueError('need independent merchant/entity families for dev and held-out sets')
    dev, count = set(), 0
    for family in families[:-1]:
        if count >= round(len(cases) * .6):
            break
        dev.add(family)
        count += sum(c['family'] == family for c in cases)
    return {c['id']: 'dev' if c['family'] in dev else 'held_out' for c in cases}


def display_text(body, content_encoding=None):
    if content_encoding == 'gzip' or body.startswith(b'\x1f\x8b'):
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        body = decoder.decompress(body, 5 * 1024 * 1024 + 1)
        if len(body) > 5 * 1024 * 1024 or not decoder.eof or decoder.unused_data:
            raise ValueError('gzip evidence is incomplete or exceeds safe display bound')
    return body.decode('utf-8', errors='replace')


def render_html(packet, root):
    evidence = {}
    for c in packet['cases']:
        items = [{'title': '原始来源响应', 'text': display_text(verified_file(root, c['snapshot_path'],
                 c['snapshot_sha256']))}]
        for ref in c['receipts']:
            body = verified_file(root, ref['path'], ref['sha256'])
            receipt = json.loads(body)
            items.append({'title': ref['path'], 'text': body.decode('utf-8')})
            if receipt.get('response_path') and receipt['response_path'] != c['snapshot_path']:
                items.append({'title': receipt['response_path'], 'text': display_text(verified_file(
                    root, receipt['response_path'], receipt['sha256']), receipt.get('content_encoding'))})
        evidence[c['id']] = items
    encoded = json.dumps({'packet': packet, 'evidence': evidence}, ensure_ascii=False)
    for char, escape in [('<', '\\u003c'), ('>', '\\u003e'), ('&', '\\u0026')]:
        encoded = encoded.replace(char, escape)
    template = Path(__file__).resolve().parents[1] / 'research/phase1-closeout-2026-10-11/annotation/template.html'
    return template.read_text().replace('DATA_PLACEHOLDER', encoded)


def write_private(path, body):
    with path.open('x', encoding='utf-8') as handle:
        handle.write(body)
    path.chmod(0o600)


def prepare_packets(cases, root, output, batch_size=10):
    if not 1 <= batch_size <= 20:
        raise ValueError('review packet batch size must be between 1 and 20')
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    index = {'candidate_count': len(cases), 'labels': 0, 'frozen': False, 'packets': []}
    wholes = {split: make_packet(cases, root, split) for split in ('dev', 'held_out')}
    plan = {'version': 1, 'seed': SEED, 'case_count': len(cases), 'cases': [
        {'id': c['id'], 'task_id': c['task_id'], 'family': c['family'], 'split': split,
         'base_case_sha256': fingerprint(_immutable(c))}
        for split, whole in wholes.items() for c in whole['cases']]}
    plan['sha256'] = fingerprint(plan)
    write_private(output / 'split-plan.json', json.dumps(plan, ensure_ascii=False, indent=2))
    index['split_plan_sha256'] = plan['sha256']
    for split in ('dev', 'held_out'):
        whole = wholes[split]
        batches = max(1, (len(whole['cases']) + batch_size - 1) // batch_size)
        for batch in range(batches):
            name = split if batches == 1 else f'{split}-{batch + 1:03d}'
            packet = {**whole, 'cases': whole['cases'][batch * batch_size:(batch + 1) * batch_size],
                      'whole_packet_sha256': whole['packet_sha256'], 'batch_index': batch,
                      'split_plan_sha256': plan['sha256']}
            packet['packet_sha256'] = fingerprint({k: v for k, v in packet.items() if k != 'packet_sha256'})
            write_private(output / (name + '.packet.json'), json.dumps(packet, ensure_ascii=False, indent=2))
            write_private(output / (name + '.html'), render_html(packet, root))
            index['packets'].append({'name': name, 'count': len(packet['cases']),
                'split': split, 'packet_sha256': packet['packet_sha256'],
                'case_ids': [c['id'] for c in packet['cases']]})
    index['sha256'] = fingerprint(index)
    write_private(output / 'index.json', json.dumps(index, ensure_ascii=False, indent=2))
    links = '\n'.join(f'<li><a href="{p["name"]}.html">{p["name"]}</a> — {p["count"]} cases</li>'
                      for p in index['packets'])
    write_private(output / 'index.html', '<!doctype html><meta charset="utf-8"><title>标注包</title>'
        '<h1>逐条标注包</h1><p>未标不计完成；锁定答案请独立保管。</p><ul>' + links + '</ul>')
    return index


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'admit', 'freeze'])
    parser.add_argument('--cases', required=True, type=Path)
    parser.add_argument('--snapshot-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--packet', type=Path)
    parser.add_argument('--implementation-actor', action='append')
    parser.add_argument('--batch-size', type=int, default=10)
    parser.add_argument('--split-plan', type=Path)
    parser.add_argument('--annotator')
    parser.add_argument('--evaluation-scope', choices=['task_outcome', 'claim_calibration'], default='task_outcome')
    args = parser.parse_args(argv)
    value = json.loads(args.cases.read_text())
    if args.command == 'prepare':
        prepare_packets(value, args.snapshot_root, args.output, args.batch_size)
        print('Prepared offline unlabeled packets; no human labels or frozen corpus created.')
        return
    if args.command == 'freeze':
        if not args.split_plan or not args.annotator:
            parser.error('freeze requires original --split-plan and --annotator')
        plan = json.loads(args.split_plan.read_text())
        if args.evaluation_scope == 'claim_calibration':
            freeze_calibration(value, args.snapshot_root, args.output, plan,
                annotator=args.annotator, implementation_actors=args.implementation_actor)
        else:
            validate_freeze_ready(value, args.snapshot_root, args.implementation_actor, plan)
            freeze_cases(value, args.snapshot_root, args.output, annotator=args.annotator,
                         implementation_actors=args.implementation_actor, seed=SEED)
            args.output.chmod(0o600)
        print(json.dumps({'frozen': True, 'case_count': len(value), 'evaluation_scope': args.evaluation_scope}))
        return
    if not args.packet or not sys.stdin.isatty():
        parser.error('admit requires original --packet and the human operator local terminal')
    if input('本人逐条核对原始证据并确认这些标签由本人定稿。请输入 HUMAN REVIEWED: ') != 'HUMAN REVIEWED':
        parser.error('human confirmation not received')
    cases = validate_labels(value, json.loads(args.packet.read_text()), args.snapshot_root,
                            args.implementation_actor)
    write_private(args.output, json.dumps(cases, ensure_ascii=False, indent=2))
    print(json.dumps({'admitted': len(cases), 'sha256': fingerprint(cases), 'frozen': False}))


def main(argv=None):
    try:
        return _main(argv)
    except (ValueError, TypeError, KeyError, OSError):
        print('Annotation operation rejected. Check fields and evidence privately; no label values are logged.',
              file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
