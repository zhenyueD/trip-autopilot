"""Reading order only; original prose and structured payload remain in records."""
import re
from copy import deepcopy
from decimal import Decimal
from .single_session_context import pending_conditions


def sections(prose):
    """Reorder explicit sections, preserving unrecognized prose verbatim."""
    groups = {key: [] for key in ('opening', 'todo', 'money', 'corrections', 'records')}
    title, current, block = '', 'opening', []
    for line in prose.splitlines():
        if line.startswith('# ') and not title:
            title = line
            continue
        heading = re.match(r'^##\s+(.+)', line)
        if heading:
            groups[current].extend(block)
            block = []
            name = heading[1]
            if re.search(r'待办|行前准备|出行前|出发前准备|失败出路|应急方案', name):
                current = 'todo'
            elif re.search(r'价格|费用|预算|花费|^钱', name):
                current = 'money'
            elif re.search(r'说法|纠正|误区|出发前先核', name):
                current = 'corrections'
            elif re.search(r'核查记录|来源附录|修订回执', name):
                current = 'records'
            else:
                current = 'opening'
            # Keep original subheadings, but the reading order has one h2 per part.
            line = '#' + line
        block.append(line)
    groups[current].extend(block)
    return title, {key: '\n'.join(value).strip() for key, value in groups.items()}


def render(prose, data, inline, markdown, days, overview, budget, pending):
    title, parts = sections(prose)
    parts['todo'] = re.sub(r'^###[^\n]*(?:待办|应急|失败出路)[^\n]*\n?', '', parts['todo'], flags=re.M)
    budget_html = budget(data.get('overview'), inline)
    if data.get('overview'):
        for key in ('money', 'opening'):
            parts[key] = re.sub(r'^### 预算分档[^\n]*\n.*?(?=^### [^#]|\Z)', '', parts[key], flags=re.M | re.S)
    result = [markdown(title), '<section class="trip-decision"><h2>这趟怎么定下来的</h2>',
              markdown(parts['opening']), overview(data.get('overview'), inline),
              '<p>预算分档详见「钱：价格与预算」。</p>' if budget_html else '', '</section>',
              '<section class="trip-todo"><h2>出发前准备与应急方案</h2>', markdown(parts['todo'])]
    result.append('<section class="predeparture-confirmation"><h3>出发前确认</h3>')
    result.append('<p>未核验项目尚不能作为确定承诺；按下列条件核实后再预订，完整明细保留在核查记录。</p>')
    conditions = pending_conditions(data.get('overview'))
    result.extend('<p><strong>' + inline(condition) + '</strong></p>' for condition in conditions)
    gaps = list(dict.fromkeys(pending(data)))
    if gaps:
        result += ['<p><strong>出发前由出行人核实以下路段；未确认前不锁定衔接，无法落实则调整当日安排。</strong></p>',
                   '<div class="traffic-confirmations">' + ''.join('<p>' + inline(gap) + '</p>' for gap in gaps) + '</div>']
    if not parts['todo'] and not gaps:
        result.append('<p>未列出额外待办；不代表已完成预订或办理。</p>')
    result += ['</section></section>', '<section class="trip-days"><h2>逐日安排</h2>', days(data, inline), '</section>',
               '<section class="trip-money"><h2>钱：价格与预算</h2>', markdown(parts['money']),
               budget_html, '</section>',
               '<section class="trip-corrections"><h2>出发前先核这几条</h2>', markdown(parts['corrections'])]
    if not parts['corrections']:
        result.append('<p>尚未提供有依据的网上说法纠正。</p>')
    result.append('</section>')
    return '\n'.join(result)


