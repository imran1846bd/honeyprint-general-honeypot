"""Load only selected platform settings from a fixed configuration directory."""
from pathlib import Path
from core.config_loader import load_config

ROOT = Path(__file__).resolve().parents[1]

FIELDS = {
    'octoprint': {'python', 'source', 'settings'},
    'klipper': {'source'},
    'custom': {'code', 'api_url', 'python', 'health_path'},
}


def load_platform_settings(platform, base):
    base = Path(base).absolute()
    result = {}
    selected = list(FIELDS) if platform == 'all' else [platform]
    for name in selected:
        local = base / 'configs/platforms' / (name + '.yaml')
        # An external env directory without a platform folder uses shipped defaults.
        folder_exists = (base / 'configs/platforms').exists()
        path = local if folder_exists else ROOT / 'configs/platforms' / (name + '.yaml')
        if path.stat().st_size > 65536: raise ValueError('Platform configuration exceeds size limit')
        data = load_config(path)
        if type(data.get('schema_version')) is not int or data['schema_version'] != 1 or set(data) - (FIELDS[name] | {'schema_version'}):
            raise ValueError(f'{name}: invalid platform configuration schema')
        for field in FIELDS[name]:
            if field == 'settings':
                from deployment.octoprint_settings import resolve_octoprint_settings
                result['_octoprint_settings'] = resolve_octoprint_settings(data.get(field, {}))
                continue
            value = data.get(field, '/' if field == 'health_path' else '')
            if not isinstance(value, str): raise ValueError(f'{name}.{field}: expected string')
            if field in ('source', 'code', 'python') and value:
                p = Path(value).expanduser()
                # Paths in configs/platforms/*.yaml are relative to that project root.
                value = str(p if p.is_absolute() else path.parent.parent.parent / p)
            suffix = {'code': 'CODE', 'api_url': 'API_URL', 'python': 'PYTHON', 'source': 'SOURCE', 'health_path': 'HEALTH_PATH'}[field]
            result['HONEYPRINT_' + name.upper() + '_' + suffix] = value
    return result
