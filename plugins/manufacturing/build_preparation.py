"""Preflight a job on detached simulation before changing live printer state."""
from copy import deepcopy


def build_preparation(job, simulator):
    if job.get('stage') != 'job_intake': raise ValueError('Intake must precede preparation')
    candidate = deepcopy(simulator)
    for command in job['commands']:
        candidate.command(command)
        candidate.step(1.0)
    return {**job, 'stage': 'build_preparation', 'preflight': candidate.snapshot()}