def accounting_view(prose, data, ledger, display_totals=None):
    """Apply explicit document accounting; never infer applicability from prose."""
    data = deepcopy(data)
    if data.get('overview') and re.search(r'^#{2,6} 预算分档\s*$', prose, re.M):
        data['budget_authority'] = 'overview'
    flags = data.get('price_applicability', {})
    if not isinstance(flags, dict):
        raise ValueError('price_applicability must map evidence IDs to applicability')
    for eid, status in flags.items():
        if eid not in ledger['evidence'] or ledger['evidence'][eid]['kind'] != 'price':
            raise ValueError('price_applicability requires existing price evidence')
        if status not in ('applicable', 'reference-only'):
            raise ValueError('unsupported price applicability')
    excluded = {eid for eid, status in flags.items() if status == 'reference-only'}
    replacements = data.get('presentation_replacements', [])
    for replacement in replacements:
        old, new = replacement['old'], replacement['new']
        if not old or prose.count(old) != 1:
            raise ValueError('presentation replacement must match exactly once')
        prose = prose.replace(old, new, 1)
    def affected(text):
        return any(eid in excluded for eid in re.findall(r'\{\{price:(E[1-9]\d*)\}\}', text))
    tiers = (data.get('overview') or {}).get('budget_tiers', [])
    affected_tiers = {index for index, tier in enumerate(tiers) if affected(str(tier))}
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('price_applicability', 'presentation_replacements'):
                    continue
                if key == 'price_or_timetable' and isinstance(item, str) and affected(item):
                    value[key] = re.sub(r'\{\{price:(E[1-9]\d*)\}\}',
                        lambda m: '当季不适用（参考价格见表）' if m[1] in excluded else m[0], item)
                elif key in ('verified_items', 'estimated_items', 'missing') and isinstance(item, list):
                    value[key] = [re.sub(r'\{\{price:(E[1-9]\d*)\}\}',
                        lambda m: '（不计入小计）' if m[1] in excluded else m[0], text)
                        for text in item if not affected(text) or any(
                            eid not in excluded for eid in re.findall(r'\{\{price:(E[1-9]\d*)\}\}', text))]
                elif isinstance(item, str) and affected(item):
                    value[key] = re.sub(r'\{\{price:(E[1-9]\d*)\}\}',
                        lambda m: '当季不适用（参考价格见表）' if m[1] in excluded else m[0], item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, str) and affected(item):
                    value[index] = re.sub(r'\{\{price:(E[1-9]\d*)\}\}',
                        lambda m: '当季不适用（参考价格见表）' if m[1] in excluded else m[0], item)
                else:
                    walk(item)
    walk(data)
    for tier in (data.get('overview') or {}).get('budget_tiers', []):
        tier.pop('_verified_subtotals', None)
    if excluded:
        for index, tier in enumerate(tiers):
            totals = {}
            ids = dict.fromkeys(re.findall(r'\{\{price:(E[1-9]\d*)\}\}', ' '.join(tier.get('verified_items', []))))
            for eid in ids:
                row = ledger['evidence'][eid]
                amount = (display_totals or {}).get(eid, row.get('total_amount'))
                if amount is None:
                    continue  # Legacy derived totals require the display quantity contract.
                currency = row['currency']
                totals[currency] = totals.get(currency, Decimal(0)) + Decimal(amount)
            if index in affected_tiers and tier.get('total_range') and any(value != '未核验' for value in tier['total_range'].values()):
                tier['total_range'] = {'lower': '未核验', 'upper': '未核验'}
            tier['_verified_subtotals'] = {key: format(value, 'f') for key, value in totals.items()}
    lines = []
    for line in prose.splitlines():
        if affected(line):
            if line.lstrip().startswith('|'):
                line = re.sub(r'(\{\{price:(E[1-9]\d*)\}\})',
                              lambda m: m[1] + ('（仅供参考·当季不适用，不计入预算）' if m[2] in excluded else ''), line)
            else:
                line = re.sub(r'\{\{price:(E[1-9]\d*)\}\}',
                              lambda m: '当季不适用（参考价格见表）' if m[1] in excluded else m[0], line)
        lines.append(line)
    return '\n'.join(lines), data, excluded
