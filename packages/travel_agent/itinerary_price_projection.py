"""Pure price projection from frozen rules and verified structured source offers.

Model budget notes are never monetary authority. The caller preserves raw notes;
the frozen rules define the complete output budget and traveler eligibility.
Budget ``amount`` is a unit amount; ``line_amount`` and frontend ``price.amount``
are the whole line total. This module neither researches nor executes purchases.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
import math
import re
from urllib.parse import urlsplit


DISPOSITIONS = frozenset(('priced', 'unpriced', 'self_booked'))
MAX_SAFE_CENTS = 2 ** 53 - 1
ACTIVITY_IDENTITY_FIELDS = ('id', 'supply_id', 'title', 'activity_kind')
INVALIDATED_QUOTE_REASON = '活动身份或原报价绑定已变更，旧报价失效；须重新核验并显式绑定。'


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not _text(value) or not re.match(r'\d{4}-\d{2}-\d{2}(?:$|[T ])', value):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except ValueError:
        return None


def price_is_complete(price):
    """Conservative producer check for the unchanged frontend price contract."""
    if not isinstance(price, dict):
        return False
    amount = price.get('amount')
    return (price.get('status') == 'priced' and type(amount) in (int, float)
            and math.isfinite(amount) and amount >= 0
            and bool(re.fullmatch(r'[A-Z]{3}', _text(price.get('currency'))))
            and bool(_text(price.get('source_name')))
            and bool(_text(price.get('source_url')))
            and isinstance(price.get('quoted_at'), str) and _day(price['quoted_at']) is not None
            and isinstance(price.get('breakdown'), list)
            and all(isinstance(part, str) for part in price['breakdown']))


def _index(rows, label):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(label + ' must be a list of objects')
    ids = [_text(row.get('id')) for row in rows]
    if any(not key for key in ids) or len(set(ids)) != len(ids):
        raise ValueError(label + ' IDs must be nonempty and unique')
    return dict(zip(ids, rows))


def _validate_rules(rules):
    _index(rules, 'price rules')
    if not rules:
        raise ValueError('price rules must define at least one budget item')
    for rule in rules:
        if (rule.get('disposition') not in DISPOSITIONS or not _text(rule.get('label'))
                or not _text(rule.get('reason')) or type(rule.get('quantity')) is not int
                or rule['quantity'] < 1):
            raise ValueError('each price rule requires disposition, label, reason and positive quantity')
        ids = rule.get('item_ids')
        if (not isinstance(ids, list) or any(not _text(key) for key in ids)
                or len(ids) != len(set(ids))):
            raise ValueError('price rule item_ids must be unique nonempty strings')


def _plan_counts(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get('days'), list):
        raise ValueError('plan must contain days')
    counts = Counter()
    for day in plan['days']:
        if not isinstance(day, dict) or not isinstance(day.get('items'), list):
            raise ValueError('plan days must contain items')
        for item in day['items']:
            if not isinstance(item, dict) or not _text(item.get('id')):
                raise ValueError('plan items require nonempty IDs')
            counts[item['id']] += 1
    return counts


def _activity_identity(item):
    """Literal identity bookkeeping; never classify the meaning of an edit."""
    return tuple(item.get(field) for field in ACTIVITY_IDENTITY_FIELDS)


def _invalidated_price(previous=None):
    previous = previous if isinstance(previous, dict) else {}
    # Retain historical source provenance so selective undo need not introduce
    # a URL that disappeared when the quote became non-payable.
    return dict(status='unpriced', amount=None, currency='',
                breakdown=[INVALIDATED_QUOTE_REASON],
                **{key: previous.get(key, '') for key in ('source_name', 'source_url', 'quoted_at')})


def invalidate_changed_quotes(previous, candidate):
    """Invalidate inherited quotes before C3 computes its owned, undoable diff."""
    old = {item['id']: item for day in previous['days'] for item in day['items']}
    for day in candidate['days']:
        for item in day['items']:
            prior = old.get(item['id'])
            price = item.get('price')
            if (prior is not None and _activity_identity(prior) != _activity_identity(item)
                    and isinstance(price, dict) and price.get('status') == 'priced'):
                item['price'] = _invalidated_price(price)


def _stale_quote_items(plan, binding_plan):
    if binding_plan is None:
        return {}
    old_counts = _plan_counts(binding_plan)
    old = {item['id']: item for day in binding_plan['days'] for item in day['items']}
    current = {item['id']: item for day in plan['days'] for item in day['items']}
    stale = set(old) ^ set(current)
    for key in set(old) & set(current):
        # A later edit cannot revive an invalidated quote merely by restoring
        # its title/supply. Undo restores the recorded price delta explicitly.
        quote_lost = (price_is_complete(old[key].get('price'))
                      and not price_is_complete(current[key].get('price')))
        if (old_counts[key] != 1 or quote_lost
                or _activity_identity(old[key]) != _activity_identity(current[key])):
            stale.add(key)
    return {key: old.get(key, {}).get('price') for key in stale}


def _seed_failure(seed, as_of):
    if not seed:
        return '所选供给种子缺失，不能绑定价格。'
    if seed.get('grade') != 'S1' or seed.get('verification_status') != 'verified':
        return '供给未满足 S1 已核验条件，不能绑定价格。'
    checked, fresh = _day(seed.get('checked_at')), _day(seed.get('fresh_until'))
    if checked is None or fresh is None or checked > fresh:
        return '供给核验日期或新鲜期缺失、无效，不能绑定价格。'
    if checked > as_of:
        return '供给核验日期晚于本次投影时间，不能绑定价格。'
    if fresh < as_of:
        return '供给种子已过期，需重新核验后绑定价格。'
    if not all(_text(seed.get(key)) for key in ('source_name', 'source_url', 'quote')):
        return '供给缺少来源名称、官方 URL 或逐字引文，不能绑定价格。'
    try:
        url = urlsplit(seed['source_url'])
        valid_url = url.scheme in ('https', 'http') and bool(url.hostname)
    except ValueError:
        valid_url = False
    return '' if valid_url else '供给来源 URL 无效，不能绑定价格。'


def _offer_price(rule, seed):
    offers = seed.get('offers')
    matches = [offer for offer in offers if isinstance(offer, dict)
               and offer.get('id') == rule.get('offer_id')] if isinstance(offers, list) else []
    if len(matches) != 1:
        return None, '缺少唯一已核验结构化报价，不能从正文反推金额。'
    offer = matches[0]
    if not _text(rule.get('audience')) or offer.get('audience') != rule['audience']:
        return None, '报价适用人群与冻结旅客资格不符，不能绑定价格。'
    amount, currency = offer.get('unit_amount'), offer.get('currency')
    if (not isinstance(amount, str) or not re.fullmatch(r'(?:0|[1-9]\d*)(?:\.\d{1,2})?', amount)
            or not re.fullmatch(r'[A-Z]{3}', _text(currency)) or not _text(offer.get('product'))):
        return None, '结构化报价的单价、币种或产品无效，不能绑定价格。'
    unit = Decimal(amount)
    line = unit * rule['quantity']
    if line * 100 > MAX_SAFE_CENTS or Decimal(str(float(line))) != line:
        return None, '行合计不能安全表示为前端金额，不能绑定价格。'
    expression = f'{unit:.2f} × {rule["quantity"]} = {line:.2f} {currency}'
    price = dict(status='priced', amount=float(line), currency=currency,
                 breakdown=[f'{offer["product"]}：{expression}'],
                 source_name=seed['source_name'], source_url=seed['source_url'],
                 quoted_at=seed['checked_at'])
    if not price_is_complete(price):
        return None, '报价未满足前端完整价格契约，不能绑定价格。'
    return (dict(unit_amount=float(unit), line_amount=float(line), price=price), '')


def _binding_price(rule, seed, as_of, plan_counts, binding_counts, stale_items):
    ids = rule['item_ids']
    if set(ids).intersection(stale_items):
        return None, INVALIDATED_QUOTE_REASON
    if len(ids) != 1 or plan_counts[ids[0]] != 1 or binding_counts[ids[0]] != 1:
        return None, '只允许唯一预算项与唯一现存行程条目一对一绑定；不分摊或重复投影。'
    failure = _seed_failure(seed, as_of)
    if failure:
        return None, failure
    return _offer_price(rule, seed)


def _unknown(rule, text, suffix):
    return dict(id=f'price-{suffix}-{rule["id"]}', text=text,
                impact='预算完整性与价格适用条件', recheck_when='选择或来源条件更新时重新核验',
                item_ids=list(rule['item_ids']))


def _project_row(rule, seed, as_of, plan_counts, binding_counts, stale_items):
    disposition, reason = rule['disposition'], rule['reason']
    priced = None
    stale = disposition == 'priced' and bool(set(rule['item_ids']).intersection(stale_items))
    if disposition == 'priced':
        priced, failure = _binding_price(rule, seed, as_of, plan_counts, binding_counts, stale_items)
        if failure:
            disposition, reason = 'unpriced', reason + '；' + failure
    price = priced['price'] if priced else dict(status=disposition, amount=None,
        currency='', breakdown=[reason], source_name='', source_url='', quoted_at='')
    if stale:
        price = _invalidated_price(stale_items.get(rule['item_ids'][0]))
    row = dict(id=rule['id'], label=rule['label'], disposition=disposition, reason=reason,
               quantity=rule['quantity'], item_ids=list(rule['item_ids']), basis=reason,
               amount=priced['unit_amount'] if priced else None,
               line_amount=priced['line_amount'] if priced else None, currency=price['currency'],
               source_ids=[seed['id']] if priced else [],
               binding_status='stale' if stale else 'current', price=price)
    disposition_row = dict(id=rule['id'], label=rule['label'], disposition=disposition,
        reason=reason, item_ids=list(rule['item_ids']), quantity=rule['quantity'],
        projected_item_ids=list(rule['item_ids']) if priced else [])
    if priced:
        disposition_row.update(seed_id=seed['id'], offer_id=rule['offer_id'])
    unknowns = [_unknown(rule, reason, 'unknown')] if disposition == 'unpriced' else []
    if _text(rule.get('assumption')):
        unknowns.append(_unknown(rule, rule['assumption'], 'assumption'))
    return row, disposition_row, unknowns


def _budget_summary(rows):
    totals = {}
    unknown = sum(row['disposition'] == 'unpriced' for row in rows)
    for row in rows:
        if row['disposition'] != 'priced':
            continue
        currency = row['currency']
        subtotal = totals.setdefault(currency, dict(currency=currency,
            known_subtotal=Decimal(0), known_count=0, unknown_count=0))
        subtotal['known_subtotal'] += Decimal(str(row['line_amount']))
        subtotal['known_count'] += 1
    for subtotal in totals.values():
        subtotal['known_subtotal'] = float(subtotal['known_subtotal'])
        subtotal['label'] = '已知小计' if unknown else '已列项目合计'
    return dict(items=rows, currencies=list(totals.values()), unknown_count=unknown,
                status='unknown' if unknown else 'accounted',
                scope='仅覆盖冻结裁决所列项目；自订无金额项目不计入合计')


def project_prices(budget_items, seeds, *, rules, as_of, plan, binding_plan=None):
    """Normalize every frozen rule, then project complete one-to-one prices.

    ``budget_items`` accepts the model's raw budget for interface compatibility,
    but none of its amount/basis/quantity/source fields can override frozen rules.
    Ineligible/missing/expired price evidence yields explicit unpriced reasons.
    Invalid rule configuration raises ValueError and cannot emit partial prices.
    Revision callers supply the frozen ``binding_plan`` so stable IDs alone
    cannot transfer an old activity's offer or revive an invalidated quote.
    """
    if not isinstance(budget_items, list) or any(not isinstance(row, dict) for row in budget_items):
        raise ValueError('budget_items must be a list of objects')
    _validate_rules(rules)
    sources, plan_counts = _index(seeds, 'supply seeds'), _plan_counts(plan)
    stale_items = _stale_quote_items(plan, binding_plan)
    projection_day = _day(as_of)
    if projection_day is None:
        raise ValueError('as_of must be a valid ISO date or timestamp')
    binding_counts = Counter(item_id for rule in rules if rule['disposition'] == 'priced'
                             for item_id in rule['item_ids'])
    rows, dispositions, item_prices, unknowns = [], [], {}, []
    for rule in rules:
        row, disposition, row_unknowns = _project_row(rule, sources.get(rule.get('seed_id')),
            projection_day, plan_counts, binding_counts, stale_items)
        rows.append(row)
        dispositions.append(disposition)
        unknowns.extend(row_unknowns)
        if price_is_complete(row['price']):
            item_prices[rule['item_ids'][0]] = dict(row['price'], breakdown=list(row['price']['breakdown']))
    return dict(budget=_budget_summary(rows), dispositions=dispositions,
                item_prices=item_prices, unknowns=unknowns)
