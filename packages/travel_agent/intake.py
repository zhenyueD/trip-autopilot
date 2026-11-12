"""Evidence-bound understanding of user-selected materials, not truth certificates."""
import copy
import hashlib

from .validation import PlanError, records, strings, text
from .date_window import validate_date_window

MAX_MATERIAL_CHARS = 100000
MAX_IMAGE_TEXT_CHARS = 12000


def check_source_budget(sources):
    # Reserve extraction space before accepting materials, not after a paid run.
    reserved = sum(8000 if x['kind'] == 'web_link' and x['status'] in ('pending', 'unavailable')
                   else MAX_IMAGE_TEXT_CHARS if x['kind'] == 'image_extracted' and x['status'] == 'pending'
                   else len(x.get('content', '')) for x in sources)
    if len(sources) > 60 or reserved > MAX_MATERIAL_CHARS:
        raise ValueError('本趟材料已达到处理上限，新材料未接收；现有旅行仍可查看和调整')


def add_sources(existing, *, revision, brief='', note='', feedback='', links=None, images=None,
                documents=None):
    sources = copy.deepcopy(existing)
    for kind, value, label in [('user_brief', brief, '你的旅行想法'),
                               ('user_text', note, '你分享的材料'),
                               ('user_feedback', feedback, '你的最新补充')]:
        if value.strip():
            sources.append({'id': f'{kind}-{revision}', 'kind': kind, 'label': label,
                            'status': 'read', 'content': value})
    seen = {x.get('url') for x in sources if x['kind'] == 'web_link'}
    for url in links or []:
        if url not in seen:
            sources.append({'id': 'link-' + hashlib.sha256(url.encode()).hexdigest()[:16],
                            'kind': 'web_link', 'label': '收藏链接', 'url': url,
                            'status': 'pending', 'content': ''})
            seen.add(url)
    if images:
        sources.append({'id': f'photos-{revision}', 'kind': 'image_extracted',
                        'label': '图片材料：' + '、'.join(x['name'] for x in images),
                        'status': 'pending', 'content': ''})
    known_documents = {x.get('sha256') for x in sources if x['kind'] == 'document_extracted'}
    for document in documents or []:
        if document['sha256'] not in known_documents:
            sources.append(copy.deepcopy(document))
            known_documents.add(document['sha256'])
    check_source_budget(sources)
    return sources


def material_receipts(sources):
    from .detail_reviews import source_withdrawn
    result = []
    for source in sources:
        receipt = {key: source[key] for key in ('id', 'kind', 'label', 'status', 'url', 'message') if key in source}
        if source_withdrawn(source):
            receipt.update(status='unavailable', availability='withdrawn',
                message='此版本材料已下架，不再用于回答或安排行动。原文保留供核查；可补充较新的可靠材料。')
        result.append(receipt)
    return result


def _evidence(items, sources):
    result = []
    for item in records(items, '理解证据', 5):
        source = sources.get(item.get('source_id'))
        quote = text(item.get('quote'), '来源引文', 600).strip()
        if not source or source.get('status') not in ('read', 'image_extracted'):
            raise PlanError('理解引用了未成功读取的材料')
        if not quote or quote not in source.get('content', ''):
            raise PlanError('理解引文不在原始材料中；不能编造用户说过的话')
        result.append({'source_id': source['id'], 'quote': quote, 'label': source['label']})
    return result


def _signal(item, sources):
    evidence = _evidence(item.get('evidence', []), sources)
    kind = 'hypothesis'
    if item.get('kind') != 'hypothesis':
        if not evidence:
            raise PlanError('不是猜测的理解必须有材料引文')
        kinds = {sources[e['source_id']]['kind'] for e in evidence}
        kind = ('source_claim' if 'web_link' in kinds else
                'image_extracted' if 'image_extracted' in kinds else
                'document_extracted' if 'document_extracted' in kinds else
                'user_provided' if kinds <= {'user_text', 'user_brief', 'user_feedback'} else 'hypothesis')
    return {'id': text(item.get('id'), '理解ID', 80),
            'text': text(item.get('text'), '理解内容'), 'kind': kind,
            'implication': text(item.get('implication', ''), '如何影响安排'),
            'evidence': evidence}


def _refs(value, known):
    result = strings(value, '理解引用', 20)
    if any(id_ not in known for id_ in result):
        raise PlanError('冲突或默认方向引用了不存在的理解')
    return result


