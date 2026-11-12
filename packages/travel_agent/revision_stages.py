"""Per-request timing and safe progress events; no prompts or credentials stored."""
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import threading
import time
import uuid

LABELS = {'material_details': '正在抽取材料字段', 'material_corroboration': '正在交叉核对材料',
          'detail_focus': '正在确定需要补查的信息', 'detail_research': '正在查公开信息',
          'scope': '正在确定本次调整范围', 'writer': '正在合成调整方案',
          'critic': '正在核对修改与来源', 'revision_receipt': '正在整理调整理由和下一步',
          'revision_receipt_repair': '正在核对回执中的来源与建议'}


def error_kind(error, metrics=None):
    kind = (metrics or {}).get('error_type') if isinstance(metrics, dict) else None
    if kind in ('timeout', 'network', 'http', 'config', 'transport', 'format'):
        return kind
    return 'timeout' if isinstance(error, TimeoutError) else 'caller_exception'


class StageTrace:
    def __init__(self, on_event=None, *, clock=None):
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.on_event = on_event
        self.stages, self.active = [], {}
        self.lock = threading.RLock()
        self.run_id = uuid.uuid4().hex
        self.sequence = 0
        self.current = None

    def _emit(self, event):
        with self.lock:
            if event['status'] == 'started':
                self.active[event['id']] = event
            else:
                if event['id'] not in self.active:
                    return  # Consumer deadline already closed this stage.
                self.active.pop(event['id'], None)
                self.stages.append(copy.deepcopy(event))
            self.current = copy.deepcopy(next(reversed(self.active.values())) if self.active else event)
            if self.on_event:
                self.on_event(copy.deepcopy(event))

    def cancel_stage(self, phase, material_id=None, error_type='timeout'):
        with self.lock:
            for event in list(self.active.values()):
                if event['phase'] == phase and event.get('material_id') == material_id:
                    ended = copy.deepcopy(event)
                    ended.update(status='failed', error_type=error_type,
                        elapsed_seconds=round(self.clock() - self.started - event['start_seconds'], 6),
                        at=datetime.now(timezone.utc).isoformat())
                    self._emit(ended)

    @contextmanager
    def span(self, phase, **metadata):
        with self.lock:
            self.sequence += 1
            identifier = f'{self.run_id}-{self.sequence}'
        started = self.clock()
        days = sorted(set(d for d in metadata.get('day_numbers', []) if type(d) is int and d > 0))
        label = LABELS.get(phase, '正在处理调整')
        if phase == 'writer' and days:
            label = '正在合成第 ' + '、'.join(map(str, days)) + ' 天'
        event = {'id': identifier, 'phase': phase, 'status': 'started', 'label': label,
                 'at': datetime.now(timezone.utc).isoformat(), 'elapsed_seconds': 0,
                 'start_seconds': round(started - self.started, 6), 'material_id': metadata.get('material_id'),
                 'target': metadata.get('target'), 'day_numbers': days, 'error_type': None}
        self._emit(event)
        try:
            yield event
        except Exception as error:
            event.update(status='failed', error_type=error_kind(error, getattr(error, 'usage', {})))
            raise
        else:
            event['status'] = 'completed'
        finally:
            event.update(elapsed_seconds=round(self.clock() - started, 6),
                         at=datetime.now(timezone.utc).isoformat())
            self._emit(event)

    def wrap(self, caller):
        def call(messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            phase = payload.get('phase')
            if phase not in LABELS:
                return caller(messages, **kwargs)
            nodes = payload.get('nodes', {})
            days = [value['day'] for path, value in nodes.items() if path.startswith('/plan/days/')
                    and isinstance(value, dict) and type(value.get('day')) is int]
            with self.span(phase, material_id=payload.get('material', {}).get('id'), day_numbers=days):
                started = self.clock()
                result = caller(messages, **kwargs)
                timeout = kwargs.get('timeout')
                if (phase in ('material_details', 'material_corroboration')
                        and isinstance(timeout, (int, float)) and self.clock() - started >= timeout):
                    raise TimeoutError('Stage caller returned after its deadline')
                return result
        call.cancel_stage = self.cancel_stage
        return call
