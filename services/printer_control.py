"""Bounded control of the selected, locally managed virtual printer.

The network consoles never execute a host shell. Only the commands below are
translated to the selected platform's loopback API.
"""

import http.client
import json
from pathlib import Path
import re
from uuid import uuid4


GCODE = re.compile(r'^(?:G0|G1|G28|G90|G91|M82|M83|M104|M105|M114|M115|M140)\b(?: [A-Za-z][+-]?(?:\d+(?:\.\d*)?|\.\d+))*$', re.I)
TEMPERATURE = re.compile(r'^(?:0|[1-9][0-9]{0,2})(?:\.[0-9])?$')
FILENAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.gcode$', re.I)
MAX_UPLOAD = 1048576

COMMANDS = (
    'status / temperature / position: read current virtual printer state',
    'gcodes: list available G-code with examples',
    'files: list uploaded G-code files',
    'upload NAME.gcode: enter G-code lines; finish with a single . line',
    'show NAME.gcode: preview a G-code file',
    'select NAME.gcode: load a file without starting it (OctoPrint/custom)',
    'print NAME.gcode: start an uploaded G-code file',
    'home: home the virtual axes (G28)',
    'move X10 Y10 F600: request virtual motion (G1)',
    'heat 180 / bed 50 / cooldown: set virtual temperature targets',
    'run G1 X10 F600: send one allowed G-code directly',
    'connect: connect OctoPrint to its virtual printer',
    'start / pause / resume / cancel: control a prepared print job',
    'job: show the selected file and current print progress',
)

GCODE_HELP = (
    'G0/G1 X.. Y.. Z.. E.. F..  virtual motion/extrusion',
    'G28  home axes; G90/G91  absolute/relative positioning',
    'M82/M83  absolute/relative extrusion',
    'M104 S..  hotend target; M140 S..  bed target',
    'M105  temperature; M114  position where available; M115  identity',
)


class ControlError(Exception):
    pass


