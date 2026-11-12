"""Inspectable travel judgments, not medical thresholds or factual evidence."""
import copy

from .validation import PlanError


PRINCIPLES = (
    {'id': 'pace-intensity', 'applies_when': '用户要求放慢、减少疲劳或有明确节奏偏好',
     'advice': '评估已知步行时长、连续活动数、休息间隔和换乘负担；通过减项目、增加休息或缩短活动降低强度，不能只平移钟点。未知时长保持未知。'},
    {'id': 'older-rest-pacing', 'applies_when': '用户明确有年长同行者，结合其本人偏好和体力信息',
     'advice': '建议在连续活动之间留可退出的休息段；连续步行接近两小时可作为询问休息偏好的规划提示，不是适用于每位老人的健康上限。'},
    {'id': 'older-access-comfort', 'applies_when': '年长同行者希望少走路、少台阶或有明确行动需求',
     'advice': '优先讨论少台阶、较短步行和较少换乘方案；电梯、无障碍入口及路面情况须查证，不因年龄推断膝盖疾病或行动障碍。'},
    {'id': 'older-heat-comfort', 'applies_when': '年长同行且来源或用户描述提示炎热、暴晒',
     'advice': '可建议将正午至午后约12至15点的长时间户外活动移向室内或较凉时段；这是可调整的舒适度建议，不是天气事实或医疗安全阈值。'},
    {'id': 'older-transfer-rest', 'applies_when': '年长同行者面对长车程、频繁换车或体力负担',
     'advice': '长车程可建议约每一至两小时询问停靠休息需要，优先比较直达交通；包车仅作待核报价的选项，不自动推断必要或已安排。'},
)

JUDGMENT_RULES = '''
判断层可以提出建议、解释取舍、用自己的话复述；观点不需要伪造来源。
travel_principles 是可调整的舒适度建议，不是医学标准。必须先判断适用条件，年龄不等于疾病或行动障碍。
traveler_profile 来自本次旅行的用户偏好；延续已有值，只有用户明确新表达才建议更新candidate.traveler_profile。
不能把模型假设写成用户确认，不能以旧understanding自动覆盖用户编辑后的profile。
放慢意味着评估强度：已知步行时长、连续活动数、休息间隔、换乘负担；未知数字标未知。
结果应包含减项目、缩短活动、增加休息或减少步行/换乘之一，说明保留与放弃的取舍；只整体平移时间不够。
先比较原安排与新安排的实际负担，再选择有效的调整。给原有空档起名不等于新增可用休息；
收拾、退房、候车、候船和在途不是可支配休息。休息须有可使用的地点、连续时段和前后衔接，
不能在退房后默认仍能用原客房。若保留的长活动、长途交通和晚间转场仍连在一起，应比较缩短或舍弃
非核心活动、直达住宿等取舍；把保留体验与放弃内容说清楚，不为凑休息文字继续保留原来的赶路负担。
检查本次相关行程的交通耗时与时间格是否一致；不够时调整安排或明确限制，不用模糊的缓冲掩盖冲突。
输出 reasoning 为简短可审查的决策摘要（intent、principles_applied、tradeoffs），不是隐藏思维链或逐步内部思考。
principles_applied列适用原则id，tradeoffs说明强度观察、信息缺口和方案取舍，不诊断疾病。
输出 suggestions 最多2条主动建议；没有实际帮助可为空。字段 id,title,advice,reason,principle_ids,proposed_feedback。
advice明确以“我建议”等观点表达；proposed_feedback是用户可选择采纳的修改请求，未采纳绝不自动执行。
数据层仍须守住：价格、营业/班次时间、电话、URL有canonical来源才引用；无来源写未核实，不以“经验”捏造数字。
拟定的行程钟点是规划建议，不能冒充营业时间/实际班次。reasoning和suggestions同样受此事实边界约束。
'''

REASONING_SCHEMA = {'anyOf': [{'type': 'string', 'maxLength': 1200}, {
    'type': 'object', 'required': ['intent', 'principles_applied', 'tradeoffs'],
    'properties': {'intent': {'type': 'string', 'maxLength': 400},
        'principles_applied': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'maxLength': 160}},
        'tradeoffs': {'type': 'string', 'maxLength': 1200}}, 'additionalProperties': False}]}
SUGGESTIONS_SCHEMA = {'type': 'array', 'maxItems': 2, 'items': {'type': 'object',
    'required': ['id', 'title', 'advice', 'reason', 'principle_ids', 'proposed_feedback'],
    'properties': {**{key: {'type': 'string', 'maxLength': 800} for key in
        ('id', 'title', 'advice', 'reason', 'proposed_feedback')},
        'principle_ids': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'maxLength': 160}}},
    'additionalProperties': False}}


def judgment_context(context):
    return {'traveler_profile': copy.deepcopy(context.get('traveler_profile') or {}),
            'travel_principles': copy.deepcopy(list(PRINCIPLES)),
            'request_constraints': copy.deepcopy(context.get('request_constraints') or {})}


def writer_metadata(raw):
    """Validate optional legacy-compatible summaries; never store provider CoT."""
    result = {}
    if 'reasoning' in raw:
        reasoning = raw['reasoning']
        valid = isinstance(reasoning, str) and 0 < len(reasoning) <= 1200
        if isinstance(reasoning, dict):
            valid = (set(reasoning) == {'intent', 'principles_applied', 'tradeoffs'}
                and _text(reasoning['intent'], 400) and _text(reasoning['tradeoffs'], 1200)
                and _principle_ids(reasoning['principles_applied']))
        if not valid:
            raise PlanError('reasoning必须为简短意图、原则适用与取舍摘要')
        result['reasoning'] = copy.deepcopy(reasoning)
    if 'suggestions' in raw:
        suggestions = raw['suggestions']
        if not isinstance(suggestions, list) or len(suggestions) > 2:
            raise PlanError('suggestions最多2条建议')
        seen = set()
        fields = {'id', 'title', 'advice', 'reason', 'principle_ids', 'proposed_feedback'}
        for suggestion in suggestions:
            if (not isinstance(suggestion, dict) or set(suggestion) != fields
                    or not all(_text(suggestion[key], 800) for key in fields - {'principle_ids'})
                    or not _principle_ids(suggestion['principle_ids']) or suggestion['id'] in seen):
                raise PlanError('建议字段无效、原则不明或id重复')
            seen.add(suggestion['id'])
        result['suggestions'] = copy.deepcopy(suggestions)
    return result


def _text(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _principle_ids(value):
    known = {principle['id'] for principle in PRINCIPLES}
    return isinstance(value, list) and len(value) <= 8 and all(isinstance(p, str) and p in known for p in value)


def attach_metadata(candidate, metadata, usage, row):
    if 'reasoning' in metadata:
        usage['writer_reasoning'] = copy.deepcopy(metadata['reasoning'])
        row['writer_reasoning'] = copy.deepcopy(metadata['reasoning'])
    if 'suggestions' in metadata:
        candidate['suggestions'] = copy.deepcopy(metadata['suggestions'])
    return candidate
