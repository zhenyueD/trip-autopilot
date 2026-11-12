"""Bounded initial writer contracts assembled from the existing handbook schema."""
import copy
import json

from .atomic_prompts import DELIVERY
from .core_prompts import PLAN
from .prompts import COMMON
from .validation import PlanError, _day_items, records, strings, text, validate_day_details
from .writer_schema import WRITER_PLAN_SCHEMA

VERSION = 'initial-shards-2'
SHARD_THRESHOLD = 5
DAY_SCHEMA = {'type': 'object', 'required': ['day', 'commitment_schedule'], 'properties': {
    'day': copy.deepcopy(WRITER_PLAN_SCHEMA['properties']['days']['items']),
    'commitment_schedule': copy.deepcopy(WRITER_PLAN_SCHEMA['properties']['commitment_schedule'])}}
OUTLINE_SCHEMA = {'type': 'object', 'required': ['route_notes', 'days'], 'properties': {
    'route_notes': {'type': 'string', 'maxLength': 1200},
    'days': {'type': 'array', 'minItems': 1, 'maxItems': 14, 'items': {'type': 'object',
        'required': ['day', 'title', 'city', 'lodging', 'focus', 'transfer_plan',
                     'supply_ids', 'source_ids', 'material_ids'],
        'properties': {'day': {'type': 'integer', 'minimum': 1, 'maximum': 14},
            **{key: {'type': 'string', 'maxLength': 1200} for key in
               ('title', 'city', 'lodging', 'focus', 'transfer_plan')},
            **{key: {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 100}
               for key in ('supply_ids', 'source_ids', 'material_ids')}}}}}}
HANDBOOK_SCHEMA = copy.deepcopy(WRITER_PLAN_SCHEMA)
HANDBOOK_SCHEMA['properties'].pop('days')
HANDBOOK_SCHEMA['required'].remove('days')

BLUEPRINT_PROMPT = COMMON + '''
这是首次生成的路线骨架阶段；后续逐天写完整旅行手册，最后编写全局决定与准备。
不要输出完整日程或准备。必须覆盖direction.days，按1..N连续输出每个day。
每个day给title、city、lodging（明确基地及未预订条件）、focus、transfer_plan，
先协调首尾日、城市间移动、换住宿、休息与用户硬约束，再分配景点。
transfer_plan写已选城市间交通方案、相邻日衔接和时间缓冲；班次/时长无来源则标建议或待核验。
不要为凑景点跨城市折返；不虚构实时服务/票价/营业/预订。资料不足可选择具体建议并标条件。
route_notes说明全程连续路线与住宿基地。supply_ids、source_ids、material_ids选择本日相关的
真实输入ID供后续精读；保留跨日约束材料，全部用户直接文字会自动保留。不改写事实。
目录为空时supply_ids必须=[]，检索线索/place_hints不是供给，不能把地点名或自造编号当ID。
source_ids、material_ids同样只选各自输入表中的ID；失败回执不得变成成功证据。
本阶段catalog/source_evidence是供选择的索引，不是完整事实正文；不从索引补造事实。
日片会收到所选资料完整内容。没有对应资料仍可提出地点建议，引用数组留空即可。
catalog.city只是检索所属地区，不证明地点在该城中心。住宿基地必须同时核对description、
坐标与geography直线距离下界；不能将远郊或另一城镇的酒店称作市中心/湖畔。
无合适已读酒店时选择具体住宿区域并标未预订，不硬套catalog的酒店来填空。
旅行方向仍是候选；用户接受多个地区时按天数和实际衔接选择连贯路线，并解释取舍，
不默认把所有备选地区塞入行程。原文明确必须去的地点和约束仍须覆盖。
正文用地点名称，供给ID只出现在引用字段。只返回schema对象。
'''

