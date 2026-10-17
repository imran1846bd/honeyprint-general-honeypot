"""Validated, shared runtime controls; used by env, YAML and CLI consumers."""
import math
import re

# name: (default, minimum, maximum); booleans and filenames have separate checks.
NUMBERS = {
    'upload_max_bytes': (1048576, 1, 1048576),
    'request_max_bytes': (65536, 1, 1048576),
    'disk_warning_bytes': (1073741824, 0, 1099511627776),
    'preheat_hotend_c': (200.0, 0, 280),
    'preheat_bed_c': (60.0, 0, 130),
    'sqlite_busy_timeout_seconds': (5.0, 0.01, 300),
    'run_duration_seconds': (0.0, 0, 86400),
    'startup_timeout_seconds': (180.0, 0.1, 86400),
    'ready_timeout_seconds': (30.0, 0.1, 3600),
    'health_interval_seconds': (1.0, 0.01, 60),
    'shutdown_timeout_seconds': (60.0, 0.1, 3600),
    'event_queue_capacity': (1024, 1, 1000000),
    'collector_batch_size': (100, 1, 1000),
    'collector_max_batches': (100, 1, 100000),
    'collector_interval_seconds': (0.2, 0.01, 60),
    'final_drain_max_batches': (1000, 1, 100000),
    'artifact_max_file_bytes': (1048576, 1, 1048576),
    'artifact_max_total_bytes': (104857600, 1, 1073741824),
    'artifact_max_files': (1000, 1, 10000),
    'analysis_interval_seconds': (0.5, 0.1, 60),
    'analysis_batch_size': (100, 1, 1000),
    'dashboard_page_size': (100, 1, 1000),
    'dashboard_request_timeout_seconds': (2.0, 0.1, 60),
    'dashboard_startup_timeout_seconds': (10.0, 0.1, 3600),
}
DEFAULTS = {name: spec[0] for name, spec in NUMBERS.items()}
DEFAULTS.update(evidence_db_name='events.sqlite3', analysis_db_name='analysis.sqlite3', analysis_enabled=True,
                artifact_dir='auto', camera_enabled=True, camera_source='auto')


def resolve_settings(values=None):
    if values is None: values = {}
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError('runtime: unknown settings or invalid mapping')
    result = {**DEFAULTS, **values}
    for name, (default, low, high) in NUMBERS.items():
        value = result[name]
        valid_type = type(value) is int if type(default) is int else type(value) in (int, float)
        if not valid_type or not low <= value <= high or not math.isfinite(value):
            raise ValueError(f'runtime.{name}: expected finite value from {low} to {high}')
    for name in ('analysis_enabled', 'camera_enabled'):
        if type(result[name]) is not bool: raise ValueError(f'runtime.{name}: expected boolean')
    for name in ('artifact_dir', 'camera_source'):
        value=result[name]
        if not isinstance(value,str) or not value.strip() or len(value)>4096 or any(ord(c)<32 for c in value):
            raise ValueError(f'runtime.{name}: expected a nonempty path/source or auto')
    if not result['camera_enabled'] and result['camera_source'] != 'auto':
        raise ValueError('Disabled camera requires camera_source=auto')
    if result['camera_source'].startswith(('http://','https://')):
        # Streams may use query parameters; credentials and fragments remain invalid.
        from urllib.parse import urlsplit
        url=urlsplit(result['camera_source'])
        if not url.hostname or url.username or url.password or url.fragment or any(c.isspace() for c in result['camera_source']):
            raise ValueError('Invalid camera stream URL')
        url.port
    for name in ('evidence_db_name', 'analysis_db_name'):
        value = result[name]
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.sqlite3', value):
            raise ValueError(f'runtime.{name}: expected a plain .sqlite3 filename')
    if result['evidence_db_name'].casefold() == result['analysis_db_name'].casefold():
        raise ValueError('Evidence and analysis database filenames must differ')
    if result['artifact_max_file_bytes'] > result['artifact_max_total_bytes']:
        raise ValueError('Artifact per-file limit exceeds its store quota')
    return result


def settings_from_env(values):
    parsed = {}
    for name, default in DEFAULTS.items():
        key = 'HONEYPRINT_' + name.upper()
        raw = values.get(key, '')
        if raw == '': continue
        if type(default) is bool:
            if raw.lower() not in ('true', 'false'): raise ValueError(f'{key}: expected true or false')
            parsed[name] = raw.lower() == 'true'
        elif type(default) in (int, float):
            try: parsed[name] = type(default)(raw)
            except (ValueError, OverflowError): raise ValueError(f'{key}: invalid number') from None
        else: parsed[name] = raw
    return resolve_settings(parsed)
