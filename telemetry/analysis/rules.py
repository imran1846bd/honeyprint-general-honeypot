"""Explainable observation rules; findings are indicators, not proof of attack."""

from hashlib import sha256

from telemetry.normalizer import validate_event


RULE_VERSION = '1.0.0'


def classify_event(event) -> tuple[dict, ...]:
    """Produce stable findings from minimal telemetry without ML claims.

    Context is required to distinguish authorized research activity from hostile
    activity. Findings describe observed behaviour and are deduplicated per rule
    and captured event, including the rule version in their identity.
    """
    record = validate_event(event)
    metadata = record.get('metadata', {})
    indicators = []
    if record['event_type'] == 'http.request':
        status = metadata.get('status')
        if type(status) is int and status in (401, 403):
            indicators.append(('access_denied', 'medium', 'API access was denied; review authorization context.'))
        if type(status) is int and status == 404:
            indicators.append(('unknown_endpoint', 'low', 'An unknown API endpoint was requested.'))
    if record['event_type'] == 'printer.command':
        if metadata.get('gcode') == 'M112':
            indicators.append(('emergency_stop', 'high', 'An emergency-stop command was observed; review operator intent.'))
        if record['outcome'] == 'rejected':
            indicators.append(('command_rejected', 'low', 'The platform rejected a printer command.'))
    if record['event_type'] == 'platform.event' and metadata.get('event') in ('Error', 'Disconnected'):
        indicators.append(('platform_interruption', 'medium', 'A platform error or disconnection was observed.'))
    return tuple({
        'finding_id': sha256(f'{record["event_id"]}:{rule_id}:{RULE_VERSION}'.encode()).hexdigest(),
        'event_id': record['event_id'], 'instance_id': record['instance_id'],
        'timestamp': record['timestamp'], 'rule_id': rule_id, 'rule_version': RULE_VERSION,
        'severity': severity, 'message': message,
    } for rule_id, severity, message in indicators)
