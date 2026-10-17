"""Platform-independent shadow manufacturing over an immutable instance plan.

This service evaluates an artifact using registered stage providers and a local
simulation. It never submits a job to the platform or claims native execution.
"""
from copy import deepcopy
from threading import RLock

from core.profile_loader import load_profile
from plugins.manufacturing.pipeline import validate_stages, run_job
from plugins.simulators.printer import PrinterSimulator


class ManufacturingService:
    def __init__(self, plan, *, artifact_storage, sink):
        self.stages = validate_stages(plan.config.get('stages', ()))
        if not self.stages or not callable(sink):
            raise ValueError('Manufacturing requires enabled stages and an event sink')
        self.factories = {stage: plan.factories[('manufacturing_stage', stage)] for stage in self.stages}
        if any(not callable(factory) for factory in self.factories.values()):
            raise ValueError('Manufacturing provider must be callable')
        self.simulator = PrinterSimulator(load_profile(plan.config['printer_profile']))
        self.artifacts = artifact_storage
        self.sink = sink
        self._lock = RLock()
        self._failed = False
        self._closed = False

    def submit(self, content, *, name='job.gcode'):
        with self._lock:
            if self._closed:
                raise RuntimeError('Manufacturing service is closed')
            if self._failed:
                raise RuntimeError('Manufacturing evidence failed; create a new service before further jobs')
            if type(content) is not bytes or not 1 <= len(content) <= self.artifacts.max_file_bytes:
                raise ValueError('Artifact exceeds configured retention file limit')
            candidate = deepcopy(self.simulator)
            result, events = run_job(content, candidate, self.stages, name=name, factories=self.factories)
            artifact = self.artifacts.store(content)
            try:
                for event in events:
                    accepted = self.sink({'source': 'manufacturing', 'event_type': 'manufacturing.stage',
                                          'action': event['stage'], 'outcome': event['outcome'],
                                          'simulation': True, 'metadata': {**artifact, 'execution_mode': 'shadow'}})
                    if accepted is False:
                        raise RuntimeError('Manufacturing evidence queue rejected a stage')
            except Exception:
                self._failed = True
                raise RuntimeError('Manufacturing stage evidence could not be captured') from None
            self.simulator = candidate
            return {'simulation': True, 'execution_mode': 'shadow', **artifact,
                    'stage': result['stage'], 'inspection': result.get('inspection')}

    def snapshot(self):
        with self._lock:
            return self.simulator.snapshot()

    def close(self):
        """Reject future submissions after the owning runtime begins shutdown."""
        with self._lock:
            self._closed = True
