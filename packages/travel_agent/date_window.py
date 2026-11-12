"""Calendar arithmetic over model-extracted dates; lodging is only a coverage window."""
from datetime import date
import re

from .validation import PlanError, records, text

ENGLISH_MONTHS = ('January', 'February', 'March', 'April', 'May', 'June',
                  'July', 'August', 'September', 'October', 'November', 'December')


def _english_quoted(value, quote):
    """Recognize explicit English month names without process-wide locale changes."""
    month = ENGLISH_MONTHS[value.month - 1]
    names = {month, month[:3]} | ({'Sept'} if value.month == 9 else set())
    word = '(?:' + '|'.join(sorted(names, key=len, reverse=True)) + ')'
    day = f'0?{value.day}' if value.day < 10 else str(value.day)
    forms = (rf'{day}\s+{word}\s+{value.year}',
             rf'{word}\s+{day}(?:,\s*|\s+){value.year}')
    return any(re.search(r'(?<![A-Za-z\d])' + form + r'(?![A-Za-z\d])', quote, re.I)
               for form in forms)


def _date(value, label):
    value = text(value, label, 10)
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except ValueError:
        pass
    raise PlanError(f'{label}必须是合法的YYYY-MM-DD日期')


def _quoted(value, evidence):
    forms = {value.isoformat()} | {
        f'{value.year:04}年{month}月{day}日'
        for month in (str(value.month), f'{value.month:02}')
        for day in (str(value.day), f'{value.day:02}')}
    pattern = r'(?<!\d)(?:' + '|'.join(re.escape(form) for form in forms) + r')(?!\d)'
    for item in evidence:
        if re.search(pattern, item['quote']) or _english_quoted(value, item['quote']):
            return True
        ranges = re.findall(r'(?<!\d)([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日'
                            r'\s*(?:至|到|—)\s*([0-9]{1,2})日(?!\d)', item['quote'])
        for year, month, first, last in ranges:
            try:
                start = date(int(year), int(month), int(first))
                end = date(int(year), int(month), int(last))
            except ValueError:
                continue
            if start <= end and value in (start, end):
                return True
    return False


def validate_date_window(raw, sources):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PlanError('日期窗口必须为对象或null')
    start, end = _date(raw.get('start'), '开始日期'), _date(raw.get('end'), '结束日期')
    duration = (end - start).days + 1
    if duration < 1:
        raise PlanError('日期先后错误：结束日期不能早于开始日期')
    if duration > 14:
        raise PlanError('日期窗口超过当前14天规划范围；请明确调整范围，不能缩短或截断')
    basis = text(raw.get('basis'), '日期依据', 20)
    if basis not in ('trip', 'lodging_only', 'provisional'):
        raise PlanError('日期依据必须为trip、lodging_only或provisional')
    assumption = text(raw.get('assumption', ''), '日期假设').strip()
    if basis != 'trip' and not assumption:
        raise PlanError('住宿或暂定日期窗口必须明示假设，不能冒充完整旅行或航班日期')
    evidence = []
    for item in records(raw.get('evidence'), '日期证据', 5):
        source = sources.get(text(item.get('source_id'), '日期来源ID', 100))
        quote = text(item.get('quote'), '日期引文', 600).strip()
        if not source or source.get('status') not in ('read', 'image_extracted'):
            raise PlanError('日期引用了未成功读取的材料')
        if not quote or quote not in source.get('content', ''):
            raise PlanError('日期引文必须逐字存在于来源材料中，不能编造')
        evidence.append({'source_id': source['id'], 'quote': quote,
                         'label': source['label'], 'kind': source['kind']})
    if not all(_quoted(value, evidence) for value in (start, end)):
        raise PlanError('开始与结束日期必须分别见于来源短引文；只有晚数或日期不全时请返回null，不编造日期')
    return {'start': start.isoformat(), 'end': end.isoformat(),
            'duration_days': duration, 'basis': basis,
            'assumption': assumption, 'evidence': evidence}
