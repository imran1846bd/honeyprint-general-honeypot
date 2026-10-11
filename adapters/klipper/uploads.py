"""Validate data-only multipart G-code uploads and retain immutable evidence."""
from email import policy
from email.parser import BytesParser
import re
import uuid

MAX_UPLOAD=1179648


def prepare_upload(body,content_type,artifacts):
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
            if not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.gcode',filename,re.I):
                raise ValueError('Only simple G-code filenames are supported')
            if not 1<=len(data)<=1048576 or b'\0' in data: raise ValueError('Invalid G-code artifact')
            data.decode('utf-8')
            content=data
        elif name not in ('root','path','print') or len(data)>128:
            raise ValueError('Unsupported form field')
        values[name]=data
    if content is None or values.get('root',b'gcodes')!=b'gcodes' or values.get('path',b'') not in (b'',b'/'):
        raise ValueError('Uploads are limited to the G-code root')
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
