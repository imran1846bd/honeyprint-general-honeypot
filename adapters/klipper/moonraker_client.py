"""Bounded read-only Moonraker HTTP client for an existing local runtime.

Use a loopback port or a user-managed SSH tunnel. This transport launches no
Klipper process, sends no printer commands and does not validate MCU simulation.
"""
import http.client
import json
import math
import os
import re

from adapters.base_adapter import AdapterError

QUERY = '/printer/objects/query?webhooks&toolhead=position&extruder=temperature,target&heater_bed=temperature,target&print_stats=state'


class MoonrakerClient:
    def __init__(self, *, port=7125, api_key_env=None, expected_klipper=None, expected_moonraker=None):
        if type(port) is not int or not 1 <= port <= 65535:
            raise AdapterError('Invalid Moonraker port')
        if api_key_env is not None and (not isinstance(api_key_env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', api_key_env)):
            raise AdapterError('Invalid API key environment reference')
        for version in (expected_klipper, expected_moonraker):
            if version is not None and (not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', version)
                                        or version.lower() in {'latest','main','master','head','dev','stable'}):
                raise AdapterError('Expected versions must be explicit')
        self.port = port
        self.api_key_env = api_key_env
        self.expected_klipper = expected_klipper
        self.expected_moonraker = expected_moonraker

    def _get(self, path):
        if path not in ('/server/info', '/printer/info', QUERY):
            raise AdapterError('Unsupported read-only endpoint')
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            headers = {'Accept': 'application/json'}
            if self.api_key_env is not None:
                key = os.environ.get(self.api_key_env)
                if not key or len(key) > 4096 or '\r' in key or '\n' in key:
                    raise AdapterError('API key environment value is unavailable or invalid')
                headers['X-Api-Key'] = key
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise AdapterError('Moonraker read-only request failed')
            payload = response.read(1048577)
            if len(payload) > 1048576: raise AdapterError('Moonraker response exceeds limit')
            result = json.loads(payload)
            if not isinstance(result, dict) or set(result) != {'result'} or not isinstance(result['result'], dict):
                raise AdapterError('Invalid Moonraker response envelope')
            return result['result']
        except (OSError, ValueError, http.client.HTTPException, RecursionError):
            raise AdapterError('Moonraker response could not be read') from None
        finally:
            connection.close()

    def snapshot(self):
        """Return selected status fields only; readiness is connectivity evidence.

        A ready response does not prove an MCU is emulated, software is isolated,
        or traffic from other clients is captured. Version pins are checked when
        supplied. Raw server paths, errors, configuration and headers are omitted.
        """
        server = self._get('/server/info')
        printer = self._get('/printer/info')
        objects = self._get(QUERY)
        try:
            versions = {'klipper': printer['software_version'], 'moonraker': server['moonraker_version']}
            if any(not isinstance(v, str) or not 1 <= len(v) <= 128 for v in versions.values()):
                raise ValueError()
            for expected, key in ((self.expected_klipper, 'klipper'), (self.expected_moonraker, 'moonraker')):
                if expected is not None and expected != versions[key]: raise ValueError()
            status = objects['status']
            position = status['toolhead']['position']
            if not isinstance(position, list) or len(position) != 4: raise ValueError()
            def finite(value):
                if type(value) not in (int, float) or not -1e9 <= value <= 1e9 or not math.isfinite(value):
                    raise ValueError()
                return float(value)
            position = [finite(v) for v in position]
            temperatures = {}
            for key in ('extruder', 'heater_bed'):
                if key in status:
                    temperatures[key] = {field: finite(status[key][field]) for field in ('temperature', 'target')}
            if 'extruder' not in temperatures: raise ValueError()
            state = status['print_stats']['state']
            if state not in ('standby','printing','paused','complete','cancelled','error'): raise ValueError()
            connected = server['klippy_connected']
            if type(connected) is not bool: raise ValueError()
            ready = bool(connected and server['klippy_state'] == 'ready' and printer['state'] == 'ready'
                         and status['webhooks']['state'] == 'ready')
            return {'ready': ready, 'versions': versions, 'position': position,
                    'temperatures': temperatures, 'print_state': state,
                    'scope': 'read-only platform status; MCU simulation is not verified'}
        except (KeyError, TypeError, ValueError, OverflowError):
            raise AdapterError('Moonraker status or expected version validation failed') from None
