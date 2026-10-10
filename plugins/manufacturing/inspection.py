"""Assess simulated process evidence without claiming real part quality."""


def inspection(job, simulator):
    if job.get('stage') != 'post_processing': raise ValueError('Post-processing must precede inspection')
    state = simulator.snapshot()
    checks = {'commands_complete': job['executed_commands'] == len(job['commands']),
              'powered': state['powered'],
              'thermistor_available': state['sensors']['thermistor_fault'] is None,
              'filament_present': state['sensors']['filament_present'],
              'heaters_off': state['target_temperature'] == 0 and state['bed_target'] == 0,
              'cooled': state['temperature'] < 50 and state['bed_temperature'] < 50,
              'motion_complete': state['position'] == state['target_position']}
    return {**job, 'stage': 'inspection',
            'inspection': {'simulation': True, 'checks': checks, 'passed': all(checks.values()),
                           'scope': 'simulated process consistency; no physical part quality measurement'}}