class PrinterControl:
    def __init__(self, platform, port, runtime_dir=None, instance_id=None):
        if platform not in ('octoprint', 'klipper', 'custom_lab') or not 1 <= port <= 65535:
            raise ValueError('Unsupported printer control target')
        self.platform = platform
        self.port = port
        self.credentials = (Path(runtime_dir) / instance_id / 'credentials.json'
                            if platform == 'octoprint' and runtime_dir and instance_id else None)

    def _request(self, method, path, body=None):
        headers = {'Content-Type': 'application/json'}
        if self.platform == 'octoprint':
            try:
                credentials = json.loads(self.credentials.read_text())
                headers['X-Api-Key'] = credentials['api_key']
            except (AttributeError, OSError, KeyError, ValueError) as exc:
                raise ControlError('OctoPrint credentials unavailable') from exc
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            payload = response.read(65537)
            if len(payload) > 65536:
                raise ControlError('Printer response too large')
            if not 200 <= response.status < 300:
                raise ControlError(f'Printer rejected command (HTTP {response.status})')
            return json.loads(payload) if payload else {}
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise ControlError('Printer API unavailable') from exc
        finally:
            connection.close()

    def snapshot(self):
        if self.platform == 'custom_lab':
            data = self._request('GET', '/api/state')
            job_state = data.get('job_state', 'idle')
            return {'state': 'stopped' if not data['powered'] else job_state,
                    'tool': data['temperature'], 'tool_target': data['target_temperature'],
                    'bed': data['bed_temperature'], 'bed_target': data['bed_target'],
                    'position': data['position'], 'job': data.get('job_name'),
                    'job_state': data.get('job_state'), 'progress': data.get('job_progress')}
        if self.platform == 'klipper':
            data = self._request('GET', '/printer/objects/query?print_stats&toolhead&extruder&heater_bed&virtual_sdcard')
            status = data['result']['status']
            tool = status['extruder']
            bed = status.get('heater_bed', {})
            job = status.get('print_stats', {})
            return {'state': status['print_stats']['state'], 'tool': tool['temperature'],
                    'tool_target': tool['target'], 'bed': bed.get('temperature', 0),
                    'bed_target': bed.get('target', 0), 'position': status['toolhead']['position'],
                    'job': job.get('filename') or None,
                    'progress': status.get('virtual_sdcard', {}).get('progress')}
        try:
            data = self._request('GET', '/api/printer')
        except ControlError:
            connection = self._request('GET', '/api/connection')
            return {'state': connection.get('current', {}).get('state', 'Disconnected'),
                    'tool': 0, 'tool_target': 0, 'bed': 0, 'bed_target': 0}
        temperature = data.get('temperature', {})
        tool = temperature.get('tool0', {})
        bed = temperature.get('bed', {})
        job = self._request('GET', '/api/job')
        return {'state': data.get('state', {}).get('text', 'unknown'),
                'tool': tool.get('actual', 0), 'tool_target': tool.get('target', 0),
                'bed': bed.get('actual', 0), 'bed_target': bed.get('target', 0),
                'job': job.get('job', {}).get('file', {}).get('name'),
                'progress': job.get('progress', {}).get('completion')}

    def files(self):
        if self.platform == 'custom_lab':
            records = self._request('GET', '/api/files').get('files', [])
            return 'G-code files: ' + (', '.join(records[:20]) if records else '(none)')
        elif self.platform == 'octoprint':
            data = self._request('GET', '/api/files')
            records = data.get('files', [])
        else:
            data = self._request('GET', '/server/files/list?root=gcodes')
            records = data.get('result', [])
        if not isinstance(records, list):
            raise ControlError('Invalid printer file list')
        names = [name for item in records[:20] if isinstance(item, dict)
                 for name in [item.get('name', item.get('path'))]
                 if isinstance(name, str) and FILENAME.fullmatch(name)]
        return 'G-code files: ' + (', '.join(names) if names else '(none)')

    def upload(self, name, content):
        if not FILENAME.fullmatch(name):
            raise ControlError('Use a simple .gcode filename')
        if not isinstance(content, bytes) or not 1 <= len(content) <= MAX_UPLOAD or b'\0' in content:
            raise ControlError('G-code file must be 1 byte to 1 MiB of UTF-8 text')
        try:
            decoded = content.decode('utf-8')
        except UnicodeError as exc:
            raise ControlError('G-code file must be UTF-8 text') from exc
        if self.platform == 'custom_lab':
            self._request('POST', '/api/files', {'name':name, 'gcode':decoded})
            return 'Uploaded ' + name
        boundary = 'honeyprint-' + uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
        headers = {'Content-Type': 'multipart/form-data; boundary=' + boundary}
        if self.platform == 'octoprint':
            try:
                headers['X-Api-Key'] = json.loads(self.credentials.read_text())['api_key']
            except (AttributeError, OSError, KeyError, ValueError) as exc:
                raise ControlError('OctoPrint credentials unavailable') from exc
        path = '/api/files/local' if self.platform == 'octoprint' else '/server/files/upload'
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            connection.request('POST', path, body, headers)
            response = connection.getresponse()
            response.read(65537)
            if not 200 <= response.status < 300:
                raise ControlError(f'G-code upload rejected (HTTP {response.status})')
            return 'Uploaded ' + name
        except (OSError, http.client.HTTPException) as exc:
            raise ControlError('Printer upload API unavailable') from exc
        finally:
            connection.close()

    def job(self):
        if self.platform == 'custom_lab':
            data = self._request('GET', '/api/job')
            progress = data.get('progress', 0)
            return f"Job: {data.get('filename') or data.get('selected') or '(none)'}\nState: {data.get('state','unknown')}\nProgress: {progress:.1f}%"
        if self.platform == 'octoprint':
            data = self._request('GET', '/api/job')
            file = data.get('job', {}).get('file', {}).get('name') or '(none)'
            state = data.get('state', 'unknown')
            completion = data.get('progress', {}).get('completion')
        else:
            data = self._request('GET', '/printer/objects/query?print_stats&virtual_sdcard')
            status = data['result']['status']
            file = status.get('print_stats', {}).get('filename') or '(none)'
            state = status.get('print_stats', {}).get('state', 'unknown')
            completion = status.get('virtual_sdcard', {}).get('progress')
            if isinstance(completion, (int, float)):
                completion *= 100
        progress = f'{completion:.1f}%' if isinstance(completion, (int, float)) else 'unknown'
        return f'Job: {file}\nState: {state}\nProgress: {progress}'

    def file_action(self, action, name):
        if not FILENAME.fullmatch(name):
            raise ControlError('Use a simple .gcode filename from files')
        if action in ('select', 'print'):
            if action == 'select' and self.platform == 'klipper':
                raise ControlError('Separate file selection is unavailable for Klipper; use print NAME.gcode')
            if self.platform == 'octoprint':
                self._request('POST', '/api/files/local/' + name,
                              {'command': 'select', 'print': action == 'print'})
            elif self.platform == 'custom_lab':
                self._request('POST', '/api/job', {'command':'select','filename':name})
                if action == 'print':
                    self._request('POST', '/api/job', {'command':'start'})
            else:
                self._request('POST', '/printer/print/start', {'filename': name})
            return ('File selected: ' if action == 'select' else 'Print requested for ') + name
        path = ('/downloads/files/local/' if self.platform == 'octoprint'
                else '/api/files/' if self.platform == 'custom_lab'
                else '/server/files/gcodes/') + name
        headers = {}
        if self.platform == 'octoprint':
            try:
                headers['X-Api-Key'] = json.loads(self.credentials.read_text())['api_key']
            except (AttributeError, OSError, KeyError, ValueError) as exc:
                raise ControlError('OctoPrint credentials unavailable') from exc
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise ControlError(f'G-code file unavailable (HTTP {response.status})')
            raw = response.read(4097)
            preview = raw[:4096].decode('utf-8', 'replace')
            lines = [''.join(c if c.isprintable() or c == '\t' else '?' for c in line)
                     for line in preview.splitlines()[:20]]
            return '\n'.join(lines) + ('\n[preview truncated]' if len(raw) > 4096 or len(preview.splitlines()) > 20 else '')
        except (OSError, http.client.HTTPException) as exc:
            raise ControlError('G-code file unavailable') from exc
        finally:
            connection.close()

    def execute(self, command):
        command = command.strip()
        verb = command.lower()
        if verb in ('help', '?', 'commands'):
            return '\n'.join(COMMANDS), {'state': 'information'}
        if verb in ('gcodes', 'gcode', 'codes'):
            return '\n'.join(GCODE_HELP), {'state': 'information'}
        if verb == 'files':
            return self.files(), self.snapshot()
        if verb == 'job':
            return self.job(), self.snapshot()
        parts = command.split(' ', 1)
        if len(parts) == 2 and parts[0].lower() in ('show', 'select', 'print'):
            return self.file_action(parts[0].lower(), parts[1]), self.snapshot()
        if verb == 'temperature':
            command = 'M105'
            verb = 'm105'
        if verb == 'position':
            command = 'M114'
            verb = 'm114'
        if verb == 'home':
            command = 'G28'
            verb = 'g28'
        if verb == 'cooldown':
            self._send_gcode('M104 S0')
            if self.snapshot()['bed_target']:
                self._send_gcode('M140 S0')
            return 'Temperature targets set to 0', self.snapshot()
        if len(parts) == 2 and parts[0].lower() in ('heat', 'bed'):
            value = parts[1]
            maximum = 280 if parts[0].lower() == 'heat' else 130
            if not TEMPERATURE.fullmatch(value) or float(value) > maximum:
                raise ControlError(f'Temperature must be 0..{maximum} C')
            command = ('M104' if parts[0].lower() == 'heat' else 'M140') + ' S' + value
            verb = command.split(' ', 1)[0].lower()
        elif len(parts) == 2 and parts[0].lower() in ('move', 'run'):
            command = ('G1 ' if parts[0].lower() == 'move' else '') + parts[1]
            verb = command.split(' ', 1)[0].lower()
        if verb in ('status', 'm105'):
            state = self.snapshot()
            return (f"Printer: {state['state']}\nT:{state['tool']:.1f} /{state['tool_target']:.1f} "
                    f"B:{state['bed']:.1f} /{state['bed_target']:.1f}", state)
        if verb == 'm114':
            state = self.snapshot()
            if 'position' not in state:
                raise ControlError('Position unavailable')
            return f"Position: {state['position']}", state
        if verb == 'm115':
            return f'FIRMWARE_NAME:HoneyPrint {self.platform} Virtual Printer', self.snapshot()
        if verb == 'connect' and self.platform == 'octoprint':
            self._request('POST', '/api/connection', {'command': 'connect', 'port': 'VIRTUAL'})
            return 'Virtual printer connection requested', {'state': 'connecting'}
        if verb in ('start', 'pause', 'resume', 'cancel'):
            if verb == 'start' and self.platform == 'octoprint' and not self.snapshot().get('job'):
                raise ControlError('No G-code file selected. Upload a file, then select NAME.gcode')
            if self.platform == 'klipper':
                self._request('POST', '/printer/print/' + verb)
            else:
                self._request('POST', '/api/job', {'command': verb})
            return f'{verb} accepted', self.snapshot()
        if not GCODE.fullmatch(command) or len(command) > 256:
            raise ControlError('Unsupported printer command')
        return self._send_gcode(command), self.snapshot()

    def _send_gcode(self, command):
        if self.platform == 'custom_lab':
            result = self._request('POST', '/api/command', {'command': command})
            reply = result.get('response', 'ok')
        elif self.platform == 'klipper':
            self._request('POST', '/printer/gcode/script', {'script': command})
            reply = 'ok'
        else:
            self._request('POST', '/api/printer/command', {'command': command})
            reply = 'ok'
        return reply