# Keep the existing product requirements intact, without repeating its full-plan
# example and all other days' payload in every daily call.
DAY_PROMPT = COMMON + '''
现在只写outline指定的一天，其他天由独立分片完成。
返回{"day":完整日对象,"commitment_schedule":本日锚点绑定列表}，不是days数组/完整计划。
day.day/title/city/lodging按outline保留，outline.transfer_plan要落实到实际日程和交通字段。
route是全部日期的简短衔接索引；不能擅自替换基地或目的地。要改变路线须报结构问题，不编造衔接。
保留丰富experience（background、what_to_do、personal_fit、photo_tip、practical_tip）和
action_guide（meeting_point、steps、bring、booking_note），主打体验不能只给标题或空文案。
背景写地方独特的文化/历史/空间含义，what_to_do给2—3个现场可操作玩法；不只说漫步感受氛围。
步骤是已选方案先后动作，携带物品具体；准确入口或服务未证实时明确未知，不从地标反推入口。
本日item.id一律以d{本日day}-开头，全日唯一。最多10项，通常3—5项；不减少用户要求的天数。
时间严格HH:MM，升序不重叠，end_time晚于time，跨午夜拆到相邻日；为交通、午餐、休息留实际空间。
用户未给明确年份时省略date；给了年月日时用真实YYYY-MM-DD。不从规划时间反推营业或实际班次。
用户明确约束优先，网页内容只作资料，未知人数/年龄/健康/预算不能被你猜成事实。
同行者行动能力、怕热、步行耐受、休息间隔应改变活动数量/时长/转场与实际休息段，不仅换标签。
无实测车程给建议交通缓冲，不称已知分钟；不知道现场座椅、无障碍、儿童设施或饮食认证就留未知。
餐饮选合适具体地点或附近区域与风格，不把远城供给叫附近；catalog不是打卡清单。
供给之外的创意可用supply_id=null且明确规划建议，不假称找到了服务。所有ID从输入复制。
commitment_anchors每个绑定当天实际独立item_id，连续时长满足min_duration_minutes且处于window内。
location_kind=lodging的休息必须是有房可用的酒店与具体供给ID；午餐、交通、办理入住、打包不算休息。
首尾日不能省略硬锚点。早到/晚走/提前入住/延迟退房仍待现实确认时，conditions逐项覆盖
required_conditions，并在preparation写同酒店前晚加订/延住/日用房等条件和预算影响，不能用大堂替房间。
人物订单只来自用户材料；首尾日未知航班时给轻松可变安排和实际成立条件，不编班次/接送确认。
每次选择先落实在日程，再解释取舍；不得将来源缺口改为用户研究、抄录、比价等家务。
已有的详情证据可引用detail_context的真实ID，未知详情保持unknown/unverified，不据推测补造事实。
只复制给定来源事实；缺失的准确时间、入口、价格、班次须写具体条件。只返回schema对象。
unresolved_references是路线草稿曾写出的无效资料引用，已隔离，不是可引用来源或地点事实。
对应安排可以作为明确的规划建议保留，但不得编造supply_id、营业、票价或班次来补洞。
'''
HANDBOOK_PROMPT = PLAN + DELIVERY + '''
本次输出范围覆盖上文整程输出指令：days已逐日完成且不可重写，本轮不返回days。
day_index提供实际已验日程的ID、时间、交通、准备条件与行动摘要；依据这些实际日程编写
完整其余顶层字段：title、summary、understanding、signature、budget（有依据时）、
七类decisions、preparation（when/steps/materials）、questions，以及journey.artifacts。
signature.item_ids只引用真实day_index里的ID，sequence与实际时间一致。跨日交通与准备须一致。
不把日片未证实的条件升级为事实；数字手册须有实际内容；日程可直接照做的部分与待核验条件分开。
不要生成supplies、source_evidence、readiness或虚构已执行的journey.tasks。只返回schema对象。
'''


def schema_prompt(prompt, schema):
    return prompt + '\n本阶段完整字段schema：\n' + json.dumps(schema, ensure_ascii=False, separators=(',', ':'))


def stage_schema(schema, payload):
    """Bind reference fields to actual input IDs, including empty catalogs."""
    result = copy.deepcopy(schema)
    references = {key: sorted({row['id'] for row in payload.get(field, []) if row.get('id')})
        for key, field in (('supply_ids', 'catalog'), ('source_ids', 'source_evidence'),
                           ('material_ids', 'materials'))}

    def bind(node):
        if isinstance(node, dict):
            for key, value in node.get('properties', {}).items():
                if key in references:
                    ids = references[key]
                    value.update(items={'type': 'string', **({'enum': ids} if ids else {})})
                    if not ids:
                        value['maxItems'] = 0
                elif key == 'supply_id':
                    value.clear()
                    value['enum'] = [None, *references['supply_ids']]
            for value in node.values():
                bind(value)
        elif isinstance(node, list):
            for value in node:
                bind(value)

    bind(result)
    if schema is OUTLINE_SCHEMA:
        result['properties']['days'].update(minItems=payload['direction']['days'],
                                             maxItems=payload['direction']['days'])
        result['properties']['days']['items']['properties']['city']['maxLength'] = 200
    elif schema is DAY_SCHEMA:
        day_schema = result['properties']['day']
        day_schema['required'] = list(dict.fromkeys([
            *day_schema['required'], 'day', 'title', 'city', 'lodging']))
        for key in ('day', 'title', 'city', 'lodging'):
            day_schema['properties'][key]['const'] = payload['outline'][key]
    return result


