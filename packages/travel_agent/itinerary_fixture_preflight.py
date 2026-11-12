"""Read-only fixture accounting before a run; never infer choice semantics.

Existing cards store choice effects as prose. Valid IDs do not prove that a
plan implements that prose: unresolved mappings are retained per scoped item.
Engineering selections may describe a future revision, not an applied choice.
Freshness and price checks reuse the existing helpers on private copies.
"""
from __future__ import annotations

import copy
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .itinerary_assembly import project_confirmed_trip_context
from .itinerary_price_projection import project_prices
from .itinerary_supply import _day, _freshness, load_itinerary_supply
from .revision_plan import plan_sha256
from .validation import safe_url


def _finding(findings, reason, path, evidence, *, status='conflict'):
    findings.append(dict(classification='fixture', status=status, reason=reason,
                         path=path, evidence=copy.deepcopy(evidence)))


def _items(plan):
    return [(f'/plan/days/{di}/items/{ii}', day, item)
            for di, day in enumerate(plan['days'])
            for ii, item in enumerate(day['items'])]


def _plan_check(plan, findings):
    if not isinstance(plan, dict) or not isinstance(plan.get('days'), list):
        _finding(findings, 'plan_days_missing', '/plan/days', dict(value=plan))
        return False
    day_ids, item_ids = set(), set()
    for di, day in enumerate(plan['days']):
        path = f'/plan/days/{di}'
        if (not isinstance(day, dict) or type(day.get('day')) is not int
                or day['day'] < 1 or not isinstance(day.get('items'), list)):
            _finding(findings, 'invalid_plan_day', path, dict(value=day))
            return False
        if day['day'] in day_ids:
            _finding(findings, 'duplicate_day', path + '/day', dict(day=day['day']))
        day_ids.add(day['day'])
        for ii, item in enumerate(day['items']):
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']:
                _finding(findings, 'invalid_plan_item', path + f'/items/{ii}', dict(value=item))
                return False
            if item['id'] in item_ids:
                _finding(findings, 'duplicate_item', path + f'/items/{ii}/id', dict(id=item['id']))
            item_ids.add(item['id'])
    return True


def _blueprint_check(blueprint, bundle, plan, findings, checks):
    if blueprint is None:
        checks.append(dict(check='blueprint_snapshot', status='not_supplied'))
        return
    if not isinstance(blueprint, dict):
        _finding(findings, 'invalid_blueprint', '/blueprint', dict(value=blueprint))
        return
    expected = [dict(day=day['day'], date=day.get('date'), title=day.get('title'),
                     item_ids=[item['id'] for item in day['items']]) for day in plan['days']]
    actual = blueprint.get('current_plan', {}).get('days')
    if actual != expected:
        _finding(findings, 'blueprint_plan_snapshot_mismatch', '/blueprint/current_plan/days',
                 dict(expected=expected, actual=actual))
    intent = bundle.get('intent_card', {})
    for target, source in (('assumptions', 'assumptions'), ('plan', 'provisional_plan'),
                           ('questions', 'questions')):
        actual = blueprint.get('provisional', {}).get(target)
        if actual != intent.get(source, []):
            _finding(findings, 'blueprint_intent_snapshot_mismatch', '/blueprint/provisional/' + target,
                     dict(expected=intent.get(source, []), actual=actual))
    checks.append(dict(check='blueprint_snapshot', compared='days/assumptions/plan/questions'))


def _question_index(bundle, findings):
    questions = bundle.get('intent_card', {}).get('questions', [])
    result = {}
    if not isinstance(questions, list):
        _finding(findings, 'invalid_questions', '/bundle/intent_card/questions', dict(value=questions))
        return result
    for index, question in enumerate(questions):
        path = f'/bundle/intent_card/questions/{index}'
        if not isinstance(question, dict) or not isinstance(question.get('id'), str):
            _finding(findings, 'invalid_question', path, dict(value=question))
            continue
        key = question['id']
        if key in result:
            _finding(findings, 'duplicate_question_id', path, dict(id=key))
        choices = question.get('choices', [])
        if not isinstance(choices, list) or any(not isinstance(row, dict) for row in choices):
            _finding(findings, 'invalid_choices', path + '/choices', dict(value=choices))
            continue
        ids = [row.get('id') for row in choices]
        if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
            _finding(findings, 'invalid_choice_ids', path + '/choices', dict(ids=ids))
            continue
        result[key] = (path, question, dict(zip(ids, choices)))
        default = question.get('default_choice_id')
        if default is not None and default not in ids:
            _finding(findings, 'unknown_default_choice', path + '/default_choice_id', dict(value=default))
    return result


