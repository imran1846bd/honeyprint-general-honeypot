"""Capture bounded G-code artifacts as data, without executing uploaded code."""
import hashlib


def job_intake(content, *, name='job.gcode'):
    if type(content) is not bytes or not 1 <= len(content) <= 1048576:
        raise ValueError('Job must contain at most one MiB of bytes')
    if not isinstance(name, str) or not 1 <= len(name) <= 128 or any(c in name for c in '/\\\x00'):
        raise ValueError('Invalid artifact name')
    text = content.decode('utf-8', errors='strict')
    if '\x00' in text: raise ValueError('Binary jobs are unsupported')
    commands = tuple(line.split(';', 1)[0].strip() for line in text.splitlines())
    commands = tuple(line for line in commands if line)
    if not commands or len(commands) > 10000 or any(len(line) > 4096 for line in commands):
        raise ValueError('Job command limits exceeded')
    return {'name': name, 'sha256': hashlib.sha256(content).hexdigest(),
            'size_bytes': len(content), 'commands': commands, 'stage': 'job_intake'}
