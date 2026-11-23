"""One G-code filename rule shared by every adapter and console.

Real printer front ends (Mainsail/Moonraker, OctoPrint) accept slicer names such
as ``Benchy [0.2mm] #2.gcode`` and the extensions .gcode, .gco, .g and .nc.
HoneyPrint must accept the same names, or uploads fail where a real printer would
not. The rule still blocks what is unsafe: path separators, traversal, hidden
files, quotes and control characters (the name is placed in a multipart header
and in evidence), and names longer than a filesystem allows.
"""

import re

GCODE_EXTENSIONS = ('.gcode', '.gco', '.g', '.nc')
MAX_NAME_BYTES = 240
_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\"]')
_UNPRINTABLE = re.compile(r'[\x00-\x1f\x7f"\\]')


def valid_gcode_name(name):
    """True for a plain G-code file name (no directory part)."""
    if not isinstance(name, str) or not name or name != name.strip() or name.startswith('.'):
        return False
    if len(name.encode('utf-8', 'surrogatepass')) > MAX_NAME_BYTES or _UNSAFE.search(name):
        return False
    lower = name.lower()
    return any(lower.endswith(ext) and len(lower) > len(ext) for ext in GCODE_EXTENSIONS)


def valid_directory(directory):
    """True for '' or a relative folder path such as 'parts/v2' inside the G-code root."""
    if directory == '':
        return True
    if not isinstance(directory, str) or len(directory.encode('utf-8', 'surrogatepass')) > MAX_NAME_BYTES:
        return False
    return all(part and part == part.strip() and not part.startswith('.') and not _UNSAFE.search(part)
               for part in directory.split('/'))


def safe_label(name, limit=120, *, path=False):
    """A bounded, printable copy of a submitted name for evidence (never used as a path).

    With path=True, '/' is kept so a submitted path such as gcodes/a.gcode stays readable.
    """
    text = name if isinstance(name, str) else ''
    return (_UNPRINTABLE if path else _UNSAFE).sub('?', text)[:limit]