def _source_notes(raw, sources, signals):
    notes, seen = [], set()
    signal_map = {x['id']: x for x in signals}
    for item in records(raw.get('source_notes', []), '材料处置', 60):
        source_id, outcome = item.get('source_id'), item.get('outcome')
        if source_id not in sources or source_id in seen:
            raise PlanError('材料处置必须引用现有来源且不重复')
        source = sources[source_id]
        seen.add(source_id)
        if outcome not in ('used', 'context_only', 'needs_clarification', 'unavailable'):
            raise PlanError('材料处置状态不正确')
        if source['status'] not in ('read', 'image_extracted'):
            if outcome != 'unavailable':
                raise PlanError('没有读取的材料不能被采用')
        elif outcome == 'unavailable':
            raise PlanError('已读取的材料需说明如何采用或为何未采用')
        refs = _refs(item.get('signal_ids', []), set(signal_map))
        if outcome == 'used' and not any(e['source_id'] == source_id
                for id_ in refs for e in signal_map[id_]['evidence']):
            supported = [s['id'] for s in signals if any(e['source_id'] == source_id for e in s['evidence'])]
            raise PlanError(f'材料 {source_id} 声称used，但signal_ids未关联引用该来源的理解；可引用的理解ID：{supported}')
        evidence = (_evidence([{'source_id': source_id, 'quote': item.get('quote', '')}], sources)
                    if outcome in ('context_only', 'needs_clarification') else [])
        notes.append({'source_id': source_id, 'outcome': outcome,
                      'explanation': text(item.get('explanation'), '材料处置理由'),
                      'signal_ids': refs, 'evidence': evidence})
    if seen != set(sources):
        raise PlanError('每份材料都需要source_notes：如何使用、仅作背景、待澄清或未读到；不得略过')
    return notes


def validate_intake(raw, source_records, questions=None, require_disposition=False):
    if not isinstance(raw, dict):
        raise PlanError('缺少对材料的有依据理解，请返回intake对象')
    sources = {x['id']: x for x in source_records}
    signals = [_signal(item, sources) for item in records(raw.get('signals'), '材料理解', 20)]
    known = {x['id'] for x in signals}
    if len(known) != len(signals) or '' in known:
        raise PlanError('理解ID必须非空且唯一')
    conflicts, defaults = [], []
    for item in records(raw.get('conflicts', []), '材料冲突', 6):
        refs = _refs(item.get('signal_ids', []), known)
        if require_disposition and len(set(refs)) < 2:
            raise PlanError(f'冲突 {item.get("id", "")!s} 仅引用 {refs}；需要拆出两条真实互斥理解分别引用。已被最新补充解决或目录凭空制造的冲突应移除，不能编造第二条')
        fields = strings(item.get('fields', []), '冲突影响的条件', 4)
        if (require_disposition and not fields) or any(x not in ('dates', 'party', 'budget', 'other') for x in fields):
            raise PlanError('冲突必须标注fields：dates为全程日期，party为同行，budget或other；不可省略影响条件')
        conflict = {**{k: text(item.get(k, ''), k) for k in ('id', 'summary', 'question')},
                    'signal_ids': refs, 'fields': fields}
        if 'dates' in fields:
            conflict['date_options'] = _date_options(item, sources)
            if raw.get('date_window') is not None:
                raise PlanError('全程日期仍冲突，date_window必须null：不可选更早/更多/更完整一方，也不可合并互斥日期；以共同天数排相对日程')
        conflicts.append(conflict)
    for item in records(raw.get('defaults', []), '默认取舍', 6):
        defaults.append({**{k: text(item.get(k, ''), k) for k in ('choice', 'reason')},
                         'signal_ids': _refs(item.get('signal_ids', []), known)})
    warnings = [f"{x['label']}：{x.get('message') or '尚未读取，不能据此作决定'}"
                for x in source_records if x.get('status') in ('blocked', 'unavailable')]
    dispositions = _source_notes(raw, sources, signals) if require_disposition else []
    return {'summary': text(raw.get('summary'), '材料理解摘要'), 'signals': signals,
            'conflicts': conflicts, 'defaults': defaults,
            'source_notes': dispositions,
            'date_window': validate_date_window(raw.get('date_window'), sources),
            'questions': strings(questions or [], '待补硬条件', 3), 'warnings': warnings}


def _date_options(conflict, sources):
    options = records(conflict.get('date_options'), '全程日期候选', 4)
    if len(options) < 2:
        raise PlanError('全程日期冲突须分别列出至少两个有引文的date_options，不能取跨候选的并集')
    result = [validate_date_window({**option, 'basis': 'provisional',
        'assumption': '互为替代的未定日期候选，不是生效窗口'}, sources) for option in options]
    if len({(x['start'], x['end']) for x in result}) != len(result):
        raise PlanError('日期候选不得用重复窗口伪造冲突')
    return result


def source_support(raw, source_records):
    """Tell a repair model exactly which evidence edges already exist; no truth upgrade."""
    signals = raw.get('signals', []) if isinstance(raw, dict) else []
    signals = signals if isinstance(signals, list) else []
    result = []
    for source in source_records:
        ids = []
        for signal in signals:
            if not isinstance(signal, dict) or not isinstance(signal.get('evidence'), list):
                continue
            if any(isinstance(e, dict) and e.get('source_id') == source['id']
                   and isinstance(e.get('quote'), str) and e['quote']
                   and e['quote'] in source.get('content', '') for e in signal['evidence']):
                ids.append(signal.get('id'))
        result.append({'source_id': source['id'], 'status': source['status'], 'quoted_by_signal_ids': ids})
    return result
