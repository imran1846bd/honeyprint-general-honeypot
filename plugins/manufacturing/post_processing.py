"""Simulated heater shutdown and cooldown after build execution."""


def post_processing(job, simulator):
    if job.get('stage') != 'build_execution': raise ValueError('Execution must precede post-processing')
    simulator.hotend.set_target(0.0)
    simulator.bed.set_target(0.0)
    simulator.step(300.0)
    return {**job, 'stage': 'post_processing', 'post_processing': simulator.snapshot()}
