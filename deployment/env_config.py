"""Build ordinary validated framework configuration from a literal local env file."""
import os
from pathlib import Path
import re
from core.config_loader import load_config
from core.profile_loader import load_profile

ROOT=Path(__file__).resolve().parents[1]
KEYS={'HONEYPRINT_PLATFORM','HONEYPRINT_PORT','HONEYPRINT_DASHBOARD_PORT','HONEYPRINT_PROFILE',
      'HONEYPRINT_WIDTH','HONEYPRINT_DEPTH','HONEYPRINT_HEIGHT','HONEYPRINT_NOZZLE',
      'HONEYPRINT_HEATED_BED','HONEYPRINT_RUNTIME_DIR','HONEYPRINT_OCTOPRINT_PYTHON',
      'HONEYPRINT_CUSTOM_CODE','HONEYPRINT_CUSTOM_PYTHON','HONEYPRINT_CUSTOM_HEALTH_PATH',
      'HONEYPRINT_CUSTOM_API_URL','HONEYPRINT_ANALYST_ENABLED','HONEYPRINT_INSTANCE_ID','HONEYPRINT_OCTOPRINT_SOURCE','HONEYPRINT_KLIPPER_SOURCE'}


def read_env(path):
    """Never source a shell file or interpolate commands, secrets or variables."""
    path=Path(path)
    values={}
    if path.exists():
        if path.stat().st_size>65536:raise ValueError('Env file exceeds the size limit')
        for number,line in enumerate(path.read_text().splitlines(),1):
            line=line.strip()
            if not line or line.startswith('#'):continue
            if line.startswith('export '):line=line[7:].strip()
            key,separator,value=line.partition('=')
            key=key.strip();value=value.strip()
            if not separator or key not in KEYS or key in values:
                raise ValueError(f'Invalid or duplicate HoneyPrint env key on line {number}')
            if value[:1] in ('"',"'"):
                if len(value)<2 or value[-1]!=value[0]:raise ValueError(f'Unclosed env value on line {number}')
                value=value[1:-1]
            values[key]=value
    # Explicit process environment overrides values in the selected env file.
    for key in KEYS:
        if key in os.environ:values[key]=os.environ[key]
    return values


def port(value,default):
    value=value or str(default)
    if not re.fullmatch(r'[0-9]{1,5}',value) or not 1<=int(value)<=65535:
        raise ValueError('Env ports must be integers from 1 to 65535')
    return int(value)