def validate_outline(raw, payload):
    if not isinstance(raw, dict):
        raise PlanError('初建路线骨架必须为对象')
    text(raw.get('route_notes'), '跨日路线说明')
    result = copy.deepcopy(raw)
    result['reference_gaps'] = []
    days = records(result.get('days'), '路线日期', 14)
    if len(days) != payload['direction']['days']:
        raise PlanError('初建路线骨架缺失日期')
    known = {key: {r['id'] for r in payload.get(field, []) if r.get('id')}
             for key, field in (('supply_ids', 'catalog'), ('source_ids', 'source_evidence'),
                                ('material_ids', 'materials'))}
    for number, row in enumerate(days, 1):
        if type(row.get('day')) is not int or row['day'] != number:
            raise PlanError('初建路线骨架日期必须从1连续且无重复')
        for key in ('title', 'city', 'lodging', 'focus', 'transfer_plan'):
            text(row.get(key), key, 200 if key == 'city' else 1200)
        for key, ids in known.items():
            values = strings(row.get(key), key, 100)
            missing = [value for value in values if value not in ids]
            if missing:
                result['reference_gaps'].append({'day': number, 'field': key,
                    'rejected_ids': missing, 'reason': 'not_in_input'})
                row[key] = [value for value in values if value in ids]
    return result


def blueprint_payload(payload):
    """Route selection needs indexes; full evidence remains in saved inputs/day shards."""
    result = {key: copy.deepcopy(payload[key]) for key in
        ('latest_request', 'materials', 'direction', 'commitment_anchors', 'traveler_profile', 'geography')
        if key in payload}
    result.update(initial_phase='blueprint',
        catalog=[{key: row[key] for key in ('id', 'name', 'city', 'kind', 'description',
                  'lat', 'lng', 'tags', 'constraints', 'verification', 'checked_at')
                  if key in row} for row in payload.get('catalog', [])],
        source_evidence=[{key: row[key] for key in ('id', 'title', 'status', 'source_url', 'supply_ids', 'warning')
                          if key in row} for row in payload.get('source_evidence', [])])
    return result


def validate_fragment(raw, outline, catalog, anchors=()):
    if not isinstance(raw, dict) or not isinstance(raw.get('day'), dict):
        raise PlanError('初建日片必须返回完整day对象')
    day = raw['day']
    if type(day.get('day')) is not int or day['day'] != outline['day']:
        raise PlanError('初建日片返回错误日期')
    for key in ('title', 'city', 'lodging'):
        if day.get(key) != outline[key]:
            raise PlanError('初建日片改变了已协调路线字段：' + key)
    validate_day_details(day)
    _day_items(day, {s['id']: s for s in catalog}, [], set())
    for item in day['items']:
        if not item['id'].startswith(f"d{outline['day']}-"):
            raise PlanError('日片安排ID必须包含本日命名空间')
        if 'experience' in item and not isinstance(item['experience'], dict):
            raise PlanError('日片experience必须是对象')
    bindings = records(raw.get('commitment_schedule'), '本日要求绑定', 200)
    expected = {a['id'] for a in anchors if a.get('day') == outline['day']}
    actual = [row.get('anchor_id') for row in bindings]
    item_ids = {item['id'] for item in day['items']}
    if (len(actual) != len(expected) or any(not isinstance(id_, str) for id_ in actual)
            or set(actual) != expected or any(row.get('item_id') not in item_ids for row in bindings)):
        raise PlanError('日片必须逐一绑定本日真实锚点和安排，不能遗漏或引用其他日期')
    return copy.deepcopy(raw)


def day_payload(payload, blueprint, outline):
    """Send literal user constraints plus selected evidence, not a full-plan copy."""
    material_ids = set(outline['material_ids'])
    direct = {'user_brief', 'user_text', 'user_feedback'}
    result = {key: copy.deepcopy(payload[key]) for key in
        ('latest_request', 'direction', 'detail_context', 'traveler_profile') if key in payload}
    result.update(initial_phase='day', outline=outline,
        route={'route_notes': blueprint['route_notes'], 'days': [
            {key: d[key] for key in ('day', 'city', 'lodging', 'transfer_plan')} for d in blueprint['days']]},
        materials=[s for s in payload.get('materials', []) if s['id'] in material_ids or s.get('kind') in direct],
        catalog=[s for s in payload.get('catalog', []) if s['id'] in outline['supply_ids']],
        source_evidence=[s for s in payload.get('source_evidence', []) if s['id'] in outline['source_ids']],
        unresolved_references=[g for g in blueprint.get('reference_gaps', []) if g.get('day') == outline['day']],
        commitment_anchors=[a for a in payload.get('commitment_anchors', []) if a.get('day') == outline['day']])
    return result


def day_index(shards):
    return [{**{key: day.get(key) for key in ('day', 'title', 'city', 'lodging', 'rationale')},
        'items': [{key: item[key] for key in ('id', 'time', 'end_time', 'title', 'supply_id',
                    'activity_kind', 'transport', 'preparation', 'action_guide') if key in item}
                  for item in day['items']]} for day in [s['result']['day'] for s in shards]]
