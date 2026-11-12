"""Account for a bounded receipt without mutating a critic-approved candidate."""
import copy
import time

from .validation import PlanError


def prepare_receipt(service, trip_id, revision, previous, candidate, feedback, review,
                    usage, tracker, *, detail_context, evidence, materials, traveler_profile,
                    deadline, started, coach=None):
    from .revision_receipt import generate_revision_receipt, ReceiptRejected

    receipt_usage = {'model_calls': 0, 'input_tokens': 0, 'output_tokens': 0,
                     'usage_complete': True, 'calls': []}

    def record(row):
        call = copy.deepcopy(row)
        receipt_usage['calls'].append(call)
        usage.setdefault('calls', []).append(call)
        for field in ('model_calls', 'input_tokens', 'output_tokens', 'reasoning_tokens'):
            value = row.get(field)
            if type(value) is int and value >= 0:
                receipt_usage[field] = receipt_usage.get(field, 0) + value
                usage[field] = (usage.get(field) or 0) + value
            elif field != 'reasoning_tokens':
                receipt_usage['usage_complete'] = usage['usage_complete'] = False
        if row.get('usage_complete') is False:
            receipt_usage['usage_complete'] = usage['usage_complete'] = False
        usage['revision_receipt'] = copy.deepcopy(receipt_usage)
        usage['stages'] = copy.deepcopy(tracker.stages)
        usage['duration_ms'] = round((time.monotonic() - started) * 1000)
        service.store.update_job(trip_id, revision, metrics=copy.deepcopy(usage))

    try:
        receipt = generate_revision_receipt(previous, candidate, feedback, review,
            caller=tracker.wrap(service.caller), detail_context=detail_context,
            evidence=evidence, materials=materials, traveler_profile=traveler_profile,
            writer_reasoning=usage.get('writer_reasoning'), deadline=deadline, on_usage=record,
            coach=coach)
    except ReceiptRejected as error:
        # The per-call callback already persisted this usage. _fail must not
        # add the exception's aggregate a second time.
        raise PlanError(str(error)) from None
    if 'revision_receipt' not in usage:
        # Execution mode speaks zero model calls; account the silent receipt too.
        usage['revision_receipt'] = copy.deepcopy(receipt_usage)
        usage['stages'] = copy.deepcopy(tracker.stages)
        service.store.update_job(trip_id, revision, metrics=copy.deepcopy(usage))
    usage['duration_ms'] = round((time.monotonic() - started) * 1000)
    return receipt
