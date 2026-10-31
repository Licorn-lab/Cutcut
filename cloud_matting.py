"""Opt-in Tencent COS/CI matting; credentials remain in macOS Keychain."""
import base64
import hashlib
import hmac
import io
import json
import re
import secrets
import shlex
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from PIL import Image
from core import atomic_json, load_image

SERVICE = 'CutCut-TencentCloud-v1'
PREFIX = 'cutcut-temporary/'
ROOT = Path(__file__).resolve().parent
JOURNAL = ROOT / 'data' / 'cloud-cleanup.json'


def validate_config(config):
    bucket = str(config.get('bucket', '')).strip()
    region = str(config.get('region', '')).strip()
    secret_id = str(config.get('secret_id', '')).strip()
    secret_key = str(config.get('secret_key', '')).strip()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,60}-[0-9]{5,20}', bucket):
        raise ValueError('存储桶名称应包含 APPID，例如 example-1234567890')
    if not re.fullmatch(r'ap-[a-z0-9-]{2,30}', region):raise ValueError('地域格式无效，例如 ap-guangzhou')
    if not re.fullmatch(r'[A-Za-z0-9]{10,128}', secret_id) or not re.fullmatch(r'[A-Za-z0-9]{10,128}', secret_key):
        raise ValueError('SecretId 或 SecretKey 格式无效')
    return dict(bucket=bucket, region=region, secret_id=secret_id, secret_key=secret_key)


def save_config(config):
    config = validate_config(config)
    payload = base64.b64encode(json.dumps(config).encode()).decode()
    # Feed through stdin, never expose the secret in the command-line arguments.
    cmd = 'add-generic-password -U -a cutcut -s ' + shlex.quote(SERVICE) + ' -w ' + shlex.quote(payload) + '\n'
    try:r = subprocess.run(['/usr/bin/security', '-i'], input=cmd, text=True, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:raise ValueError('无法访问 macOS 钥匙串') from exc
    if r.returncode or 'error:' in r.stderr.lower():raise ValueError('无法保存到钥匙串，请检查钥匙串是否解锁')


def get_config():
    try:r = subprocess.run(['/usr/bin/security', 'find-generic-password', '-a', 'cutcut', '-s', SERVICE, '-w'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:raise ValueError('无法访问 macOS 钥匙串') from exc
    if r.returncode:raise ValueError('请先配置腾讯云账号')
    try:return validate_config(json.loads(base64.b64decode(r.stdout.strip()).decode()))
    except (ValueError, TypeError, UnicodeError):raise ValueError('钥匙串中的腾讯云配置无效，请重新配置')


def public_config():
    try:c = get_config()
    except ValueError:return {'configured': False}
    return {'configured':True, 'bucket':c['bucket'], 'region':c['region']}


def signed_request(config, method, key, query=None, body=None):
    query = query or {}
    host = config['bucket'] + '.cos.' + config['region'] + '.myqcloud.com'
    path = '/' + quote(key, safe='/~-_.')
    query_string = urlencode(sorted(query.items()), quote_via=quote, safe='~-_.')
    now = int(time.time());key_time = f'{now-60};{now+900}'
    sign_key = hmac.new(config['secret_key'].encode(), key_time.encode(), hashlib.sha1).hexdigest()
    signed_headers = 'host=' + quote(host, safe='~-_.') + ('&x-cos-acl=private' if method == 'PUT' else '')
    header_list = 'host;x-cos-acl' if method == 'PUT' else 'host'
    canonical = method.lower() + '\n' + path + '\n' + query_string + '\n' + signed_headers + '\n'
    digest = hashlib.sha1(canonical.encode()).hexdigest()
    string_to_sign = 'sha1\n' + key_time + '\n' + digest + '\n'
    signature = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()
    auth = ('q-sign-algorithm=sha1&q-ak=' + config['secret_id'] + '&q-sign-time=' + key_time +
            '&q-key-time=' + key_time + '&q-header-list=' + header_list + '&q-url-param-list=' + ';'.join(sorted(query)) + '&q-signature=' + signature)
    headers={'Authorization':auth, 'Content-Type':('image/jpeg' if key.endswith('.jpg') else 'image/png') if body is not None else 'application/octet-stream'}
    if method == 'PUT':headers['x-cos-acl']='private'
    req = Request('https://' + host + path + ('?' + query_string if query else ''), data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=90) as response:
            content = response.read(40 * 1024 * 1024 + 1)
            if len(content) > 40 * 1024 * 1024:raise ValueError('云端返回图片过大')
            return content
    except HTTPError as exc:
        stage = {'PUT':'上传图片', 'GET':'云端抠图', 'DELETE':'清理临时文件'}.get(method, '请求')
        raise ValueError(f'{stage}失败（HTTP {exc.code}）；请检查该步骤的权限、存储桶地域及服务开通状态') from None
    except (URLError, TimeoutError, OSError):
        stage = {'PUT':'上传图片', 'GET':'云端抠图', 'DELETE':'清理临时文件'}.get(method, '请求')
        raise ValueError(f'{stage}失败：无法连接腾讯云，请检查网络后重试') from None


def pending():
    try:
        value = json.loads(JOURNAL.read_text())
        return value if isinstance(value, list) else []
    except (OSError, ValueError):return []


def save_pending(items):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(JOURNAL, items)


def cleanup(config):
    items = pending(); remaining=[]
    for entry in items:
        if entry.get('bucket') != config['bucket'] or entry.get('region') != config['region'] or not re.fullmatch(r'cutcut-temporary/[a-f0-9]{32}\.(?:png|jpg)', str(entry.get('key',''))):
            remaining.append(entry);continue
        try:signed_request(config, 'DELETE', entry['key'])
        except ValueError:remaining.append(entry)
    save_pending(remaining)
    return len(remaining)


def run(path, config):
    original = load_image(path)
    if original.width > 7680 or original.height > 7680 or min(original.size) > 4320 or min(original.size) < 32:
        raise ValueError('图片尺寸超出云端处理限制，请先缩小副本')
    # Preserve pixel dimensions; use a high-quality JPEG only if lossless PNG is too large.
    buffer=io.BytesIO();original.save(buffer, 'PNG');payload=buffer.getvalue();extension='png'
    if len(payload)>10*1024*1024:
        for quality in (95,90,85,80):
            buffer=io.BytesIO();original.convert('RGB').save(buffer,'JPEG',quality=quality,subsampling=0)
            payload=buffer.getvalue();extension='jpg'
            if len(payload)<=10*1024*1024:break
    if len(payload)>10*1024*1024:raise ValueError('用于云端处理的图片超过 10 MB，请先缩小副本')
    key=PREFIX + secrets.token_hex(16) + '.' + extension
    entries=pending();entries.append({'bucket':config['bucket'],'region':config['region'],'key':key});save_pending(entries)
    error=None;result=None
    try:
        signed_request(config,'PUT',key,body=payload)
        data=signed_request(config,'GET',key,{'ci-process':'AIPicMatting'})
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.size != original.size or 'A' not in image.getbands():raise ValueError('云端返回图片尺寸或透明通道异常，未更改当前结果')
            result=image.getchannel('A').copy()
    except (OSError, ValueError) as exc:error=exc
    remaining=cleanup(config)
    if error:raise ValueError(str(error) + ('；有临时文件待核实或清理，请在设置中重试清理' if remaining else '')) from None
    return result,remaining
