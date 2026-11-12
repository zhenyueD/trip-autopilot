"""Writer-facing contract; authoritative validation remains in revision_plan.

This is an OPEN JSON Schema, not an API strict-output schema. Closing objects or
requiring every optional property would silently narrow preserved plan fields.
Context-dependent references, locks and provenance still need the validator.
"""
import json
from .detail_contract import DETAIL_RULES
from .travel_principles import REASONING_SCHEMA, SUGGESTIONS_SCHEMA


def _text(limit=1200):
    return {'type': 'string', 'maxLength': limit}


def _array(items, limit, minimum=0):
    return {'type': 'array', 'items': items, 'maxItems': limit, 'minItems': minimum}


def _object(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {},
            'required': list(required), 'additionalProperties': True}


def _texts(limit=30):
    return _array(_text(), limit)


def _enum(*values):
    return {'enum': list(values)}


def _fields(*names):
    return {name: _text() for name in names}


_ID = {**_text(160), 'minLength': 1}
_CLOCK = {'type': 'string', 'pattern': r'^(?:[01]\d|2[0-3]):[0-5]\d$'}
_GUIDE = {**_object({**_fields('meeting_point', 'booking_note'),
                   'steps': _texts(8), 'bring': _texts(12)}),
          'additionalProperties': False}
_ITEM = _object({
    **_fields('title', 'why', 'transport', 'preparation'),
    'id': {**_text(80), 'minLength': 1}, 'time': _CLOCK, 'end_time': _CLOCK,
    'supply_id': {'anyOf': [_text(160), {'type': 'null'}]},
    'activity_kind': _enum('rest', 'meal', 'activity', 'personal', 'transfer'),
    'experience': _object({**_fields('background', 'personal_fit', 'photo_tip', 'practical_tip'),
                           'what_to_do': _texts(6)}),
    # Historical encodings accepted by the existing validator must stay accepted.
    'action_guide': {'anyOf': [_GUIDE, _text(), _texts(8), {'type': 'null'}]},
}, ('id', 'time', 'end_time'))
_DAY = _object({
    **_fields('title', 'rationale', 'lodging'), 'day': {'type': 'integer', 'minimum': 1},
    'date': {'type': 'string', 'maxLength': 10, 'pattern': r'^\d{4}-\d{2}-\d{2}$', 'format': 'date'},
    'city': _text(200), 'items': _array(_ITEM, 10, 1),
}, ('day', 'title', 'items'))
_DECISION = _object({
    **_fields('category', 'choice', 'reason'), 'supply_ids': _texts(),
    'status': _enum('proposed', 'provided', 'needs_check', 'user_action', 'not_applicable',
                    'pending', 'needs_confirmation', 'draft', 'unknown', 'unverified'),
})
_PREPARATION = _object({
    **_fields('task', 'category', 'why', 'when'),
    'status': _enum('needs_check', 'user_action', 'not_applicable'),
    'supply_ids': _texts(), 'steps': _texts(8), 'materials': _texts(12),
})
_DETAIL = _object({'field': _enum('time', 'price', 'phone', 'url', 'booking', 'caution'),
    'value': {'anyOf': [_text(), {'type': 'null'}]}, 'detail_ids': _texts(),
    'status': _enum('unverified', 'unknown')}, ('field', 'value', 'detail_ids', 'status'))
_PREPARATION['properties']['details'] = _array(_DETAIL, 30)
_ITEM['properties']['guide_step_ids'] = _texts()
_ACTION = _object({
    'id': _ID, 'title': _text(), 'owner': _enum('agent', 'user'),
    'status': _enum('ready', 'needs_user', 'needs_research', 'user_reported', 'not_applicable'),
    'day': {'anyOf': [{'type': 'integer', 'minimum': 1}, {'type': 'null'}]},
    **{key: _texts() for key in ('instructions', 'materials', 'assumptions')},
    **{key: _text(4000) for key in ('when', 'completion_condition', 'fallback', 'report_note')},
}, ('id', 'title', 'owner', 'status'))
_ACTION['properties']['details'] = _array(_DETAIL, 30)
_ARTIFACT = _object({
    'id': _ID, 'title': _text(4000), 'content': _text(4000),
    'kind': _enum('experience_guide', 'packing_list', 'inquiry_draft', 'route_note'),
    'status': _enum('generated'),
}, ('id', 'title', 'content', 'kind'))
_TASK = _object({'id': _ID, 'title': _text(), 'kind': {'type': 'string'},
                 'status': {'type': 'string'}}, ('id', 'title', 'kind', 'status'))
