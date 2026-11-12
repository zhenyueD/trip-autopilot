"""Small versioned method documents, selected by the model and loaded read-only."""
import copy

from .store import StateError


_METHODS = {
    'clarify_needs': dict(title='需求追问', summary='仅追问会改变安排的未知条件。',
        tools=['read_trip', 'request_input'], document=(
            '先读已知条件、用户纠正和已有回答。判断哪个未知答案会改变实际选择；'
            '能合理推进则明确假设，不强制提问。确需选择时给2–3个有实际差别的选项，'
            '说明代价并允许自由回答。等待回答时推进不依赖它的工作。')),
    'choose_attractions': dict(title='景点取舍', summary='比较同一时间段的候选、代价与可减项。',
        tools=['read_trip', 'propose_revision'], document=(
            '围绕用户目标比较候选的体验、门到门时间、费用和体力代价。给出推荐、'
            '放弃原因及可执行备选；只查会改变选择的证据，不按热度替用户决定。'
            '通过既有候选工具形成安排，保留已订承诺和必要回程。')),
    'fit_companions': dict(title='同行人适配', summary='把实际体力、兴趣与休息需要变成具体安排。',
        tools=['read_trip', 'propose_revision'], document=(
            '使用用户表达的步行能力、节奏、兴趣和最新纠正，不从年龄自动推断能力。'
            '定位冲突的步行、等候、活动和休息段，提出具体增减或交通替代，'
            '说明收益与代价。局部变化通过候选及回执体现，不改无关承诺。')),
    'pace_routes': dict(title='路线节奏', summary='组织能走通的门到门片段与缓冲。',
        tools=['read_trip', 'read_source', 'propose_revision'], document=(
            '从酒店出发到返回酒店逐段检查出入口、交通模式、步行、等候、换乘、'
            '缓冲及预约/末班边界。区分车上与门到门耗时，驾车估时不能当公共交通。'
            '可靠原文不足时给交通主方案及有条件估计，不编线路、站名或时刻。'
            '保留可减项、准备清单和实际可用的导航入口。')),
    'gather_evidence': dict(title='取证', summary='为影响决定的事实找适用原文并保留冲突。',
        tools=['read_source', 'research_fact'], document=(
            '定义会改变决定的实体、日期及条件，再选择可用来源和有帮助的原文语言。'
            '读取原文并保存定位，区分用户表达、官方来源、二手经验及推断。'
            '检查同名实体、适用日期/条件和来源独立性；翻译、转载不是独立证据。'
            '引文存在只证明可定位，语义支持另行判断。缺证据、冲突、过期保留未知，'
            '不把公开搜索授权扩大到私人材料或未授权实体。')),
    'recover_changes': dict(title='变化恢复', summary='按受影响决定局部修订并核对真实回执。',
        tools=['read_trip', 'propose_revision', 'check_constraints'], document=(
            '确认新条件或材料撤回影响哪些决定、交通及后续承诺，保留无关安排。'
            '提出替代与理由，使用既有 scoped revision、版本检查、应用/撤销回执。'
            '依赖不全时说明范围，不能静默全量重写。失败保留旧方案，结果未知先对账。'
            '说明具体修改、判断与下一步；已建议、已应用、外部已办理分别表达。')),
}
VERSION = '1'


def method_catalog(available_tools):
    available = set(available_tools)
    if 'load_method' not in available:
        return []
    return [dict(method_id=name, version=VERSION, title=value['title'], summary=value['summary'])
            for name, value in _METHODS.items() if set(value['tools']) <= available]


def load_method(method_id, version, available_tools):
    allowed = {row['method_id'] for row in method_catalog(available_tools)}
    if method_id not in allowed:
        raise StateError('METHOD_UNAVAILABLE', '方法依赖的工具未注册或未获授权', 409)
    if version != VERSION:
        raise StateError('METHOD_VERSION_UNAVAILABLE', '请使用当前目录中的方法版本', 409)
    return dict(method_id=method_id, version=VERSION, optional=True, read_only=True,
                **copy.deepcopy(_METHODS[method_id]))
