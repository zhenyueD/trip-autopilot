"""Deterministic revision facts, with bounded checks for explicit car cancellation.

Field differences prove edits, not arbitrary natural-language goal completion.
The narrow action check is intentionally labelled; a model's self-assessment
can neither manufacture a diff nor override a failed action check.
"""
import copy
import re

from .validation import PlanError


class DeliveryTruthError(PlanError):
    """A candidate cannot truthfully be delivered as this revision's result."""


_DAY = re.compile(r'第?([一二三四五六七八九十两\d]+)天|\bday\s*(\d+)\b', re.I)
_CAR = re.compile(r'自驾|驾车|开车|(?<!出)租车|取车|还车|驾照|停车|self[- ]?driv\w*|rental\s+car|driv(?:e|ing)', re.I)
_CANCEL = re.compile(r'(?:不打算|不想|不再|不要|不用|不|取消|放弃)(?:继续|再|自己|安排)?(?:自驾|驾车|开车|租车)|(?:自驾|租车)(?:安排|计划)?(?:取消|放弃)', re.I)
_CANCELLATION = re.compile(r'取消|放弃|不去了|不要安排|不打算|不再去|不想去|cancel|remove', re.I)
_KEEP_CANCELLATION = re.compile(r'(?:不用|不要|无需|不必|不需要|不想|不打算|别|不)(?:再)?取消')
_CONDITIONAL = re.compile(r'如果|只要|除非|否则|若|视.{0,8}情况|看.{0,8}天气|\bif\b|\bunless\b', re.I)
_MODES = {'出租车': 'taxi', '打车': 'taxi', '网约车': 'taxi', 'taxi': 'taxi',
          '公交': 'bus', '巴士': 'bus', 'bus': 'bus', '公共交通': 'public',
          '地铁': 'metro', 'metro': 'metro', '火车': 'train', '高铁': 'train', 'train': 'train',
          '步行': 'walk', '徒步': 'walk', 'walk': 'walk', '骑行': 'cycle',
          '渡轮': 'ferry', '轮渡': 'ferry', '接送': 'chauffeur', '包车': 'chauffeur',
          'chauffeur': 'chauffeur'}
_MODE_PATTERN = '|'.join(re.escape(mode) for mode in _MODES)
_NEGATED_CAR = re.compile(r'(?:不再|不要|不需|无需|不用|不需要|取消|不|非|已取消|放弃)(?:使用|安排|携带|办理)?(?:自驾|驾车|开车|租车|取车|还车|驾照|停车)')
_LIMITS = '仅证明实际字段变更与有界的计划选择；不证明交通服务现实可用，不构成通用用户目标语义证明、外部事实核验或预订确认。'


def _day_numbers(value):
    numbers = set()
    digits = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
              '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    for match in _DAY.finditer(value):
        word = match[1] or match[2]
        if word.isdigit():
            numbers.add(int(word))
        elif word in digits:
            numbers.add(digits[word])
        elif word.startswith('十') and word[1:] in digits:
            numbers.add(10 + digits[word[1:]])
    return numbers


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            if key not in ('id', 'supply_id', 'supply_ids', 'related_item_ids', 'source_url'):
                yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _has_car(value):
    for text in _strings(value):
        if re.search(r'(?:不|不用|无需|不要|没有|尚未)取消.{0,4}(?:自驾|租车|还车|取车)', text):
            return True
        if _CAR.search(_NEGATED_CAR.sub('', text)):
            return True
    return False


def _field_diff(before, after, path, result):
    if before == after:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            _field_diff(before.get(key), after.get(key), f'{path}/{key}', result)
    else:
        result.append({'path': path, 'before': copy.deepcopy(before), 'after': copy.deepcopy(after)})


