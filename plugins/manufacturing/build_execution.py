"""Execute the validated simulation subset with reproducible virtual time."""
from copy import deepcopy


def build_execution(job, simulator):
    if job.get('stage') != 'build_preparation': raise ValueError('Preparation must precede execution')
    candidate = deepcopy(simulator)
    count = 0
    for command in job['commands']:
        candidate.command(command)
        candidate.step(1.0)
        count += 1
    simulator.__dict__.update(candidate.__dict__)
    return {**job, 'stage': 'build_execution', 'executed_commands': count,
            'execution': simulator.snapshot()}