def _selection_rows(fixture, questions, findings):
    provenance = fixture.get('provenance', {})
    selected = provenance.get('engineering_selections', [])
    if not isinstance(selected, list):
        _finding(findings, 'invalid_selections', '/provenance/engineering_selections', dict(value=selected))
        return []
    rows, seen = [], set()
    for index, selection in enumerate(selected):
        path = f'/provenance/engineering_selections/{index}'
        if not isinstance(selection, dict):
            _finding(findings, 'invalid_selection', path, dict(value=selection))
            continue
        question_id = selection.get('question_id')
        if not isinstance(question_id, str) or question_id not in questions:
            _finding(findings, 'unknown_selected_question', path, dict(selection=selection))
            continue
        if question_id in seen:
            _finding(findings, 'duplicate_selected_question', path, dict(selection=selection))
        seen.add(question_id)
        rows.append((path, selection, 'engineering_selection_application_unknown'))
    for key, (path, question, _) in questions.items():
        if key not in seen and question.get('default_choice_id') is not None:
            rows.append((path + '/default_choice_id',
                         dict(question_id=key, choice_id=question['default_choice_id']), 'default_assumption'))
    return rows


def _selection_check(fixture, bundle, plan, findings, checks):
    questions = _question_index(bundle, findings)
    scope = fixture.get('provenance', {}).get('scope_day_numbers', [])
    if not isinstance(scope, list) or any(type(day) is not int for day in scope):
        _finding(findings, 'invalid_selection_scope', '/provenance/scope_day_numbers', dict(value=scope))
        scope = []
    known_days = {day['day'] for day in plan['days']}
    if set(scope) - known_days:
        _finding(findings, 'absent_selection_scope_day', '/provenance/scope_day_numbers',
                 dict(absent=sorted(set(scope) - known_days)))
    candidates = [(path, day, item) for path, day, item in _items(plan) if day['day'] in scope]
    for path, selection, application in _selection_rows(fixture, questions, findings):
        qpath, question, choices = questions[selection['question_id']]
        choice = choices.get(selection.get('choice_id'))
        if choice is None:
            _finding(findings, 'unknown_selected_choice', path, dict(selection=selection))
            continue
        evidence = dict(selection=selection, selection_path=path, application_status=application,
                        question_path=qpath, choice=choice, default_choice_id=question.get('default_choice_id'),
                        default_assumption=question.get('default_assumption'), scope_day_numbers=scope,
                        note='Choice text has no structured applied effects; no semantic verdict inferred.')
        checks.append(dict(check='selection_reference', status='valid', **copy.deepcopy(evidence)))
        if not candidates:
            _finding(findings, 'selection_plan_mapping_unresolved', path, evidence, status='unresolved')
        for item_path, day, item in candidates:
            item_evidence = dict(evidence, day=day['day'], item=copy.deepcopy(item))
            _finding(findings, 'selection_plan_mapping_unresolved', item_path, item_evidence, status='unresolved')
    assumptions = bundle.get('intent_card', {}).get('assumptions', [])
    if assumptions and not questions:
        _finding(findings, 'assumption_plan_mapping_unresolved', '/bundle/intent_card/assumptions',
                 dict(assumptions=assumptions), status='unresolved')


def _comparison_check(bundle, findings, checks):
    card = bundle.get('comparison_card', {})
    options = card.get('options', [])
    if not isinstance(options, list) or any(not isinstance(row, dict) for row in options):
        _finding(findings, 'invalid_comparison_options', '/bundle/comparison_card/options', dict(value=options))
        return
    ids = [row.get('id') for row in options]
    if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
        _finding(findings, 'invalid_comparison_option_ids', '/bundle/comparison_card/options', dict(ids=ids))
    recommendation = card.get('recommendation', {}).get('option_id')
    if recommendation is not None and recommendation not in ids:
        _finding(findings, 'unknown_recommended_option', '/bundle/comparison_card/recommendation',
                 dict(option_id=recommendation))
    selection = bundle.get('comparison_selection')
    if selection is not None:
        _finding(findings, 'comparison_selection_mapping_unresolved', '/bundle/comparison_selection',
                 dict(selection=selection, option_ids=ids), status='unresolved')
    checks.append(dict(check='comparison_reference', selection_recorded=selection is not None,
                       recommendation_is_selection=False))