def revision_diff(previous, plan, *, complete=False):
    """Return user-editable facts, excluding generated prose and derived state."""
    result = []
    old_days = {d['day']: d for d in previous.get('days', [])}
    new_days = {d['day']: d for d in plan.get('days', [])}
    for number in sorted(set(old_days) | set(new_days)):
        old, new = old_days.get(number, {}), new_days.get(number, {})
        for field in ('date', 'title', 'rationale', 'city', 'lodging'):
            _field_diff(old.get(field), new.get(field), f'/days/{number}/{field}', result)
        before = {i['id']: i for i in old.get('items', [])}
        after = {i['id']: i for i in new.get('items', [])}
        if complete:
            _field_diff(list(before), list(after), f'/days/{number}/item_order', result)
        for id_ in sorted(set(before) | set(after)):
            _field_diff(before.get(id_), after.get(id_), f'/days/{number}/items/{id_}', result)
    for field in ('budget', 'decisions', 'preparation', 'questions'):
        _field_diff(previous.get(field), plan.get(field), '/' + field, result)
    for field in ('understanding', 'user_commitments', 'commitment_schedule'):
        _field_diff(previous.get(field, []), plan.get(field, []), '/' + field, result)
    for field in ('sequence', 'tradeoff', 'supply_ids', 'item_ids'):
        _field_diff(previous.get('signature', {}).get(field), plan.get('signature', {}).get(field),
                    '/signature/' + field, result)
    _field_diff(previous.get('journey', {}).get('artifacts', []),
                plan.get('journey', {}).get('artifacts', []), '/journey/artifacts', result)
    if complete:
        domains = [(previous, plan, '', {'days', 'signature', 'journey', 'budget', 'decisions',
            'preparation', 'questions', 'understanding', 'user_commitments', 'commitment_schedule',
            'change_summary', 'revision_changes', 'delivery_truth'}),
            (previous.get('signature', {}), plan.get('signature', {}), '/signature',
             {'sequence', 'tradeoff', 'supply_ids', 'item_ids'}),
            (previous.get('journey', {}), plan.get('journey', {}), '/journey', {'artifacts'})]
        domains.extend((old_days.get(n, {}), new_days.get(n, {}), f'/days/{n}',
            {'day', 'date', 'title', 'rationale', 'city', 'lodging', 'items'})
            for n in sorted(set(old_days) | set(new_days)))
        for old, new, path, covered in domains:
            for field in sorted((set(old) | set(new)) - covered):
                _field_diff(old.get(field), new.get(field), f'{path}/{field}', result)
    return result


def _relevant(row, targets, ids):
    refs = set(row.get('related_item_ids', []))
    if refs:
        return bool(refs & ids)
    days = ({row['day']} if type(row.get('day')) is int else
            _day_numbers(' '.join(_strings(row))))
    return not days or bool(days & targets)


def _dependent_has_car(row, targets, ids):
    if row.get('status') == 'not_applicable' or not _relevant(row, targets, ids):
        return False
    for text in _strings(row):
        scope = set()
        for clause in re.split(r'[，。；,;\n]', text):
            scope = _day_numbers(clause) or scope
            if (not scope or scope & targets) and _has_car(clause):
                return True
    return False


def _supported_cancel_clause(clause):
    """Explicitly bounded grammar; unknown qualifiers are not unconditional."""
    remaining = _DAY.sub('', _CANCEL.sub('', clause))
    remaining = re.sub(r'(?:改为|改成|换成|改坐|改乘).*$', '', remaining)
    return re.fullmatch(r'(?:\s|我们|我|请|麻烦|本次|这次|当天|现在|全程|全部|整个行程|'
                        r'就|也|还|同时|并且|以及|并|打算|决定|要|计划|想|了|吧|即可|就行|'
                        r'的安排|的计划|安排)*', remaining) is not None


def _cancellation_scope(feedback, default_days):
    """Recognize the whole bounded request, never treat one match as coverage.

    Conditions and unrecognized cancellation objects are intentionally outside
    this check. A keep-cancellation phrase is not a cancellation instruction.
    """
    value = _KEEP_CANCELLATION.sub('保留', feedback)
    targets, issues, scope = set(), [], set()
    cancellation_seen = False
    for clause in re.split(r'[，。；,;\n]', value):
        scope = _day_numbers(clause) or scope
        matches = list(_CANCEL.finditer(clause))
        if not matches and not _CANCELLATION.search(clause):
            continue
        cancellation_seen = True
        remaining = _CANCEL.sub('', clause)
        # "cancel self-drive and the museum" has a second implicit object;
        # repeated verbs must also all be covered, not just the first match.
        bundled = any(re.match(r'\s*(?:和|与|及|、)', clause[m.end():]) for m in matches)
        if (not matches or _CANCELLATION.search(remaining) or bundled
                or not _supported_cancel_clause(clause)):
            issues.append('本轮包含未覆盖的取消目标，尚无可靠的程序核对规则，不能声称已落实')
        if matches:
            targets.update(scope or default_days)
    if cancellation_seen and _CONDITIONAL.search(value):
        issues.append('本轮取消带有条件或分支，条件尚未核实，不能按无条件取消交付')
    return targets, list(dict.fromkeys(issues))


def _known_destinations(previous, number):
    """Literal existing plan destinations; never an arbitrary text suffix."""
    day = next((d for d in previous.get('days', []) if d['day'] == number), {})
    supplies = {row['id']: row for row in previous.get('supplies', [])}
    destinations = {'目的地', '下一站', '原定地点'}
    for item in day.get('items', []):
        name = supplies.get(item.get('supply_id'), {}).get('name', '')
        destinations.update(value for value in (name, item.get('title', '')) if value)
        for noun in ('博物馆', '美术馆', '酒店', '机场', '车站', '公园'):
            if name.endswith(noun):
                destinations.add(noun)
    return destinations


