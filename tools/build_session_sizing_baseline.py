"""Offline sizing projection; a censored response is not a complete sample."""
from copy import deepcopy
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.travel_agent.single_session_agent import DESTINATION_CONTEXT_SPEC, MODEL
from packages.travel_agent.writer_tokenizer import count_writer_text

SOURCE = Path('research/session-frontend-2026-11-04/api-sessions/e0b80dd0378374ffea48fdb3a773ada9/session')
STAGE = 'single_session_generation'


def build():
    response_path = SOURCE / 'model-artifacts/model-012-response.json'
    receipt = json.loads((ROOT / response_path).read_text())
    data = json.loads(receipt['body_utf8'])
    choice = data['choices'][0]
    assert choice['finish_reason'] == 'length'
    usage = data['usage']
    reasoning = usage['completion_tokens_details']['reasoning_tokens']
    arguments = choice['message']['tool_calls'][0]['function']['arguments']
    # The recorded JSON string ends mid-day, without an incomplete escape.
    assert arguments.startswith('{"markdown": "')
    partial = json.loads('"' + arguments[len('{"markdown": "'):] + '"')
    prefix, block = partial.split('```destination-detail-v1\n', 1)
    remaining = block.split('"days":[', 1)[1]
    days = []
    while remaining:
        try:
            day, end = json.JSONDecoder().raw_decode(remaining)
        except json.JSONDecodeError:
            break
        days.append(day)
        remaining = remaining[end:].lstrip(',')
    assert len(days) == 4, 'Reinspect source layout before changing projection'
    intake = json.loads((ROOT / SOURCE / 'conversation.json').read_text())['intake']
    count = (date.fromisoformat(intake['end_date']) - date.fromisoformat(intake['start_date'])).days + 1
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    tokens = lambda text: count_writer_text(text, model=MODEL)
    sizes = [tokens(encode(day)) for day in days]
    largest = days[sizes.index(max(sizes))]
    overview_line = next(line for line in DESTINATION_CONTEXT_SPEC.splitlines()
                         if line.startswith('{"season_note"'))
    overview = json.loads(overview_line)
    projected = {'version': 1, 'overview': overview, 'days': []}
    for index in range(count):
        day = deepcopy(largest)
        day['id'] = f'day-{index + 1}'
        projected['days'].append(day)
    markdown = prefix + '```destination-detail-v1\n' + encode(projected) + '\n```\n'
    # Count escaped tool arguments, not merely rendered Markdown.
    visible = tokens(encode({'markdown': markdown})) + tokens(choice['message'].get('content') or '')
    observed_visible = usage['completion_tokens'] - reasoning
    demand = reasoning + max(visible, observed_visible)
    return {'stage': STAGE, 'measurement_status': 'censored_observation_plus_projection',
            'physical_calls': 1, 'censored_calls': 1,
            'recommended_max_tokens': math.ceil(demand * 1.25 / 1000) * 1000,
            'basis': 'ceil((observed_reasoning + max(observed_visible, projected_visible)) * 1.25 / 1000) * 1000',
            'margin_fraction': 0.25, 'round_up_multiple': 1000,
            'reasoning_tokens': reasoning, 'observed_visible_tokens': observed_visible,
            'projected_visible_tokens': visible, 'projected_days': count,
            'complete_day_token_counts': sizes,
            'projection': 'Existing prose + six copies of largest complete day + current overview example; escaped save arguments. Projection only, not a validated handbook or provider billing.',
            'uncertainty': 'One censored sample; 25% margin covers variation and tool framing but cannot guarantee arbitrary future output fits.',
            'source_response': str(response_path),
            'source_sha256': hashlib.sha256((ROOT / response_path).read_bytes()).hexdigest(),
            'finish_reason': choice['finish_reason'], 'http_status': receipt['http_status'],
            'elapsed_ms': receipt['elapsed_ms'], 'product_calls': 0}


if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False, indent=2))