def _supply(fixture, bundle, findings):
    supplied = fixture.get('supply_input')
    if supplied:
        try:
            imported = load_itinerary_supply(supplied['path'], destination=supplied['destination'],
                as_of=supplied['as_of'], structured_seeds=supplied.get('structured_seeds'))
        except (KeyError, TypeError, ValueError, OSError) as exc:
            _finding(findings, 'supply_import_invalid', '/supply_input', dict(error=str(exc)))
            return [], None
        return imported['supply_seeds'], imported['destination']
    return bundle.get('supply_seeds', []), bundle.get('destination')


def _supply_reference_check(seeds, plan, findings, checks):
    catalog = plan.get('supplies', [])
    catalog = catalog if isinstance(catalog, list) else []
    for path, day, item in _items(plan):
        reference = item.get('supply_id')
        if reference is None or reference == '':
            continue
        seed_matches = [i for i, seed in enumerate(seeds) if seed.get('id') == reference]
        legacy_matches = [(i, row) for i, row in enumerate(catalog)
                          if isinstance(row, dict) and row.get('id') == reference]
        evidence = dict(item_id=item['id'], day=day['day'], supply_id=reference,
                        seed_paths=[f'/supply_seeds/{i}' for i in seed_matches],
                        legacy_paths=[f'/plan/supplies/{i}' for i, _ in legacy_matches])
        if isinstance(reference, str) and len(seed_matches) == 1:
            checks.append(dict(check='supply_reference', status='seed_reference',
                               path=path + '/supply_id', **evidence))
        elif (isinstance(reference, str) and not seed_matches and len(legacy_matches) == 1
              and safe_url(legacy_matches[0][1].get('source_url'))):
            checks.append(dict(check='supply_reference', status='legacy_catalog_reference',
                path=path + '/supply_id', **evidence,
                source_url=legacy_matches[0][1]['source_url'],
                note='Source reference only; not a paid seed or evidence of trip-date freshness.'))
        else:
            _finding(findings, 'supply_reference_unresolved', path + '/supply_id',
                dict(evidence, reason='No unique seed or legacy catalog entry with valid source provenance.'),
                status='unresolved')


def _freshness_check(seeds, destination, bundle, plan, findings, checks):
    if not seeds:
        checks.append(dict(check='seed_trip_freshness', status='no_seeds'))
        return
    try:
        zone = ZoneInfo((destination or {})['timezone'])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        _finding(findings, 'seed_timezone_unknown', '/bundle/destination/timezone',
                 dict(destination=destination), status='unresolved')
        return
    rule_refs = {}
    for rule in bundle.get('price_rules', []):
        if isinstance(rule, dict) and rule.get('seed_id'):
            rule_refs.setdefault(rule['seed_id'], set()).update(rule.get('item_ids', []))
    for index, seed in enumerate(seeds):
        path = f'/supply_seeds/{index}'
        refs = rule_refs.get(seed.get('id'), set())
        used = [day for day in plan['days'] if any(item.get('supply_id') == seed.get('id')
                    or item['id'] in refs for item in day['items'])]
        days = used or plan['days']
        statuses = [dict(day=day['day'], date=day.get('date'),
            status=_freshness(seed, parsed, zone) if (parsed := _day(day.get('date'), zone)) else 'trip_date_unknown')
            for day in days]
        evidence = dict(seed_id=seed.get('id'), checked_at=seed.get('checked_at'),
            fresh_until=seed.get('fresh_until'), binding='explicit_item_or_price_rule' if used else 'all_trip_days_envelope',
            days=statuses, helper='itinerary_supply._freshness')
        checks.append(dict(check='seed_trip_freshness', **evidence))
        bad = [row for row in statuses if row['status'] != 'current']
        if bad:
            conflict = bool(used) and all(row['status'] not in ('unknown', 'trip_date_unknown') for row in bad)
            _finding(findings, 'seed_trip_window_not_covered' if conflict else 'seed_trip_coverage_unresolved',
                     path, dict(evidence, uncovered=bad), status='conflict' if conflict else 'unresolved')


