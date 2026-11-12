"""Authenticated tool dispatch; schemas are separate from model judgments."""
from .autopilot_research_failure import research_failure
from .public_search import SearchResponseError
from .autopilot_toolspec import parse_arguments, specification
from .store import StateError


def receipt(store, run, *, result=None, operation=None, code=None, status=None):
    return {'operation_id': (operation or {}).get('id'),
            'status': status or ('rejected' if code else 'succeeded'),
            'result': result, 'error_code': code, 'retryable': False,
            'source_refs': (result or {}).get('source_refs', []) if isinstance(result, dict) else [],
            'state_version': run['goal_version'],
            'observed_at': (operation or {}).get('finished_at', store.clock())}


def recorded_handler(store, name, function):
    """For read/compute adapters. DB mutations use their own atomic handlers.

    A callback receives server-owned context, never a model-provided actor.
    Unhandled exceptions remain in-flight/unknown instead of fabricated failure.
    """
    def handle(run_id, lease, call_id, arguments):
        reserved = store.reserve_operation(run_id, lease, call_id, name, arguments)
        operation = reserved['operation']
        if reserved['disposition'] == 'replay':
            return operation['result']
        if reserved['disposition'] != 'execute':
            raise StateError('OPERATION_UNKNOWN', '操作结果尚未确认，已停止重复执行', 409)
        run = store.internal_get_run(run_id)
        context = {'run_id': run_id, 'lease': lease, 'trip_id': run['trip_id'],
                   'goal_id': run['goal_id'], 'operation_id': operation['id']}
        try:
            result = function(context, arguments)
        except StateError as error:
            # The adapter's explicit rejection is a known, durable tool result.
            result = {'error_code': error.code, 'status': 'rejected'}
        except (OSError, SearchResponseError) as error:
            if name != 'research_fact':
                raise
            result = research_failure(error)
            store.finish_operation(run_id, lease, operation['id'], result, status='failed')
            return result
        store.finish_operation(run_id, lease, operation['id'], result)
        return result
    return handle


class ToolGateway:
    def __init__(self, store, handlers):
        self.store, self.handlers = store, dict(handlers)
        for name, handler in self.handlers.items():
            specification(name)
            if not callable(handler):
                raise ValueError('registered handler must be callable')

    def _allowed_action(self, run, goal, name, arguments=None):
        if name not in {'apply_revision', 'undo_revision'} or goal['constraints'].get('auto_apply') is True:
            return True
        command = run.get('event', {}).get('server_command')
        return (isinstance(command, dict) and command.get('name') == name
                and (arguments is None or command.get('arguments') == arguments))

    def catalog(self, run_id, lease):
        run = self.store.assert_dispatch(run_id, lease)
        goal = self.store.internal_get_goal(run['goal_id'])
        return [specification(name, goal=goal) for name in self.handlers
                if name in goal['authorization']['tools'] and self._allowed_action(run, goal, name)]

    def execute(self, run_id, lease, call):
        # Recheck authority even for malformed requests; never leak trip state.
        run = self.store.internal_get_run(run_id)
        self.store.internal_get_messages(run_id, lease)
        try:
            name, raw = call['function']['name'], call['function']['arguments']
            call_id = call['id']
            if call.get('type') != 'function' or not isinstance(call_id, str) or not call_id:
                raise ValueError()
            if name not in self.handlers:
                return receipt(self.store, run, code='TOOL_UNAVAILABLE')
            arguments = parse_arguments(name, raw, request_id=f'{run_id}:{call_id}')
        except (KeyError, TypeError, ValueError, RecursionError):
            return receipt(self.store, run, code='INVALID_TOOL_ARGUMENTS')
        goal = self.store.internal_get_goal(run['goal_id'])
        if name not in goal['authorization']['tools']:
            return receipt(self.store, run, code='TOOL_NOT_AUTHORIZED')
        if not self._allowed_action(run, goal, name, arguments):
            return receipt(self.store, run, code='MANUAL_ACTION_REQUIRED')
        try:
            result = self.handlers[name](run_id, lease, call_id, arguments)
            operation = self.store.internal_get_operation(run_id, lease, call_id)
            if (name == 'research_fact' and operation['status'] == 'failed'
                    and isinstance(result, dict) and result.get('status') == 'failed'):
                return receipt(self.store, run, result=result, operation=operation,
                               code=result.get('error_code'), status='failed')
            if operation['status'] != 'succeeded':
                raise StateError('OPERATION_UNKNOWN', '工具缺少可信完成回执', 409)
            return receipt(self.store, run, result=result, operation=operation,
                           code=result.get('error_code') if isinstance(result, dict) else None)
        except StateError as error:
            if error.code in {'LEASE_LOST', 'AUTHORIZATION_REVOKED', 'GOAL_VERSION_CONFLICT', 'GOAL_NOT_ACTIVE'}:
                raise
            return receipt(self.store, run, code=error.code,
                           status='unknown' if error.code == 'OPERATION_UNKNOWN' else 'rejected')
