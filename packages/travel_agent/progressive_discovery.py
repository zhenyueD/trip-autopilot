"""Publish sourced photos and Chinese reading independently of full composition."""
from .phase1_budget_context import ContextThreadPoolExecutor as ThreadPoolExecutor
from copy import deepcopy
import threading
import time

from .runtime import add_usage
from .store import StateError

PHOTO_FIELDS = ('image_url', 'image_source_url', 'image_author', 'image_license',
                'image_license_url', 'image_attribution', 'image_is_generated',
                'image_checked_at', 'image_usage_notes')
READING_FIELDS = ('name_zh', 'description_zh')


class CandidatePresentation:
    def __init__(self, service, trip_id, revision, supplies, usage):
        self.service, self.trip_id, self.revision = service, trip_id, revision
        self.supplies, self.usage = deepcopy(supplies), usage
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.stats, self.reading_metrics = {}, {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='place-presentation')
        self.futures = []

    def __enter__(self):
        from .phase1_budget_context import current_budget
        budget = current_budget()
        if budget is not None and not self.service.store.internal_get(self.trip_id).get('plan'):
            self.stats['deferred'] = 'writer_budget_reserved; media follows validated itinerary'
            return self
        discovery = getattr(self.service.researcher, 'discovery', None)
        image_reader = getattr(discovery, 'enrich_images', None)
        if callable(image_reader) and self.supplies:
            self.futures.append(self.pool.submit(self._images, image_reader))
        if callable(self.service.place_localizer) and self.supplies:
            self.futures.append(self.pool.submit(self._reading))
        return self

    def _images(self, reader):
        try:
            images = reader(deepcopy(self.supplies[:6]))
            self._publish(images, PHOTO_FIELDS, 'images',
                          '首批地点图片已整理，完整行程仍在编排')
        except Exception:
            # This optional presentation work never discards the useful trip.
            self.stats['images'] = 'unavailable'

    def _reading(self):
        try:
            result = self.service.place_localizer(deepcopy(self.supplies))
            self.reading_metrics = result.get('metrics', {})
            self._publish(result.get('supplies', []), READING_FIELDS, 'reading',
                          '首批地点中文简介已整理，完整行程仍在编排', result.get('status', 'unavailable'))
        except Exception:
            self.stats['reading'] = 'unavailable'

    def _publish(self, incoming, fields, kind, label, status='ready'):
        by_id = {s['id']: s for s in incoming if isinstance(s, dict) and s.get('id')}
        with self.lock:
            for supply in self.supplies:
                update = by_id.get(supply['id'], {})
                supply.update({key: update[key] for key in fields if update.get(key) is not None})
            count = sum(bool(s.get('image_url' if kind == 'images' else 'description_zh')) for s in self.supplies)
            self.stats[kind] = status if count else 'unavailable'
            self.stats[kind + '_elapsed_ms'] = round((time.monotonic() - self.started) * 1000)
            if not count:
                return
            try:
                self.service.store.update_job(self.trip_id, self.revision,
                    discovery={'supplies': deepcopy(self.supplies), 'phase': 'composing',
                               'presentation': deepcopy(self.stats)}, event=label)
            except StateError:
                pass  # Deleted/superseded tasks cannot be revived by late photos.

    def __exit__(self, *error):
        # Image transport and translation each have a hard bound. Draft is
        # already visible if it finishes before optional presentation work.
        self.pool.shutdown(wait=True)
        add_usage(self.usage, self.reading_metrics)
        self.usage['candidate_presentation'] = deepcopy(self.stats)

    def merge_plan(self, plan):
        by_id = {s['id']: s for s in self.supplies}
        for supply in plan['supplies']:
            source = by_id.get(supply['id'], {})
            supply.update({key: source[key] for key in PHOTO_FIELDS + READING_FIELDS if key in source})
