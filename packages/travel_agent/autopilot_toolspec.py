"""Small public tool contracts. Identity and authority never come from arguments."""
import copy
import json
import math

from .model_boundary import normalize_json_response


def string(limit=4000, **extra):
    return dict(type='string', minLength=1, maxLength=limit, **extra)


def array(items, maximum=30):
    return dict(type='array', items=items, maxItems=maximum)


def obj(properties, required=()):
    return dict(type='object', properties=properties, required=list(required), additionalProperties=False)


def nullable(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


ID = string(128)
NUMBER = dict(type='number', minimum=0)
INTEGER = dict(type='integer', minimum=0)
SOURCE = obj({'source_id': ID, 'fingerprint': string(64)}, ['source_id', 'fingerprint'])
ARTIFACT = obj({'kind': string(32, enum=['source', 'input', 'work', 'revision']), 'id': ID,
                'fingerprint': string(64)}, ['kind', 'id'])
REQUEST = obj({'instruction': string(8000), 'day_ids': array(dict(type='integer', minimum=1)),
               'source_ids': array(ID)}, ['instruction'])
ADVICE_CLAIM = obj({'claim_id': ID, 'text': string(2000),
    'status': string(16, enum=['asserted', 'conditional', 'unknown']), 'source_ids': array(ID, 20)},
    ['claim_id', 'text', 'status', 'source_ids'])
ADVICE = obj({'text': string(8000), 'claims': array(ADVICE_CLAIM, 20),
    'source_ids': array(ID, 20), 'unknowns': array(string(1000), 20)},
    ['text', 'claims', 'source_ids', 'unknowns'])

# A contract is advertised only after its real implementation is registered.
_SPECS = {
    'load_method': ('按需只读加载目录中的方法版本；可跳过，不执行固定流程或修改旅行。', obj({
        'method_id': string(64), 'version': string(32)}, ['method_id', 'version'])),
    'read_trip': ('读取当前旅行、承诺、锁和工作状态；仅按需取相关日期。', obj({
        'scope': string(20, enum=['summary', 'full', 'days']),
        'day_ids': array(dict(type='integer', minimum=1)), 'fields': array(string(64))})),
    'read_source': ('读取可见来源原文及定位；未找到不等于事实不存在。', obj({
        'source_id': ID, 'start': INTEGER, 'end': nullable(INTEGER)}, ['source_id'])),
    'research_fact': ('保留原问题及其实体、日期、时区和条件取得原文证据；可选原文语言，不发送整份行程。'
        'topic 仅辅助寻找来源，不得替代 question 或添加无关目标；地址、地铁站、步行距离使用 location_access。'
        'timezone 仅在已知且相关时填写，不推断缺失时区。'
        '公开搜索每次调用只研究一个授权枚举名称，多个实体用多个调用，不拼接名称或增加别名。'
        '非空 source_ids 表示读取已授权材料，此时 entity 可描述材料中的对象，不受公开搜索枚举限制。', obj({
        'question': string(2000), 'entity': string(500), 'date': string(100), 'timezone': string(100),
        'conditions': array(string(500)), 'source_ids': array(ID),
        'source_languages': array(string(32), 3),
        'topic': string(64, enum=['official_entry', 'ticketing', 'allergy_policy', 'accessibility',
            'transport', 'transport_fare', 'transport_booking_channel', 'transport_pickup_point',
            'transport_timetable', 'lodging_policy', 'location_access', 'weather', 'health_guidance'])}, ['question', 'entity'])),
    'check_constraints': ('独立复核当前或候选方案；也可提交单条 advice 精确文字、逐条声明、来源及未知。'
        'advice 与 proposal_id/source_ids 互斥，来源列在 advice 内；声明 status 是待审表达，不是事实核验结果。'
        '单条建议 pass 不表示整趟目标完成。缺证保留 unknown，按回执原文交付，不得改写后复用。', obj({
        'proposal_id': nullable(ID), 'source_ids': array(ID), 'advice': ADVICE})),
    'propose_revision': ('形成经过真实校验的候选方案；这一步尚未应用。', obj({
        'expected_revision': dict(type='integer', minimum=1), 'request': REQUEST}, ['expected_revision', 'request'])),
    'apply_revision': ('按已有授权原子应用指定候选；提交时重新检查版本和证据。', obj({
        'proposal_id': ID, 'expected_revision': dict(type='integer', minimum=1)}, ['proposal_id', 'expected_revision'])),
    'undo_revision': ('撤销指定应用且保留后来编辑；冲突时不强行覆盖。', obj({
        'revision_id': ID, 'expected_revision': dict(type='integer', minimum=1)}, ['revision_id', 'expected_revision'])),
    'update_work': ('记录真实未完成工作；完成必须引用已存在的结果。', obj({
        'title': string(1000), 'status': string(32, enum=['pending', 'in_progress', 'waiting', 'completed', 'cancelled']),
        'work_id': nullable(ID), 'artifact_refs': array(ARTIFACT), 'source_refs': array(SOURCE)}, ['title'])),
    'request_input': ('保存必要问题；可给2–3个简短选项说明取舍，用户仍可自由回答。', obj({
        'question': string(2000), 'key': ID, 'reason': string(2000),
        'options': array(string(200), 3)}, ['question', 'key', 'reason'])),
    'retrieve_memory': ('检索当前旅行内仍适用的纠正和记忆；不混用其他主体。', obj({
        'query': dict(type='string', maxLength=1000), 'scope': string(32, enum=['trip']), 'as_of': nullable(NUMBER)})),
    'propose_memory': ('提出有来源的记忆候选，不自动升级为永久偏好或生产规则。', obj({
        'content': string(4000), 'kind': string(32, enum=['inference', 'observation']),
        'source_refs': array(SOURCE), 'expires_at': nullable(NUMBER)}, ['content'])),
    'schedule_check': ('建立实际持久检查作业；成功回执包含job_id后才可承诺。', obj({
        'due_at': NUMBER, 'purpose': string(2000), 'key': ID, 'source_refs': array(SOURCE)}, ['due_at', 'purpose', 'key'])),
    'delegate': ('按需取证、反例检查或材料整理；结果是证据/提案，不是正式写入。', obj({
        'task_type': string(32, enum=['evidence', 'counterexample', 'materials']),
        'instruction': string(4000), 'source_ids': array(ID)}, ['task_type', 'instruction'])),
}


def specification(name, *, goal=None):
    description, schema = _SPECS[name]
    schema = copy.deepcopy(schema)
    if name == 'research_fact' and goal is not None:
        allowed = goal.get('constraints', {}).get('public_search_entities', [])
        entities = list(dict.fromkeys(entity.strip() for entity in allowed
            if isinstance(entity, str) and entity.strip())) if isinstance(allowed, list) else []
        if entities:
            # A nonempty admitted-source route is separate from public discovery.
            # Bindings remain the authority for names and source visibility.
            schema['anyOf'] = [
                {'properties': {'entity': {'enum': entities}, 'source_ids': {'maxItems': 0}}},
                {'properties': {'source_ids': {'minItems': 1}}, 'required': ['source_ids']},
            ]
        else:
            schema['required'].append('source_ids')
            schema['properties']['source_ids']['minItems'] = 1
    return {'type': 'function', 'function': {'name': name, 'description': description,
                                            'parameters': schema}}


def _validate(value, schema):
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try:
                _validate(value, option)
                return
            except ValueError:
                pass
        raise ValueError('type')
    kind = schema['type']
    if kind == 'null' and value is None:
        return
    if kind == 'object' and isinstance(value, dict):
        if set(value) - set(schema['properties']) or set(schema['required']) - set(value):
            raise ValueError('properties')
        for key, item in value.items():
            _validate(item, schema['properties'][key])
        return
    if kind == 'array' and isinstance(value, list) and len(value) <= schema['maxItems']:
        for item in value:
            _validate(item, schema['items'])
        return
    if kind == 'string' and isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema['maxLength']:
            raise ValueError('length')
        if 'enum' in schema and value not in schema['enum']:
            raise ValueError('enum')
        return
    if kind in {'integer', 'number'} and type(value) in (int, float):
        if kind == 'integer' and type(value) is not int:
            raise ValueError('integer')
        if math.isfinite(value) and value >= schema.get('minimum', 0):
            return
    raise ValueError('type')


def parse_arguments(name, raw, *, request_id=None):
    if not isinstance(raw, str) or len(raw.encode()) > 64000:
        raise ValueError('arguments')
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
    value = normalize_json_response(value, _SPECS[name][1],
                                    {'provider_request_id': request_id} if request_id else {})
    _validate(value, _SPECS[name][1])
    return value
