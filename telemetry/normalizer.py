"""Validate and normalize platform observations into JSON-compatible events.

Identity is supplied by the framework, not by an observation. Metadata keys for
common credentials are redacted recursively; producers must also redact secrets
embedded in free text before emitting observations.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
import re
from uuid import UUID, uuid4


class EventValidationError(ValueError):
    """An observation does not meet the common telemetry contract."""


_REQUIRED = {'event_id', 'timestamp', 'instance_id', 'platform', 'source',
             'event_type', 'action', 'outcome', 'simulation'}
_OPTIONAL = {'session_id', 'metadata'}
_OBSERVATION = {'source', 'event_type', 'action', 'outcome', 'simulation'}
_SECRET_KEYS = {'password', 'passwd', 'secret', 'token', 'access_token',
                'refresh_token', 'api_key', 'apikey', 'authorization', 'cookie',
                'set_cookie', 'private_key', 'credential', 'credentials'}
MAX_EVENT_BYTES = 65536
MAX_DEPTH = 12


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise EventValidationError(f'{field} must be a nonempty string up to 256 characters')
    return value


def _metadata(value, depth=0):
    if depth > MAX_DEPTH:
        raise EventValidationError('Event metadata exceeds the nesting limit')
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventValidationError('Metadata keys must be strings')
            normalized = re.sub(r'[^a-z0-9]+', '_', key.lower()).strip('_')
            result[key] = '[REDACTED]' if normalized in _SECRET_KEYS else _metadata(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_metadata(item, depth + 1) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise EventValidationError('Metadata must contain finite JSON values')


def validate_event(event: Mapping) -> dict:
    """Return a detached, redacted event or reject an invalid canonical record.

    Unknown fields are rejected. Timestamps require an explicit UTC offset; UUID
    event IDs are preserved so producers can retry with the same identity.
    Validation and queue acceptance do not prove durable persistence.
    """
    if not isinstance(event, Mapping) or any(not isinstance(k, str) for k in event):
        raise EventValidationError('Event must be a mapping with string keys')
    if not _REQUIRED <= set(event) or set(event) - _REQUIRED - _OPTIONAL:
        raise EventValidationError('Event fields do not match the telemetry schema')
    result = {key: _text(event[key], key) for key in _REQUIRED - {'simulation'}}
    for key in ('instance_id', 'platform'):
        if not re.fullmatch(r'[a-z][a-z0-9_.-]*', result[key]):
            raise EventValidationError(f'{key} must be a framework identifier')
    try:
        UUID(result['event_id'])
        stamp = datetime.fromisoformat(result['timestamp'].replace('Z', '+00:00'))
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise EventValidationError('Event requires a UUID and UTC timestamp') from None
    if type(event['simulation']) is not bool:
        raise EventValidationError('simulation must be a boolean')
    result['simulation'] = event['simulation']
    if 'session_id' in event:
        result['session_id'] = _text(event['session_id'], 'session_id')
    if 'metadata' in event:
        if not isinstance(event['metadata'], Mapping):
            raise EventValidationError('metadata must be a mapping')
        result['metadata'] = _metadata(event['metadata'])
    try:
        size = len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode('utf-8'))
    except (ValueError, OverflowError, RecursionError):
        raise EventValidationError('Event metadata cannot be serialized') from None
    if size > MAX_EVENT_BYTES:
        raise EventValidationError('Event exceeds the byte limit')
    return result


def normalize_event(observation: Mapping, *, instance_id: str, platform: str) -> dict:
    """Tag an observation with caller-owned identity, UUID and UTC timestamp.

    A producer can supply event_id and timestamp to preserve capture time and
    retry identity. Otherwise these values describe normalization time.
    """
    allowed = _OBSERVATION | _OPTIONAL | {'event_id', 'timestamp'}
    if not isinstance(observation, Mapping) or not _OBSERVATION <= set(observation) or set(observation) - allowed:
        raise EventValidationError('Observation fields do not match the telemetry schema')
    event = dict(observation)
    event.update(instance_id=instance_id, platform=platform)
    event.setdefault('event_id', str(uuid4()))
    event.setdefault('timestamp', datetime.now(timezone.utc).isoformat())
    return validate_event(event)
