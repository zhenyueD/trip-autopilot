"""Thin service: model judges; store owns publication and budget boundaries."""
from __future__ import annotations

from .comparison_card_budget import BudgetExhausted
from .comparison_card_store import ComparisonStore
from .store import StateError


class ComparisonService:
    def __init__(self, store: ComparisonStore, model):
        self.store, self.model = store, model

    def snapshot(self):
        return {**self.store.snapshot(), 'route': None, 'budget': self.store.budget.snapshot()}

    def perform(self, action, body):
        if action not in {'generate', 'select', 'adjust'}:
            raise StateError('NOT_FOUND', '接口不存在', 404)
        if not isinstance(body, dict):
            raise StateError('BAD_BODY', '请求体必须是 JSON 对象')
        state, replay = self.store.begin(action, body)
        if replay:
            return {**state, 'route': None, 'budget': self.store.budget.snapshot()}
        request_id = body['request_id']
        try:
            if action == 'select':
                result = self.store.finish(request_id, state['version'], option_id=body['option_id'])
            else:
                card = self.model(request_id=request_id, previous=state['card'],
                                  feedback=body.get('feedback'), answer_id=body.get('answer_id'))
                result = self.store.finish(request_id, state['version'], card=card)
        except BudgetExhausted:
            error = StateError('BUDGET_EXHAUSTED', '本轮已达 4 次调用上限，现有比较卡和选择保留', 429)
            self.store.fail(request_id, error)
            raise error from None
        except StateError as error:
            self.store.fail(request_id, error)
            raise
        except Exception:
            error = StateError('MODEL_FAILED', '这次更新未完成，原比较卡已保留；没有自动重试', 502)
            self.store.fail(request_id, error)
            raise error from None
        return {**result, 'route': None, 'budget': self.store.budget.snapshot()}
