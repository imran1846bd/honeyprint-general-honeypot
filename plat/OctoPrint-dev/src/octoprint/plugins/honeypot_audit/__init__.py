"""Server-side audit collector for the local research dashboard."""
import base64
import json
import threading
import time
import uuid

import flask
import octoprint.plugin
from .store import record, heartbeat


class HoneypotAuditPlugin(octoprint.plugin.StartupPlugin,
                          octoprint.plugin.ShutdownPlugin,
                          octoprint.plugin.EventHandlerPlugin):
    def __init__(self):
        self._actor_lock = threading.Lock()
        self._print_actor = None

    def request_actor(self):
        if not flask.has_request_context():
            return None
        from octoprint.server import current_user
        user = None
        if current_user is not None and getattr(current_user, "is_authenticated", False):
            user = current_user.get_name()
        return {
            "user": user,
            "ip": flask.request.remote_addr or "",
            "request_id": getattr(flask.g, "audit_id", ""),
            "forwarded_for_claim": flask.request.headers.get("X-Forwarded-For", ""),
            "attribution": "request",
        }

    def on_startup(self, host, port):
        from octoprint.server import app
        if getattr(app, '_honeypot_audit_installed', False):
            return
        app._honeypot_audit_installed = True

        @app.before_request
        def audit_begin():
            flask.g.audit_started = time.monotonic()
            flask.g.audit_id = uuid.uuid4().hex

        @app.after_request
        def audit_end(response):
            try:
                req = flask.request
                details = {
                    'request_id': getattr(flask.g, 'audit_id', ''),
                    'actor': self.request_actor(),
                    'query': req.args.to_dict(flat=False),
                    'user_agent': req.headers.get('User-Agent', ''),
                    'forwarded_for_claim': req.headers.get('X-Forwarded-For', ''),
                    'content_type': req.content_type,
                    'content_length': req.content_length,
                    'duration_ms': round((time.monotonic() - getattr(flask.g, 'audit_started', time.monotonic())) * 1000, 2),
                }
                # Bound capture; avoid reading streaming uploads or retaining credentials.
                if req.content_length is not None and req.content_length <= 65536:
                    if req.is_json:
                        details['body'] = req.get_json(silent=True)
                    elif req.mimetype == 'application/x-www-form-urlencoded':
                        details['form'] = req.form.to_dict(flat=False)
                if req.mimetype == 'multipart/form-data':
                    details['body_capture'] = 'upload content omitted; see upload events'
                elif req.content_length and req.content_length > 65536:
                    details['body_capture'] = 'omitted: exceeds 64 KiB'
                record('http', req.method + ' ' + req.path, req.remote_addr or '',
                       response.status_code, details)
                body = details.get('body')
                if 200 <= response.status_code < 300 and isinstance(body, dict):
                    starts_job = (
                        (req.path == '/api/job' and body.get('command') in ('start', 'restart'))
                        or (req.path.startswith('/api/files/') and body.get('command') == 'select'
                            and body.get('print') is True)
                    )
                    if starts_job:
                        with self._actor_lock:
                            self._print_actor = self.request_actor()
            except Exception:
                self._logger.exception('Could not record request')
            return response

        self._audit_stop = threading.Event()
        def keep_alive():
            while not self._audit_stop.is_set():
                heartbeat()
                self._audit_stop.wait(5)
        threading.Thread(target=keep_alive, name='audit-heartbeat', daemon=True).start()
        record('system', 'Collector started', details={'host': host, 'port': port})

    def on_shutdown(self):
        if hasattr(self, '_audit_stop'):
            self._audit_stop.set()
        record('system', 'Collector stopped')

    def on_event(self, event, payload):
        details = dict(payload) if isinstance(payload, dict) else {'payload': payload}
        if details.get('username') and not details.get('user'):
            details['user'] = details['username']
        actor = self.request_actor()
        if actor:
            details['actor'] = actor
        record('event', event, source=actor.get('ip', '') if actor else '', details=details)
        if event in ('PrintDone', 'PrintCancelled', 'PrintFailed', 'Disconnected'):
            with self._actor_lock:
                self._print_actor = None

    def on_gcode(self, comm, phase, cmd, cmd_type, gcode, *args, **kwargs):
        tags = set(kwargs.get('tags') or [])
        actor = None
        for tag in tags:
            if tag.startswith('audit-actor:'):
                try:
                    actor = json.loads(base64.urlsafe_b64decode(tag[len('audit-actor:'):]).decode())
                except (ValueError, UnicodeError):
                    pass
        additions = set()
        if actor is None and phase == 'queuing':
            actor = self.request_actor()
            if actor is None and 'source:file' in tags:
                with self._actor_lock:
                    if self._print_actor:
                        actor = dict(self._print_actor, attribution='print_initiator')
            if actor:
                encoded = base64.urlsafe_b64encode(json.dumps(actor).encode()).decode()
                additions.add('audit-actor:' + encoded)
        details = {'gcode': gcode, 'tags': sorted(t for t in tags if not t.startswith('audit-actor:')),
                   'actor': actor, 'attribution': actor['attribution'] if actor else 'unknown_or_automatic'}
        record('command', phase + ': ' + cmd, source=actor.get('ip', '') if actor else '', details=details)
        if additions:
            # Preserve command text/type; attach metadata for the asynchronous sent phase.
            return [(cmd, cmd_type, additions)]
        return None


__plugin_name__ = 'Honeypot Audit'
__plugin_pythoncompat__ = '>=3.9,<4'


def __plugin_load__():
    global __plugin_implementation__, __plugin_hooks__
    __plugin_implementation__ = HoneypotAuditPlugin()
    __plugin_hooks__ = {
        'octoprint.comm.protocol.gcode.queuing': __plugin_implementation__.on_gcode,
        'octoprint.comm.protocol.gcode.sent': __plugin_implementation__.on_gcode,
    }
