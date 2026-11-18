"""Explicit synthetic budgets for tests of unrelated tool/transport behavior."""
import json
from packages.travel_agent.single_session_detail import BLOCK


def with_budget(markdown):
    match = BLOCK.search(markdown)
    data = json.loads(match[1]) if match else {
        'version': 1, 'days': [{'id': 'day-1', 'title': '离线一天',
            'summary': '只验证工具流程', 'stops': [{'id': 'test', 'name': '合成站点'}]}]}
    data.setdefault('overview', {})['budget_totals'] = {
        'currency': 'CNY', 'tiers': [
            {'name': name, 'verified_items': [], 'estimated_items': [
                {'label': '合成住宿', 'unit_amount': amount,
                 'factors': [{'label': '晚', 'quantity': '2'}, {'label': '间', 'quantity': '1'}],
                 'assumption': '离线合成金额，不代表任何旅行报价'}], 'excluded': []}
            for name, amount in [('测试甲', '300'), ('测试乙', '500')]]}
    block = '```destination-detail-v1\n' + json.dumps(data, ensure_ascii=False) + '\n```'
    if match:
        return markdown[:match.start()] + block + markdown[match.end():]
    return markdown + '\n\n' + block
