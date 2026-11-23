"""Validate data-only multipart G-code uploads and retain immutable evidence."""
from email import policy
from email.parser import BytesParser
import mmap
from pathlib import Path
import re
import uuid

from core.gcode_files import valid_directory, valid_gcode_name

MAX_UPLOAD=1179648


class UploadRejected(ValueError):
    """An upload HoneyPrint refused, with the submitted filename kept for evidence."""
    def __init__(self, reason, filename=None):
        super().__init__(reason)
        self.filename = filename if isinstance(filename, str) else None


def prepare_upload(body,content_type,artifacts,*,max_file_bytes=1048576):
    if artifacts is None: raise ValueError('Artifact retention is unavailable')
    if not isinstance(body,bytes) or not 1<=len(body)<=MAX_UPLOAD:
        raise ValueError('Upload exceeds the limit')
    if not content_type.startswith('multipart/form-data;') or '\r' in content_type or '\n' in content_type:
        raise ValueError('A multipart G-code upload is required')
    message=BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: '+content_type.encode('ascii')+b'\r\nMIME-Version: 1.0\r\n\r\n'+body)
    if not message.is_multipart() or message.defects: raise ValueError('Invalid multipart data')
    parts=list(message.iter_parts())
    if not 1<=len(parts)<=6: raise ValueError('Too many form fields')
    values={};filename=None;content=None
    for part in parts:
        name=part.get_param('name',header='content-disposition')
        if part.get_content_disposition()!='form-data' or name in values or part.is_multipart():
            raise ValueError('Invalid form field')
        data=part.get_payload(decode=True)
        if not isinstance(data,bytes): raise ValueError('Invalid form data')
        if name=='file':
            filename=part.get_filename()
            if not valid_gcode_name(filename):
                raise UploadRejected('Unsupported G-code filename',filename)
            if not 1<=len(data)<=max_file_bytes: raise UploadRejected('G-code file exceeds the limit',filename)
            if b'\0' in data: raise UploadRejected('Binary content is not G-code text',filename)
            content=data
        elif name not in ('root','path','print') or len(data)>128:
            raise ValueError('Unsupported form field')
        values[name]=data
    if content is None or values.get('root',b'gcodes')!=b'gcodes' or values.get('path',b'') not in (b'',b'/'):
        raise UploadRejected('Uploads are limited to the G-code root',filename)
    if values.get('print',b'false').lower() not in (b'false',b'0',b'true',b'1'):
        raise ValueError('Invalid print option')
    record=artifacts.store(content)
    # Rebuild the form using validated fields; never pass attacker-selected roots or headers.
    boundary='honeyprint-'+uuid.uuid4().hex
    pieces=[]
    for name,value in [('root',b'gcodes'),('print',values.get('print',b'false'))]:
        pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()+value+b'\r\n')
    pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()+content+b'\r\n')
    pieces.append(f'--{boundary}--\r\n'.encode())
    return b''.join(pieces),'multipart/form-data; boundary='+boundary,record


def prepare_upload_file(path, content_type, artifacts):
    """Parse a spooled Mainsail upload and retain its G-code without a size cap."""
    if artifacts is None or not content_type.startswith('multipart/form-data;'):
        raise ValueError('A multipart G-code upload is required')
    if '\r' in content_type or '\n' in content_type or len(content_type) > 512:
        raise ValueError('Invalid multipart content type')
    match = re.search(r'(?:^|;)\s*boundary=(?:"([A-Za-z0-9_\-]{1,70})"|([A-Za-z0-9_\-]{1,70}))(?:;|$)', content_type)
    if not match: raise ValueError('Multipart boundary is missing')
    marker = b'--' + (match.group(1) or match.group(2)).encode('ascii')
    values = {}; file_part = None
    with Path(path).open('rb') as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
            if data[:len(marker)+2] != marker + b'\r\n': raise ValueError('Invalid multipart start')
            cursor = len(marker) + 2
            for _ in range(6):
                header_end = data.find(b'\r\n\r\n', cursor, cursor + 8192)
                if header_end < 0: raise ValueError('Invalid multipart header')
                header = BytesParser(policy=policy.default).parsebytes(data[cursor:header_end] + b'\r\n\r\n')
                if header.defects or header.get_content_disposition() != 'form-data':
                    raise ValueError('Invalid multipart field')
                name = header.get_param('name', header='content-disposition')
                if name in values or name not in ('file', 'root', 'path', 'print', 'checksum'):
                    raise ValueError('Unsupported multipart field')
                start = header_end + 4
                end = data.find(b'\r\n' + marker, start)
                if end < 0: raise ValueError('Unterminated multipart field')
                if name == 'file':
                    filename = header.get_filename()
                    if file_part: raise UploadRejected('Only one file per upload is supported', filename)
                    if not valid_gcode_name(filename): raise UploadRejected('Unsupported G-code filename', filename)
                    if end == start: raise UploadRejected('Empty G-code file', filename)
                    file_part = (filename, start, end - start)
                    values[name] = b''
                else:
                    if end - start > 128: raise ValueError('Multipart field too long')
                    values[name] = data[start:end]
                cursor = end + 2 + len(marker)
                if data[cursor:cursor+2] == b'--':
                    if data[cursor+2:] not in (b'', b'\r\n'): raise ValueError('Invalid multipart ending')
                    break
                if data[cursor:cursor+2] != b'\r\n': raise ValueError('Invalid multipart boundary')
                cursor += 2
            else: raise ValueError('Too many multipart fields')
    submitted = file_part[0] if file_part else None
    if not file_part: raise UploadRejected('No G-code file in the upload')
    if values.get('root', b'gcodes') != b'gcodes':
        raise UploadRejected('Uploads are limited to the G-code root', submitted)
    directory = values.get('path', b'').decode('utf-8').strip('/')
    if not valid_directory(directory):
        raise UploadRejected('Invalid G-code directory', submitted)
    print_flag = values.get('print', b'false').lower()
    if print_flag not in (b'false', b'0', b'true', b'1'):
        raise UploadRejected('Invalid print option', submitted)
    filename, start, length = file_part
    checksum = values.get('checksum')
    if checksum is not None:
        checksum = checksum.decode('ascii').lower()
        if not re.fullmatch(r'[0-9a-f]{64}', checksum): raise UploadRejected('Invalid G-code checksum', submitted)
    try:
        record = artifacts.store_file(path, start, length, expected_digest=checksum)
    except ValueError as error:
        raise UploadRejected(str(error), submitted) from None
    return filename, directory, print_flag in (b'true', b'1'), record