def _affirmative_alternative(transport, destinations):
    """Full finite selection grammar, not a keyword/negative-word classifier.

    Every clause must be a selected mode, an explicit excluded mode, or one of
    the bounded planning caveats. Destination suffixes are literal prior-plan
    labels. Unknown prose cannot inherit proof from a known mode substring.
    """
    destination = '|'.join(re.escape(value) for value in sorted(destinations))
    selection = (rf'(?:改为|改成|改乘|改坐|改|选择|选用|使用|乘坐|乘|坐)?'
                 rf'(?P<mode>{_MODE_PATTERN})(?:(?:前往|返回|到|至|去|回)(?:{destination}))?')
    excluded = rf'(?:不坐|不乘|不要|不用|不选择|不使用|不)(?P<mode>{_MODE_PATTERN})'
    caveat = r'(?:具体)?(?:车程|费用|价格|路线)(?:仍)?(?:待|尚待)(?:路线)?(?:确认|核验)'
    selected, declined = set(), set()
    for clause in re.split(r'[，。；,;\n]', transport):
        clause = clause.strip()
        if not clause:
            continue
        choice = re.fullmatch(selection, clause, re.I)
        if choice:
            selected.add(_MODES[choice['mode'].lower()])
            continue
        exclusion = re.fullmatch(excluded, clause, re.I)
        if exclusion:
            declined.add(_MODES[exclusion['mode'].lower()])
            continue
        if _NEGATED_CAR.fullmatch(clause) or re.fullmatch(caveat, clause):
            continue
        return False
    return bool(selected) and not selected.intersection(declined)


def _cancel_car_issues(previous, plan, targets, diff, *, include_pack=False):
    if not targets:
        return [], []
    before = {d['day']: d for d in previous.get('days', [])}
    after = {d['day']: d for d in plan.get('days', [])}
    issues, checks = [], []
    ids = {item['id'] for n in targets for item in before.get(n, {}).get('items', [])}
    ids.update(item['id'] for n in targets for item in after.get(n, {}).get('items', []))
    for number in sorted(targets):
        day = after.get(number)
        if not day:
            issues.append(f'第{number}天没有可核对的替代安排')
            continue
        changed = [row for row in diff if row['path'].startswith(f'/days/{number}/items/')
                   and (row['path'].count('/') == 4 or row['path'].endswith('/transport'))]
        if not changed:
            issues.append(f'第{number}天没有实际交通替换或行程移除')
        if _has_car(day):
            issues.append(f'第{number}天仍有自驾、租还车或驾驶准备残留')
        destinations = _known_destinations(previous, number)
        transports = [item['transport'] for item in day.get('items', []) if item.get('transport')]
        if not transports or not all(_affirmative_alternative(value, destinations) for value in transports):
            issues.append(f'第{number}天没有明确的非自驾交通安排')
        checks.append({'action': 'cancel_self_drive', 'day': number,
                       'status': 'not_established' if issues else 'established'})
    dependents = [*plan.get('decisions', []), *plan.get('preparation', []),
                  *plan.get('journey', {}).get('artifacts', []),
                  *(row for row in plan.get('journey', {}).get('tasks', [])
                    if row.get('status') != 'completed')]
    if include_pack:
        dependents.extend(row for row in (plan.get('preparation_pack') or {}).get('actions', [])
                          if row.get('applicability') != 'needs_reconfirmation')
    if any(_dependent_has_car(row, targets, ids) for row in dependents):
        issues.append('关联决定、准备或随身材料仍要求自驾或租还车')
    if issues:
        for check in checks:
            check['status'] = 'not_established'
    return issues, checks


def validate_preparation_days(plan, feedback='', previous=None):
    """Final bounded cross-surface gate, including the post-compose action pack.

    Pending questions alone do not prove a changed transport choice. Explicit
    no-car instructions must agree with the itinerary and active preparation.
    """
    days = {day['day']: day for day in plan.get('days', []) if 'day' in day}
    targets, _ = _cancellation_scope(feedback, set(days))
    issues = []
    if targets and previous:
        issues, _ = _cancel_car_issues(previous, plan, targets, revision_diff(previous, plan), include_pack=True)
    preparations = [*plan.get('preparation', []),
                    *(plan.get('preparation_pack') or {}).get('actions', [])]
    item_days = {item['id']: number for number, day in days.items() for item in day.get('items', [])}
    for row in preparations:
        if row.get('status') == 'not_applicable' or row.get('applicability') == 'needs_reconfirmation':
            continue
        text = ' '.join(_strings(row))
        if not _CANCEL.search(_KEEP_CANCELLATION.sub('保留', text)) or _CONDITIONAL.search(text):
            continue
        linked = {item_days[id_] for id_ in row.get('related_item_ids', []) if id_ in item_days}
        scope = linked or ({row['day']} if type(row.get('day')) is int else _day_numbers(text)) or targets
        if any(_has_car(days[number]) for number in scope if number in days):
            issues.append('准备事项要求取消自驾，但关联日程仍保留自驾或租还车')
    if issues:
        report = {'status': 'rejected', 'issues': list(dict.fromkeys(issues)),
                  'gate': 'preparation_days', 'limits': _LIMITS}
        error = DeliveryTruthError('准备事项与日程不一致，原方案保留：' + '；'.join(report['issues']))
        error.delivery_truth = report
        raise error
    return {'status': 'checked', 'gate': 'preparation_days', 'limits': _LIMITS}


