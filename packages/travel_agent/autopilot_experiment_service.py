"""Opt-in experiment assembly; existing production service stays unchanged."""
from .autopilot_experiment import ExperimentBindings, ExperimentRunner
from .autopilot_experiment_budget import (AUTHORIZED_PROVIDER_REQUESTS,
    MAX_PROVIDER_REQUESTS, ExperimentBudget, ExperimentStore)
from .autopilot_service import AutopilotService
from .autopilot_worker import AutopilotWorker
from .autopilot_workspace_records import material_state


class ExperimentService(AutopilotService):
    def __init__(self, trip_store, *, public_research_allowed=False, **kwargs):
        self.public_research_allowed = public_research_allowed is True
        kwargs.setdefault('bindings_factory', ExperimentBindings)
        kwargs.setdefault('store_factory', ExperimentStore)
        super().__init__(trip_store, **kwargs)
        self.runner = ExperimentRunner(self.store, self.bindings.gateway,
            model_caller=self.runner.model_caller, budget_factory=ExperimentBudget,
            snapshot=self.bindings.snapshot, outcome_validator=self.bindings.outcomes)
        self.worker = AutopilotWorker(self.store, self.workspace, self.runner)

    def _constraints(self, body):
        return dict(super()._constraints(body),
                    public_research_allowed=self.public_research_allowed)

    def capabilities(self):
        return dict(super().capabilities(), experiment='expert-d1d2-20261024',
                    public_research_allowed=self.public_research_allowed,
                    provider_request_limit=MAX_PROVIDER_REQUESTS,
                    authorized_provider_requests=AUTHORIZED_PROVIDER_REQUESTS)

    def _project_advice_results(self, db, operations, trip, runs):
        stale, expired = super()._project_advice_results(db, operations, trip, runs)
        current = material_state(trip, self.store.clock())
        # Research appends sources while a run is working. Older read receipts
        # remain historical, but must not hide prose produced from the final
        # source snapshot. Later withdrawal/change still invalidates that text.
        stale = {run_id for run_id in stale if
            (runs[run_id].get('result') or {}).get('experiment_material_state') != current}
        stale.update(run_id for run_id, run in runs.items()
            if (run.get('result') or {}).get('experiment_material_state') is not None
            and run['result']['experiment_material_state'] != current)
        return stale, expired
