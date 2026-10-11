"""Run explicitly selected manufacturing stages on detached simulation state."""
from copy import deepcopy
from plugins.manufacturing.job_intake import job_intake
from plugins.manufacturing.build_preparation import build_preparation
from plugins.manufacturing.build_execution import build_execution
from plugins.manufacturing.post_processing import post_processing
from plugins.manufacturing.inspection import inspection

STAGES = ('job_intake', 'build_preparation', 'build_execution', 'post_processing', 'inspection')
FACTORIES = dict(zip(STAGES, (job_intake, build_preparation, build_execution, post_processing, inspection)))


def validate_stages(stages):
    stages = tuple(stages)
    if stages and stages != STAGES[:len(stages)]:
        raise ValueError('Manufacturing stages must be an ordered prefix starting with intake')
    return stages


def run_job(content, simulator, stages, *, name='job.gcode', factories=None):
    stages = validate_stages(stages)
    if not stages: raise ValueError('Manufacturing stages are disabled')
    providers = dict(FACTORIES if factories is None else factories)
    if any(stage not in providers or not callable(providers[stage]) for stage in stages):
        raise ValueError('Every selected stage requires a registered callable')
    candidate = deepcopy(simulator)
    result = providers['job_intake'](content, name=name)
    events = [{'stage': 'job_intake', 'outcome': 'completed'}]
    for stage in stages[1:]:
        result = providers[stage](result, candidate)
        outcome = 'completed'
        if stage == 'inspection' and not result['inspection']['passed']: outcome = 'failed'
        events.append({'stage': stage, 'outcome': outcome})
    if 'build_execution' in stages:
        simulator.__dict__.update(candidate.__dict__)
    return result, events