_INDEXED = _object({'id': _ID}, ('id',))

WRITER_PLAN_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    **_object({
        **_fields('title', 'summary', 'budget'),
        'reasoning': REASONING_SCHEMA, 'suggestions': SUGGESTIONS_SCHEMA,
        'traveler_profile': _object({'version': {'const': 1},
            'traveler_groups': _texts(12), 'mobility_preferences': _texts(20),
            'pace_preference': _text(400), 'preferences': _texts(30), 'user_notes': _text(4000)}),
        'understanding': _array(_object({'text': _text(), 'source': _text(200)}, ('text',)), 16),
        'signature': _object({**_fields('title', 'why', 'tradeoff'), 'sequence': _texts(),
                              'supply_ids': _texts()}, ('sequence',)),
        'days': _array(_DAY, 14, 1), 'decisions': _array(_DECISION, 24),
        'preparation': _array(_PREPARATION, 30), 'questions': _texts(3), 'change_summary': _texts(12),
        'supplies': _array(_INDEXED, 200), 'source_evidence': _array(_INDEXED, 200),
        'preparation_pack': _object({'summary': _text(4000), 'status': _enum('ready', 'partial'),
                                     'actions': _array(_ACTION, 200),
                                     'sources': _array(_INDEXED, 200)}, ('actions',)),
        'journey': _object({'artifacts': _array(_ARTIFACT, 200), 'tasks': _array(_TASK, 200),
                            'learning': _array(_object(), 30)}),
        'readiness': _object({'ready_to_depart': {'type': 'boolean'}}),
        'delivery_truth': _object(), 'user_commitments': _array(_INDEXED, 200),
        **{key: _array(_object(), 200) for key in
           ('routes', 'commitment_schedule', 'commitment_checks', 'decision_links')},
    }, ('title', 'summary', 'understanding', 'signature', 'days', 'decisions', 'preparation')),
}