def _price_check(bundle, seeds, plan, findings, checks):
    rules = bundle.get('price_rules', [])
    if not rules:
        for path, _, item in _items(plan):
            if isinstance(item.get('price'), dict) and item['price'].get('status') == 'priced':
                _finding(findings, 'price_without_frozen_binding', path + '/price', dict(item_id=item['id']))
        checks.append(dict(check='price_binding', status='no_rules'))
        return
    try:
        projected = project_prices([], seeds, rules=rules, as_of=bundle.get('price_as_of'),
                                   plan=plan, binding_plan=bundle.get('notes_plan'))
    except (KeyError, TypeError, ValueError) as exc:
        _finding(findings, 'price_configuration_invalid', '/bundle/price_rules', dict(error=str(exc)))
        return
    for index, (rule, row) in enumerate(zip(rules, projected['budget']['items'])):
        checks.append(dict(check='price_binding', rule_id=rule['id'], disposition=row['disposition'],
                           reason=row['reason'], item_ids=rule['item_ids']))
        if rule['disposition'] == 'priced' and row['disposition'] != 'priced':
            _finding(findings, 'price_binding_invalid', f'/bundle/price_rules/{index}',
                     dict(rule=rule, projected_disposition=row['disposition'], reason=row['reason']))
    for path, _, item in _items(plan):
        actual = item.get('price')
        if not isinstance(actual, dict) or actual.get('status') != 'priced':
            continue
        expected = projected['item_prices'].get(item['id'])
        if actual != expected:
            _finding(findings, 'price_identity_or_value_mismatch', path + '/price',
                     dict(item_id=item['id'], actual=actual, expected=expected))


def inspect_fixture(fixture, *, blueprint=None):
    """Inspect frozen input without repairing it or calling a model.

    ``passed`` means no mechanical conflict *or unresolved coverage* was found;
    it never means semantic acceptance. Paths refer to the fixture except
    ``/blueprint`` and imported ``/supply_seeds``. Findings contain raw evidence.
    """
    value = copy.deepcopy(fixture)
    findings, checks = [], []
    if not isinstance(value, dict) or not isinstance(value.get('bundle'), dict):
        _finding(findings, 'invalid_fixture', '/', dict(value=value))
        return dict(passed=False, findings=findings, checks=checks)
    bundle, plan = value['bundle'], value.get('plan')
    if not _plan_check(plan, findings):
        return dict(passed=False, findings=findings, checks=checks)
    baseline = bundle.get('notes_plan')
    if baseline is not None and baseline != plan:
        _finding(findings, 'notes_plan_binding_mismatch', '/bundle/notes_plan',
                 dict(plan_sha256=plan_sha256(plan), notes_plan_sha256=plan_sha256(baseline)))
    try:
        plan = project_confirmed_trip_context(plan, bundle.get('confirmed_trip_context'))
    except (KeyError, TypeError, ValueError) as exc:
        _finding(findings, 'confirmed_trip_context_conflict', '/bundle/confirmed_trip_context', dict(error=str(exc)))
    _blueprint_check(blueprint, bundle, plan, findings, checks)
    _selection_check(value, bundle, plan, findings, checks)
    _comparison_check(bundle, findings, checks)
    seeds, destination = _supply(value, bundle, findings)
    if not isinstance(seeds, list) or any(not isinstance(seed, dict) for seed in seeds):
        _finding(findings, 'invalid_supply_seeds', '/bundle/supply_seeds', dict(value=seeds))
        seeds = []
    _supply_reference_check(seeds, plan, findings, checks)
    _freshness_check(seeds, destination, bundle, plan, findings, checks)
    _price_check(bundle, seeds, plan, findings, checks)
    return dict(passed=not findings, findings=findings, checks=checks,
                coverage=dict(plan_days=len(plan['days']), plan_items=len(_items(plan)), seeds=len(seeds)),
                semantic_evaluation='not_performed', product_calls=0)
