"""Validate browser action metadata before it enters shared evidence storage."""

import re
from pathlib import Path


SCRIPT = (Path(__file__).with_name('ui_capture.js')).read_bytes()

# Every browser interaction the capture script reports (see ui_capture.js).
ACTIONS = frozenset(('click', 'double_click', 'context_menu', 'change', 'input', 'submit',
                     'file_select', 'file_drop', 'page_view', 'navigate', 'page_leave'))
FILE_ACTIONS = ('file_select', 'file_drop')
FIELDS = {'action', 'control', 'page', 'element', 'from', 'filename', 'size_bytes'}


def _text(value, limit):
    return isinstance(value, str) and 1 <= len(value) <= limit and not any(ord(c) < 32 for c in value)


def _page(value):
    return _text(value, 160) and value.startswith('/')


def observation(data):
    if not isinstance(data, dict) or not set(data) <= FIELDS:
        raise ValueError('Invalid UI action')
    action = data.get('action')
    if action not in ACTIONS:
        raise ValueError('Invalid UI action')
    if not _text(data.get('control'), 120):
        raise ValueError('Invalid UI control')
    if not _page(data.get('page')):
        raise ValueError('Invalid UI page')
    metadata = {'control': data['control'], 'page': data['page']}
    if 'element' in data:
        if not _text(data['element'], 40):
            raise ValueError('Invalid UI element')
        metadata['element'] = data['element']
    if 'from' in data:
        if action != 'navigate' or not _page(data['from']):
            raise ValueError('Invalid navigation origin')
        metadata['from'] = data['from']
    if action in FILE_ACTIONS:
        filename = data.get('filename')
        size = data.get('size_bytes')
        if (not isinstance(filename, str) or not re.fullmatch(r'[^/\\\x00-\x1f]{1,240}', filename)
                or type(size) is not int or size < 0):
            raise ValueError('Invalid selected file')
        metadata.update(filename=filename, size_bytes=size)
    elif set(data) & {'filename', 'size_bytes'}:
        raise ValueError('Unexpected UI fields')
    return {'source': 'webui', 'event_type': 'ui.interaction', 'action': action,
            'outcome': 'observed', 'simulation': True, 'metadata': metadata}
