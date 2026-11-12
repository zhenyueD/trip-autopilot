"""Coach-mode gate: when to speak, what to remember, and an auditable log.

Deterministic layer in front of the revision receipt. The model may only speak
inside a gap the user did not cover; an explicit user decision silences the
coach. Corrections the user already made become permanent per-trip checkpoints
("thread-crack memory"): we remember the pits stepped on, not preferences
invented by the model. This module owns no model calls and no persistence.
"""
from __future__ import annotations

import copy

MEMORY_VERSION = 1
_MAX_RECORDS = 20

# An explicit user decision downgrades the coach to execution for this
# revision and locks the matter against future re-suggestion.
EXECUTION_MODE_MARKERS = ('就这么定', '就这样定', '就这么办', '就这样办', '就按这个',
                          '按我说的', '听我的', '我决定了', '别劝', '不用劝',
                          '不用再建议', '别再建议', '别建议了', '不需要建议',
                          '不要再问', '别再问', '不用提醒', '别再提醒')

# Program-owned confirmation texts. A confirmation is not a suggestion: it
# carries no numbers, no citations, and must match one of these templates
# byte-for-byte so a model can never smuggle advice into the silent path.
CONFIRMATION_TEMPLATES = {
    'execution_mode': '你已明确拍板，本次按你的决定执行，未追加任何建议。',
    'no_gap': '这次修改都在你明确要求的范围内，按你的决定执行；没有发现需要提醒的缺口。',
}


def detect_execution_mode(feedback):
    """True when the user's own words close the matter."""
    return isinstance(feedback, str) and any(
        marker in feedback for marker in EXECUTION_MODE_MARKERS)


def normalize_memory(raw):
    """Per-trip coach memory, stored as a plain field on the trip record."""
    memory = {'version': MEMORY_VERSION, 'checkpoints': [], 'locked_decisions': []}
    if not isinstance(raw, dict):
        return memory
    for key in ('checkpoints', 'locked_decisions'):
        rows = raw.get(key)
        if isinstance(rows, list):
            memory[key] = [copy.deepcopy(row) for row in rows
                           if isinstance(row, dict) and isinstance(row.get('text'), str)
                           and row['text'].strip()][-_MAX_RECORDS:]
    return memory


def _append(memory, key, feedback, revision):
    row = {'text': feedback.strip()[:800], 'revision': revision}
    rows = [existing for existing in memory[key] if existing['text'] != row['text']]
    rows.append(row)
    memory[key] = rows[-_MAX_RECORDS:]
    return memory


def record_correction(memory, feedback, revision):
    """A user correction (fact/preference) becomes a permanent checkpoint."""
    return _append(memory, 'checkpoints', feedback, revision)


def record_locked_decision(memory, feedback, revision):
    """A '就这么定' decision is remembered so it is never re-suggested."""
    return _append(memory, 'locked_decisions', feedback, revision)


def memory_for_prompt(memory):
    """Compact projection handed to the receipt writer as data."""
    return {'checkpoints': [row['text'] for row in memory['checkpoints']],
            'locked_decisions': [row['text'] for row in memory['locked_decisions']]}


def prompt_block(memory):
    """Coach instruction for the receipt writer; memory travels as data."""
    return {
        'principle': '缺口驱动开口，指令驱动闭嘴',
        'memory_checkpoints': memory_for_prompt(memory)['checkpoints'],
        'locked_decisions': memory_for_prompt(memory)['locked_decisions'],
        'instruction': 'judgment只写用户反馈未提及、且有canonical_details硬证据支撑的缺口'
            '（如隐藏的交通/开放条件/冲突）；用户反馈已覆盖或已明确决定的领域只执行不评价；'
            'locked_decisions里的事项不再建议；memory_checkpoints是用户过去纠正过的坑，'
            '本次解释不得与之冲突。没有这样的缺口时judgment输出null、alternatives输出[]、'
            'next_action输出null。不要为了开口而开口。',
    }


def three_questions(*, execution_mode, opened, judgment):
    """Auditable self-check recorded on every receipt, spoken or silent."""
    citations = (judgment or {}).get('citations') or []
    if execution_mode:
        q1 = {'pass': False, 'reason': '用户已明确拍板，教练降级为执行，无信息增量空间'}
        q2 = {'pass': False, 'reason': '用户说出“就这么定”类指令，此时打断时机不成立'}
    elif opened:
        q1 = {'pass': True, 'reason': '判断指向用户反馈未提及的缺口，'
              + ('并以canonical来源逐字引用支持' if citations else '为纯定性判断，未含数字断言')}
        q2 = {'pass': True, 'reason': '回执是对用户主动提交修订的响应，非主动打断'}
    else:
        q1 = {'pass': False, 'reason': '用户指令已覆盖本次范围，或无硬证据支撑的缺口'}
        q2 = {'pass': True, 'reason': '回执是对用户主动提交修订的响应，但无可讲增量'}
    q3 = {'pass': True, 'reason': ('未开口，无猜错代价' if not opened else
          '开口内容经程序校验：数字逐字绑定canonical来源，kind固定suggestion，猜错代价用户可承受')}
    return {'opened': opened, 'q1_incremental': q1, 'q2_timing': q2, 'q3_tolerance': q3}
