"""OctoPrint single-file audit plugin installed into an owned plugins folder.

Hooks enqueue bounded, minimal observations. A worker sends authenticated local
UDP messages; loss and queue drops are surfaced via the admin-only status API.
Raw headers, request bodies, query strings, event payloads and command arguments
are deliberately omitted. Serial responses are counted, not copied.
"""

from datetime import datetime, timezone
import json
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
                     octoprint.plugin.SimpleApiPlugin):
    def __init__(self):
        self._queue = queue.Queue(maxsize=1024)
        self._stop = threading.Event()
        self._worker = None
        self._lock = threading.Lock()
        self._sent = self._dropped = self._errors = self._received = 0
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
            self._queue.put_nowait(event)
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

    def gcode_sent(self, comm, phase, cmd, cmd_type, gcode, *args, **kwargs):
        if self._flags.get('gcode'):
            code = gcode if isinstance(gcode, str) and re.fullmatch(r'[GMT][0-9]+', gcode) else 'other'
            self._emit('firmware', 'printer.command', 'send', 'sent', {'gcode': code})
        # None leaves the serial command unchanged.

    def gcode_received(self, comm, line, *args, **kwargs):
        with self._lock:
            self._received += 1
        return line

    def after_request(self, response):
        from flask import request
        rule = request.url_rule.rule if request.url_rule else 'unmatched'
        if self._flags.get('http') and rule != '/api/plugin/<string:name>':
            self._emit('api', 'http.request', request.method, 'observed',
                       {'route': rule[:256], 'status': response.status_code})
        return response

    def request_hooks(self, *args, **kwargs):
        return [self.after_request]

    def is_api_protected(self):
        return True

    def is_api_adminonly(self):
        return True

    def on_api_get(self, request):
        from flask import jsonify
        with self._lock:
            return jsonify(instance_id=self._settings.get(['instance_id']),
                           active=bool(self._worker and self._worker.is_alive()),
                           sent=self._sent, dropped=self._dropped, errors=self._errors,
                           serial_received=self._received, queued=self._queue.qsize())


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
