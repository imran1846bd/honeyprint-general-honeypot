"""OctoPrint single-file audit plugin installed into an owned plugins folder.

Hooks enqueue bounded, minimal observations. A worker sends authenticated local
UDP messages; loss and queue drops are surfaced via the admin-only status API.
Raw headers, request bodies, query strings, event payloads and command arguments
are deliberately omitted. Serial responses are counted, not copied.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import re
import socket
import threading
from uuid import uuid4

import octoprint.plugin


class HoneyPrintAudit(octoprint.plugin.StartupPlugin,
                     octoprint.plugin.ShutdownPlugin,
                     octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.EventHandlerPlugin,
                     octoprint.plugin.ProgressPlugin,
                     octoprint.plugin.SimpleApiPlugin,
                     octoprint.plugin.AssetPlugin):
    def __init__(self):
        self._queue = queue.Queue(maxsize=65536)
        self._stop = threading.Event()
        self._worker = None
        self._lock = threading.Lock()
        self._sent = self._dropped = self._errors = self._received = 0
        self._position = None
        self._position_sequence = 0
        self._controls = {'motors': 'on', 'fan_percent': 0,
                          'feed_percent': 100, 'flow_percent': 100}
        self._target = None
        self._token = None
        self._flags = {}

    def get_settings_defaults(self):
        return {'port': None, 'token': None, 'instance_id': None,
                'http': True, 'gcode': True, 'state': True}

    def on_after_startup(self):
        port = self._settings.get_int(['port'])
        token = self._settings.get(['token'])
        if not port or not isinstance(token, str) or len(token) < 32:
            return
        self._target = ('127.0.0.1', port)
        self._token = token
        self._flags = {key: self._settings.get_boolean([key]) for key in ('http', 'gcode', 'state')}
        self._stop.clear()
        self._worker = threading.Thread(target=self._send, name='HoneyPrintAudit', daemon=True)
        self._worker.start()
        self._emit('plugin', 'audit.started', 'observe', 'started')

    def _emit(self, source, kind, action, outcome, metadata=None):
        if self._target is None or self._stop.is_set():
            return
        event = {'source': source, 'event_type': kind, 'action': action,
                 'outcome': outcome, 'simulation': True,
                 'event_id': str(uuid4()), 'timestamp': datetime.now(timezone.utc).isoformat(),
                 'metadata': metadata or {}}
        try:
            # Wait briefly instead of dropping evidence while G-code streams during a print.
            self._queue.put(event, timeout=1)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def _send(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
            channel.settimeout(0.2)
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    event = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    payload = json.dumps({'token': self._token, 'event': event}).encode('utf-8')
                    channel.sendto(payload, self._target)
                    with self._lock:
                        self._sent += 1
                except (OSError, ValueError):
                    with self._lock:
                        self._errors += 1
                finally:
                    self._queue.task_done()

    def on_shutdown(self):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2)

    def on_event(self, event, payload):
        if self._flags.get('state'):
            self._emit('plugin', 'platform.event', 'state', 'observed', {'event': str(event)[:128]})
            if event in ('FileAdded', 'FileRemoved', 'FileMoved'):
                details = payload if isinstance(payload, dict) else {}
                name = details.get('name') or details.get('path')
                metadata = {'filename': name.rsplit('/', 1)[-1][:120]
                            if isinstance(name, str) else None,
                            'storage': str(details.get('storage', ''))[:40]}
                path = details.get('path')
                if isinstance(path, str) and len(path) <= 480:
                    # Relative path in OctoPrint's storage, so HoneyPrint can retain the file.
                    metadata['path'] = path
                self._emit('plugin', 'file.artifact',
                           {'FileAdded': 'add', 'FileRemoved': 'remove',
                            'FileMoved': 'move'}[event], 'observed', metadata)
            elif event == 'FileSelected':
                details = payload if isinstance(payload, dict) else {}
                name = details.get('name') or details.get('file')
                self._emit('plugin', 'printer.job', 'select', 'observed',
                           {'filename': name.rsplit('/', 1)[-1][:120]
                            if isinstance(name, str) else None})
            actions = {'PrintStarted': 'start', 'PrintPaused': 'pause',
                       'PrintResumed': 'resume', 'PrintCancelled': 'cancel',
                       'PrintFailed': 'failed', 'PrintDone': 'complete'}
            if event in actions:
                details = payload if isinstance(payload, dict) else {}
                filename = details.get('name') or details.get('path')
                metadata = {'filename': filename.rsplit('/', 1)[-1][:120]
                            if isinstance(filename, str) else None}
                progress = details.get('progress')
                if type(progress) in (int, float) and 0 <= progress <= 100:
                    metadata['progress'] = progress
                self._emit('plugin', 'printer.job', actions[event], 'observed', metadata)

    def on_print_progress(self, storage, path, progress):
        if (self._flags.get('state') and isinstance(path, str)
                and type(progress) is int and 0 <= progress <= 100):
            self._emit('plugin', 'printer.job', 'progress', 'observed',
                       {'filename': path.rsplit('/', 1)[-1][:120], 'progress': progress})

    def gcode_sent(self, comm, phase, cmd, cmd_type, gcode, *args, **kwargs):
        if isinstance(cmd, str):
            match = re.match(r'^\s*(M18|M84|M17|M106|M107|M220|M221|G0|G1|G28)\b', cmd, re.I)
            if match:
                code = match.group(1).upper()
                value = re.search(r'\bS(\d+)\b', cmd, re.I)
                with self._lock:
                    if code in ('M18', 'M84'):
                        self._controls['motors'] = 'off'
                    elif code in ('M17', 'G0', 'G1', 'G28'):
                        self._controls['motors'] = 'on'
                    elif code == 'M106':
                        self._controls['fan_percent'] = round(int(value.group(1)) * 100 / 255) if value else 100
                    elif code == 'M107':
                        self._controls['fan_percent'] = 0
                    elif code in ('M220', 'M221') and value:
                        self._controls['feed_percent' if code == 'M220' else 'flow_percent'] = int(value.group(1))
        if self._flags.get('gcode'):
            code = gcode if isinstance(gcode, str) and re.fullmatch(r'[GMT][0-9]+', gcode) else 'other'
            self._emit('firmware', 'printer.command', 'send', 'sent', {'gcode': code})
        # None leaves the serial command unchanged.

    def gcode_received(self, comm, line, *args, **kwargs):
        with self._lock:
            self._received += 1
            if isinstance(line, str):
                match = re.search(r'\bX:([+-]?\d+(?:\.\d+)?)\s+Y:([+-]?\d+(?:\.\d+)?)\s+Z:([+-]?\d+(?:\.\d+)?)\b', line)
                if match:
                    self._position = {axis: float(value) for axis, value in zip(('x', 'y', 'z'), match.groups())}
                    extrusion = re.search(r'\bE:([+-]?\d+(?:\.\d+)?)', line)
                    if extrusion:
                        self._position['e'] = float(extrusion.group(1))
                    self._position_sequence += 1
        return line

    def after_request(self, response):
        from flask import request
        rule = request.url_rule.rule if request.url_rule else 'unmatched'
        interactive = (request.method != 'GET' or response.status_code >= 400
                       or rule == 'frontend')
        if self._flags.get('http') and interactive and rule != '/api/plugin/<string:name>':
            self._emit('api', 'http.request', request.method, 'observed',
                       {'route': rule[:256], 'status': response.status_code})
        return response

    def request_hooks(self, *args, **kwargs):
        return [self.after_request]

    def is_api_protected(self):
        return True

    def is_api_adminonly(self):
        return False

    def get_assets(self):
        return {'js': ['js/controls.js', 'js/ui_capture.js']}

    def get_asset_folder(self):
        return str(Path(__file__).parent / 'static')

    def on_api_get(self, request):
        from flask import jsonify
        with self._lock:
            return jsonify(instance_id=self._settings.get(['instance_id']),
                           active=bool(self._worker and self._worker.is_alive()),
                           sent=self._sent, dropped=self._dropped, errors=self._errors,
                           serial_received=self._received, queued=self._queue.qsize(),
                           position=self._position, position_sequence=self._position_sequence,
                           controls=dict(self._controls))

    def get_api_commands(self):
        return {'ui_interaction': ['action', 'control', 'page']}

    # Same rules as telemetry/ui_capture.py (this plugin runs inside OctoPrint and cannot import it).
    UI_ACTIONS = frozenset(('click', 'double_click', 'context_menu', 'change', 'input', 'submit',
                            'file_select', 'file_drop', 'page_view', 'navigate', 'page_leave'))

    @staticmethod
    def _ui_text(value, limit):
        return isinstance(value, str) and 1 <= len(value) <= limit and not any(ord(c) < 32 for c in value)

    def on_api_command(self, command, data):
        if command != 'ui_interaction' or not isinstance(data, dict):
            return
        if not set(data) <= {'action', 'control', 'page', 'element', 'from', 'filename', 'size_bytes'}:
            return
        action, control, page = (data.get(key) for key in ('action', 'control', 'page'))
        if (action not in self.UI_ACTIONS or not self._ui_text(control, 120)
                or not self._ui_text(page, 160) or not page.startswith('/')):
            return
        metadata = {'control': control, 'page': page}
        if self._ui_text(data.get('element'), 40):
            metadata['element'] = data['element']
        if action == 'navigate' and self._ui_text(data.get('from'), 160) and data['from'].startswith('/'):
            metadata['from'] = data['from']
        if action in ('file_select', 'file_drop'):
            filename, size = data.get('filename'), data.get('size_bytes')
            if not self._ui_text(filename, 240) or type(size) is not int or size < 0:
                return
            metadata.update(filename=filename.rsplit('/', 1)[-1], size_bytes=size)
        self._emit('webui', 'ui.interaction', action, 'observed', metadata)


__plugin_name__ = 'HoneyPrint Audit'
__plugin_pythoncompat__ = '>=3.10,<3.14'


def __plugin_load__():
    global __plugin_implementation__, __plugin_hooks__
    __plugin_implementation__ = HoneyPrintAudit()
    __plugin_hooks__ = {
        'octoprint.comm.protocol.gcode.sent': __plugin_implementation__.gcode_sent,
        'octoprint.comm.protocol.gcode.received': __plugin_implementation__.gcode_received,
        'octoprint.server.api.after_request': __plugin_implementation__.request_hooks,
    }
