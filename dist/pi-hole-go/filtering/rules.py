"""Bounded DNS-only lists and transactional, official-schema gravity databases."""
import gzip
import io
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import time
import urllib.request

from .common import APP, UserError

MAX_BYTES = 8 * 1024 * 1024
MAX_RULES = 250000
LIST_URL = 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/light.txt'


def domain(value):
    if not isinstance(value, str):
        raise UserError('域名必须是文本')
    try:
        name = value.strip().rstrip('.').encode('idna').decode('ascii').lower()
        if len(name) > 253 or '.' not in name or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in name.split('.')):
            raise ValueError()
        try:
            ipaddress.ip_address(name)
        except ValueError:
            return name
    except (ValueError, UnicodeError):
        pass
    raise UserError('请填写域名，例如 example.com；不要包含网址路径、端口或通配符')


def manual_domains(value):
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list) or len(value) > 200:
        raise UserError('每份自定义名单最多 200 个域名')
    return sorted(set(domain(v) for v in value if v))


def parse_rules(payload):
    if len(payload) > MAX_BYTES:
        raise UserError('规则文件超过 8 MiB，已保留原规则')
    count = 0
    # Stream lines: splitlines() keeps tens of thousands of Python strings
    # alive at once, which is costly on the Go+ shared-memory budget.
    for raw in io.BytesIO(payload.removeprefix(b'\xef\xbb\xbf')):
        try:
            line = raw.decode('utf-8')
        except UnicodeError:
            raise UserError('规则文件不是 UTF-8 文本') from None
        line = line.strip()
        if not line or line.startswith(('!', '#', '[')):
            continue
        abp = line.startswith('||') and line.endswith('^')
        raw = line[2:-1] if abp else line
        # Only exact DNS names / simple ABP suffix rules. Never browser scripts.
        try:
            name = domain(raw)
        except UserError:
            raise UserError('规则包含不支持的格式，已保留原规则') from None
        count += 1
        if count > MAX_RULES:
            raise UserError('规则数超过设备上限，已保留原规则')
        yield '||' + name + '^' if abp else name
    if count == 0:
        raise UserError('规则为空，已保留原规则')


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith('https://'):
            raise UserError('规则下载禁止跳转到非 HTTPS 地址')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), HTTPSOnly())
    try:
        with opener.open(urllib.request.Request(LIST_URL, headers={'User-Agent': 'Pi-hole-Go/0.1'}), timeout=25) as r:
            return r.read(MAX_BYTES + 1)
    except Exception:
        raise UserError('规则下载失败，原规则继续生效') from None


def build_database(path, payload, settings):
    path = Path(path)
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    try:
        db.execute('PRAGMA cache_size=-1024')
        db.executescript((APP / 'vendor/gravity-schema.sql').read_text())
        db.execute('CREATE UNIQUE INDEX idx_gravity ON gravity(domain, adlist_id)')
        db.execute('CREATE INDEX idx_antigravity ON antigravity(domain, adlist_id)')
        db.execute('INSERT INTO adlist(id,address,comment) VALUES (1,?,?)', (LIST_URL, 'HaGeZi Light'))
        db.executemany('INSERT OR IGNORE INTO gravity(domain,adlist_id) VALUES (?,1)', ((d,) for d in parse_rules(payload)))
        for key, kind in [('allowlist', 2), ('denylist', 3)]:
            db.executemany('INSERT INTO domainlist(type,domain,comment) VALUES (?,?,?)',
                           ((kind, r'(^|\.)' + re.escape(d) + '$', 'Pi-hole Go') for d in settings[key]))
        count = db.execute('SELECT COUNT(*) FROM gravity').fetchone()[0]
        abp = db.execute("SELECT COUNT(*) FROM gravity WHERE domain LIKE '||%'").fetchone()[0]
        db.executemany('INSERT OR REPLACE INTO info(property,value) VALUES (?,?)',
                       [('abp_domains', str(abp)), ('gravity_count', str(count)),
                        ('antigravity_count', '0'), ('updated', str(int(time.time())))])
        db.execute('UPDATE adlist SET number=?,abp_entries=?,date_updated=?,status=1 WHERE id=1', (count, abp, int(time.time())))
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise UserError('规则数据库校验失败')
        return count
    finally:
        db.close()


def seed():
    meta = json.loads((APP / 'vendor/manifest.json').read_text())['blocklist']
    import hashlib
    raw = (APP / 'vendor' / meta['asset']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta['compressed_sha256']:
        raise UserError('内置规则校验失败')
    payload = gzip.decompress(raw)
    if hashlib.sha256(payload).hexdigest() != meta['sha256']:
        raise UserError('内置规则校验失败')
    return payload