WRITER_SCHEMA_RULES = """
你必须返回一个完整、有限的 JSON 方案对象，不是补丁，不套 plan/result 外壳，不带 Markdown。
保留未受本次修改影响的原字段、开放扩展字段和原文；schema 的 additionalProperties=true
表示允许扩展，并非允许删除旧字段。以下规则用于生成前自检，不能以程序归一化代替正确输出。

1. 顶层必填 title、summary（字符串）、understanding（对象列表）、signature（对象）、days、
   decisions、preparation（对象列表）。不能把 understanding 写成字符串/字符串列表/null，
   每项 text 必填字符串，source 可选字符串最长200；最多16项。新增或修改理解 kind=hypothesis，
   不伪造用户确认；原记录不变可完整保留其 kind/evidence。signature.sequence 必填字符串列表。
   不能只报告变更而省略完整方案。字段名、类型、上限详见下附 schema。
2. 普通文字最大1200字符（包括所有字符串列表元素）；特别上限：item.id=80，索引记录id=160，
   understanding.source/day.city=200，date=10，准备包summary/行动when/completion_condition/
   fallback/report_note/材料title/content=4000。所有列表必须符合 schema 的 maxItems。
   questions最多3；change_summary最多12；signature.sequence/supply_ids最多30。
3. days=1..14天，day为严格整数，按数组从1连续编号；若输入约定天数，保持约定。
   每天items=1..10项，休息日也写安排；item.id在全程非空且唯一。time和end_time必须是
   24小时 HH:MM，如09:00、17:30，禁止“上午”“9:00”“09:00-10:00”“24:00”。
   同日按time升序且不重叠，end_time必须严格晚于time，下一项time>=前项end_time。
   跨午夜活动拆入相邻两天，不能将次日凌晨作为本日结束时间；day.date若存在必须为有效
   YYYY-MM-DD日历日期，未知则省略。activity_kind只能rest/meal/activity/personal/transfer。
4. decisions最多24项，category必须覆盖flight/lodging/transport/dining/activities/packing/other
   七类（不是排他的类别枚举），缺资料写具体缺口。新/改决定status仅用proposed或needs_check。
   旧provided/兼容状态见schema，不借旧状态声称新决定已报价、核验或预订；程序会降为needs_check。
   preparation最多30项，status只可needs_check/user_action/not_applicable，不能标完成。
   signature/decisions/preparation.supply_ids最多30，均须来自输入catalog。
5. 新建action_guide用对象，仅可meeting_point、steps（最多8）、bring（最多12）、booking_note；
   不加链接键。旧null/文字/文字列表兼容保留，不能把未知对象串成文字。experience必须对象，
   what_to_do最多6；准备事项steps最多8、materials最多12；文本均遵守1200字符上限。
6. supplies若给出，最多200个对象，id非空唯一且存在catalog；每个被引用供给必须可在最终供给表找到。
   保留旧供给原证据；新增/修改供给只复制可信catalog事实，source_url必须公开HTTPS，image_url
   不能编造；price_status=unknown、availability_status=unknown、booking_status=not_booked。
7. 全文引用必须存在：related_item_ids/item_ids指向最终item.id；supply_ids/supply_id指向供给；
   source_ids/evidence_ids/source_id指向已接收可信来源；artifact_id指向journey.artifacts；
   item_id指向行程项；commitment_id指向user_commitments；anchor_id格式为已有承诺id:d实际天号。
   单引用可null，否则最长160字符；多引用为最多200项字符串列表（核心supply_ids仍最多30）。
   user_commitments最多200对象，id最长160非空唯一。可信供给/来源自身的provenance命名空间保留。
8. source_evidence及任意sources必须对象列表（各最多200，id非空唯一），不能生成来源或改写内容、
   核验状态；只能复制输入已接收sources、旧preparation_pack.sources、旧source_evidence、
   catalog.source_evidence/evidence中已有id及完全一致的字段值，不假装新增字段获得证据。
   输入sources上限300不表示输出来源列表可超过200。保留失败回执，不能将失败来源改成成功。
   journey.learning最多30对象；新增记录source必须是可信user_brief/user_text/user_feedback来源id，
   source_quote必须非空且逐字包含于其content，text必须等于完整content，不能伪造用户表达。
9. 任意url/link/*_url/*_urls中新添或变更位置的链接只能从catalog、可信sources或旧方案已有链接选取，
   且不得在blocked_urls内，不得引用404/410或blocked/unavailable来源。须安全公开HTTPS、
   无账号密码、仅443端口、非本机/内网，无私人账户/订单/敏感凭证参数及歧义分隔/控制字符。
   单链接最长8000；*_urls最多100项且每项最长1200。原位未改历史失败链接只是回执，
   不能搬到新展示位置当有效入口；field_evidence/evidence_boundary证据也不能伪造。
10. locks中的每个安排必须存在，保留完整item对象、day和date完全不变，包括transport等扩展字段；
    不能删除、换id、移动日期或改锁定内容。需改锁内容时说明用户须先解除锁定。
11. preparation_pack若存在必须对象，actions必填列表最多200；每项id最长160非空唯一、title必填，
    owner只agent/user；status只ready/needs_user/needs_research/user_reported/not_applicable。
    day可null，否则须是实际day整数。instructions/materials/assumptions各最多30字符串。
    包status只ready/partial。user_reported只能保留真实旧记录，不能新建或转移给同id的新任务。
12. journey若存在必须对象。artifacts最多200且id非空唯一，kind仅experience_guide/packing_list/
    inquiry_draft/route_note，title/content必填，status仅generated（默认）。tasks最多200，
    id非空唯一，title/kind/status必填字符串。completed仅限有对应artifact_id的digital_material，
    或原先已完成且授权内容保持不变的旧任务。physical_fulfillment只not_executed或原有值。
13. user_reported/外部completed保留时，除report_note/applicability注释外记录须完全不变，
    关联item_ids/related_item_ids/item_id（无id则其day）对应的日期、住宿和完整安排也须不变。
    依赖改变时将动作重新打开待确认，或仅作为历史回执保留并明确applicability=needs_reconfirmation；
    此标记只容许依赖改变，不能容许已办动作自身改写。不能凭相同id迁移完成事实。
14. readiness/delivery_truth必须对象；ready_to_depart若存在必须布尔，新值不能从非true升级true。
    routes/commitment_schedule/user_commitments/commitment_checks/decision_links各最多200对象。
    不因生成材料声称外部联络、预订、付款或旅行准备完毕。完整输出须在真实变更与摘要间一致；
    结构规则不能替代意图、修改范围与矛盾的独立critic审核。
15. summary、signature.why、day.rationale 中与本轮有关的过时说明须替换对应段落。
    允许有帮助的复述、判断和建议；避免无新增信息的整段重复，保留不同条件、适用日期、因果和细节。
    change_summary和revision_changes由程序按本轮真实差异重建，不复制上一轮记录，不自行编写。
""".strip() + '\n' + DETAIL_RULES

WRITER_SCHEMA_PROMPT = (WRITER_SCHEMA_RULES + '\n\n开放方案 JSON Schema（不关闭扩展字段）：\n'
                        + json.dumps(WRITER_PLAN_SCHEMA, ensure_ascii=False, separators=(',', ':')))