def _summary(diff):
    """Only fixed labels/counts, never model prose copied as a factual claim."""
    summaries = []
    numbers = sorted({int(row['path'].split('/')[2]) for row in diff
                      if row['path'].startswith('/days/')})
    for number in numbers:
        rows = [r for r in diff if r['path'].startswith(f'/days/{number}/')]
        fields = []
        for suffix, label in (('/transport', '交通说明'), ('/time', '开始时间'),
                              ('/end_time', '结束时间'), ('/preparation', '准备说明'),
                              ('/title', '标题'), ('/why', '活动说明'), ('/action_guide', '行动指南'),
                              ('/lodging', '住宿说明'), ('/city', '城市'), ('/date', '日期'),
                              ('/item_order', '活动顺序')):
            if any(r['path'].endswith(suffix) or suffix + '/' in r['path'] for r in rows):
                fields.append(label)
        if any(r['path'].count('/') == 4 for r in rows):
            fields.append('活动增删')
        summaries.append(f'第{number}天实际更新：' + '、'.join(fields or ['日程字段']) + '。')
    domains = (('/budget', '预算说明'), ('/decisions', '旅行决定'),
                          ('/preparation', '准备事项'), ('/questions', '待澄清问题'),
                          ('/journey/artifacts', '随身材料'), ('/understanding', '需求记录'),
                          ('/user_commitments', '用户要求记录'), ('/commitment_schedule', '要求对应安排'),
                          ('/signature', '体验构想'), ('/title', '方案标题'), ('/summary', '安排说明'),
                          ('/preparation_pack', '准备行动包'), ('/journey', '旅行任务'),
                          ('/supplies', '供给资料'), ('/source_evidence', '来源资料'),
                          ('/routes', '路线资料'), ('/commitment_checks', '要求核对'),
                          ('/decision_links', '决定关联'), ('/readiness', '准备状态'))
    matched = set()
    for prefix, label in domains:
        rows = {row['path'] for row in diff if row['path'] == prefix or row['path'].startswith(prefix + '/')}
        if rows - matched:
            summaries.append(f'实际更新：{label}。')
            matched.update(rows)
    other = {row['path'].split('/')[1] for row in diff
             if row['path'] not in matched and not row['path'].startswith('/days/')}
    if other:
        summaries.append(f'实际更新：其他方案字段（{len(other)}类）。')
    return summaries


def validate_delivery(previous, plan, feedback, *, fact_correction=False):
    """Attach factual summaries or reject; mutates only the uncommitted candidate."""
    if not previous or not feedback:
        return {'status': 'not_applicable', 'limits': _LIMITS}
    diff = revision_diff(previous, plan)
    substantive = [row for row in diff if not row['path'].endswith(('/title', '/rationale', '/why'))
                   and (row['path'] != '/understanding' or fact_correction)]
    targets, request_issues = _cancellation_scope(feedback, {d['day'] for d in previous.get('days', [])})
    issues, checks = _cancel_car_issues(previous, plan, targets, diff)
    issues.extend(request_issues)
    if request_issues:
        for check in checks:
            check['status'] = 'not_established'
    if not substantive:
        issues.append('没有实际安排、决定、材料或待澄清问题的变更，不能仅改理解或标题')
    report = {'status': 'rejected' if issues else 'diff_verified', 'diff': diff,
              'action_checks': checks, 'issues': issues, 'limits': _LIMITS,
              'goal_verification': 'bounded_action_check' if checks else 'not_established'}
    if issues:
        error = DeliveryTruthError('本轮未能落实你的要求，原方案保留：' + '；'.join(issues))
        error.delivery_truth = report
        raise error
    plan['change_summary'] = _summary(diff)
    plan['summary'] = '本轮方案变更：' + ' '.join(plan['change_summary'])
    if not checks:
        plan['summary'] += ' 以上仅为实际修改记录，是否满足本次要求仍需核对。'
    conditional = sum(row.get('status') == 'conditional' for row in plan.get('commitment_checks', []))
    if conditional:
        plan['summary'] += f' 仍有{conditional}项条件安排，详见每日要求核对。'
    plan['delivery_truth'] = report
    return report