def build_config(values,base,output):
    """Resolve automatic platform companions, then write transient standard YAML."""
    import yaml
    base=Path(base).absolute();output=Path(output);output.mkdir(parents=True,exist_ok=True)
    def path(key,default=None):
        value=values.get(key,'') or default
        if value is None:return None
        p=Path(value).expanduser();return p.absolute() if p.is_absolute() else (base/p).absolute()
    platform=values.get('HONEYPRINT_PLATFORM','octoprint').strip().lower()
    names={'octoprint':'octoprint','klipper':'klipper','custom':'custom'}
    if platform not in (*names,'all'):raise ValueError('HONEYPRINT_PLATFORM must be all, octoprint, klipper or custom')
    octoprint_settings = None
    octoprint_source = path('HONEYPRINT_OCTOPRINT_SOURCE')
    octoprint_python = path('HONEYPRINT_OCTOPRINT_PYTHON')
    if platform in ('octoprint', 'all'):
        from deployment.octoprint_settings import resolve_octoprint_settings
        octoprint_file = base / 'configs/platforms/octoprint.yaml'
        if not octoprint_file.is_file() and not (base / 'configs/platforms').exists():
            octoprint_file = ROOT / 'configs/platforms/octoprint.yaml'
        if octoprint_file.stat().st_size > 65536:
            raise ValueError('OctoPrint platform configuration exceeds size limit')
        octoprint_data = load_config(octoprint_file)
        if (octoprint_data.get('schema_version') != 1 or
                set(octoprint_data) - {'schema_version', 'source', 'python', 'settings'}):
            raise ValueError('Invalid OctoPrint platform configuration schema')
        for field in ('source', 'python'):
            value = octoprint_data.get(field, '')
            if not isinstance(value, str):
                raise ValueError(f'OctoPrint {field} must be a path string')
            if value and (octoprint_source if field == 'source' else octoprint_python) is None:
                candidate = Path(value).expanduser()
                resolved = candidate if candidate.is_absolute() else octoprint_file.parent.parent.parent / candidate
                if field == 'source':
                    octoprint_source = resolved.absolute()
                else:
                    octoprint_python = resolved.absolute()
        octoprint_settings = resolve_octoprint_settings(octoprint_data.get('settings', {}))
    klipper_settings = None
    klipper_source = path('HONEYPRINT_KLIPPER_SOURCE')
    if platform in ('klipper', 'all'):
        from deployment.docker.klipper.settings import resolve_klipper_settings
        klipper_file = base / 'configs/platforms/klipper.yaml'
        if not klipper_file.is_file() and not (base / 'configs/platforms').exists():
            klipper_file = ROOT / 'configs/platforms/klipper.yaml'
        if klipper_file.stat().st_size > 65536:
            raise ValueError('Klipper platform configuration exceeds size limit')
        klipper_data = load_config(klipper_file)
        if (type(klipper_data.get('schema_version')) is not int or
                klipper_data['schema_version'] != 1 or
                set(klipper_data) - {'schema_version', 'source', 'settings'}):
            raise ValueError('Invalid Klipper platform configuration schema')
        source_value = klipper_data.get('source', '')
        if not isinstance(source_value, str):
            raise ValueError('Klipper source must be a path string')
        if source_value and klipper_source is None:
            candidate = Path(source_value).expanduser()
            klipper_source = (candidate if candidate.is_absolute() else
                              klipper_file.parent.parent.parent / candidate).absolute()
        klipper_settings = resolve_klipper_settings(klipper_data.get('settings', {}))
    config=load_config(ROOT/'configs/instances/all.yaml' if platform=='all' else ROOT/f'configs/instances/{names[platform]}.yaml')
    profile=load_profile(path('HONEYPRINT_PROFILE',str(ROOT/'configs/profiles/virtual_lab.yaml')))
    for key,dimension in [('HONEYPRINT_WIDTH','width'),('HONEYPRINT_DEPTH','depth'),('HONEYPRINT_HEIGHT','height')]:
        if values.get(key):profile['volume'][dimension]=float(values[key])
    if values.get('HONEYPRINT_NOZZLE'):profile['nozzle_diameter']=float(values['HONEYPRINT_NOZZLE'])
    if values.get('HONEYPRINT_HEATED_BED'):
        value=values['HONEYPRINT_HEATED_BED'].lower()
        if value not in ('true','false'):raise ValueError('HONEYPRINT_HEATED_BED must be true or false')
        profile['heated_bed']=value=='true'
    profile_path=output/'profile.yaml';profile_path.write_text(yaml.safe_dump(profile,sort_keys=False))
    load_profile(profile_path)
    if platform=='all' and (values.get('HONEYPRINT_PORT') or values.get('HONEYPRINT_INSTANCE_ID')):
        raise ValueError('A single port or instance ID requires selection of one platform')
    if values.get('HONEYPRINT_CUSTOM_CODE') and values.get('HONEYPRINT_CUSTOM_API_URL'):
        raise ValueError('Choose a custom code path or API URL, not both')
    analyst = values.get('HONEYPRINT_ANALYST_ENABLED', 'false').lower()
    if analyst not in ('true', 'false'):raise ValueError('HONEYPRINT_ANALYST_ENABLED must be true or false')
    for instance in config['instances']:
        instance['printer_profile']=str(profile_path.absolute())
        if instance['platform'] == 'octoprint':
            instance['octoprint_settings'] = octoprint_settings
        elif instance['platform'] == 'klipper':
            instance['klipper_settings'] = klipper_settings
        if values.get('HONEYPRINT_INSTANCE_ID'):instance['id']=values['HONEYPRINT_INSTANCE_ID']
        if platform != 'all':instance['services']['web_port']=port(values.get('HONEYPRINT_PORT'),8081)
        if instance['platform']=='custom_lab' and values.get('HONEYPRINT_CUSTOM_CODE'):
            entry=path('HONEYPRINT_CUSTOM_CODE')
            if not entry.is_file() or entry.suffix!='.py':raise ValueError('Custom code must select an existing Python entry file')
            instance['platform']='custom';instance['simulation']='custom_process';instance['stages']=[]
        if instance['platform']=='custom_lab' and values.get('HONEYPRINT_CUSTOM_API_URL'):
            from adapters.custom.api_adapter import parse_api_url
            parse_api_url(values['HONEYPRINT_CUSTOM_API_URL'])
            instance.update(platform='custom', deployment='attach', simulation='custom_api', stages=[])
    config_path=output/'config.yaml';config_path.write_text(yaml.safe_dump(config,sort_keys=False))
    return {'config':config,'config_path':config_path,
            'runtime_dir':path('HONEYPRINT_RUNTIME_DIR',str(ROOT/'.honeyprint/quickstart')),
            'dashboard_port':port(values.get('HONEYPRINT_DASHBOARD_PORT'),8090),
            'octoprint_python':octoprint_python,
            'octoprint_source':octoprint_source if platform in ('all','octoprint') else None,
            'klipper_source':klipper_source if platform in ('all','klipper') else None,
            'analyst_enabled':analyst == 'true',
            'custom_api_url':values.get('HONEYPRINT_CUSTOM_API_URL') if platform in ('all','custom') else None,
            'custom_code':path('HONEYPRINT_CUSTOM_CODE') if platform in ('all','custom') else None,
            'custom_python':path('HONEYPRINT_CUSTOM_PYTHON'),
            'custom_health_path':values.get('HONEYPRINT_CUSTOM_HEALTH_PATH') or '/'}
