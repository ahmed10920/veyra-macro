"""Shared Support Code session protocol, Relay store, and local discovery for Veyra."""
import base64
import glob
import io
import json
import os
import re
import secrets
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request

CODE_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
SESSION_TTL_S = 1800  # 30 minutes max before auto-expiry
MAX_COMMANDS = 40

ALLOWED_CMDS = {
    'start_macro',
    'stop_macro',
    'heal_screen',
    'check_chat',
    'preview_biome',
    'update_cfg',
    'capture_screen',
    'roblox_click',
    'roblox_key',
    'list_dir',
    'read_file',
    'write_file',
    'make_dir',
    'delete_path',
}

# Config keys a connected dev is allowed to adjust remotely (never credentials/webhooks)
SAFE_DEV_CFG_KEYS = {
    'low_end', 'perf_mode', 'fx_on', 'anti_afk', 'fish_on', 'fish_vip', 'fish_sell_all', 'fish_full_check',
    'fish_sell', 'fish_sell_amount', 'merchant_on', 'boss_on', 'collect_on', 'auto_items',
    'auto_reconnect', 'rejoin_browser', 'eden_on', 'eden_path', 'clip_on', 'equip_on', 'skip_intro',
    'rune_on', 'selector_on', 'pop_on',
}


def generate_support_code():
    """Return an easy-to-read 8-character code formatted as XXXX-XXXX."""
    chars = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return chars[:4] + '-' + chars[4:]


def normalize_code(code):
    raw = re.sub(r'[^A-Za-z0-9]', '', str(code or '').upper())
    if len(raw) == 8:
        return raw[:4] + '-' + raw[4:]
    return str(code or '').strip().upper()


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.3)
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'


def discovery_dir():
    d = os.path.join(tempfile.gettempdir(), 'mmacro_support_sessions')
    os.makedirs(d, exist_ok=True)
    return d


def register_local_session(code, info):
    norm = normalize_code(code)
    p = os.path.join(discovery_dir(), '%s.json' % norm)
    tmp = '%s.%d.tmp' % (p, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(info, f)
    os.replace(tmp, p)


def unregister_local_session(code):
    norm = normalize_code(code)
    p = os.path.join(discovery_dir(), '%s.json' % norm)
    try:
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass


def lookup_local_session(code):
    norm = normalize_code(code)
    p = os.path.join(discovery_dir(), '%s.json' % norm)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        if time.time() - float(data.get('updated_at', 0)) > SESSION_TTL_S:
            unregister_local_session(norm)
            return None
        return data
    except Exception:
        return None


def capture_screen_b64(max_w=960, quality=45, bbox=None, allow_full=False):
    """Capture a screen region and return base64 JPEG (empty on failure).
    If bbox=(l,t,r,b) is provided, captures only that region (used for Roblox-only remote view).
    Full desktop capture is disabled unless allow_full=True, to avoid leaking unrelated screen content.
    """
    try:
        from PIL import ImageGrab
        if bbox:
            l, t, r, b = [int(v) for v in bbox]
            if r <= l or b <= t:
                return ''
            img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
        elif allow_full:
            img = ImageGrab.grab(all_screens=False)
        else:
            return ''
        if getattr(img, 'mode', '') != 'RGB':
            img = img.convert('RGB')
        w, h = img.size
        if max_w and w > max_w:
            nh = max(1, int(round(h * (max_w / float(w)))))
            try:
                from PIL.Image import Resampling
                resample = Resampling.BILINEAR
            except Exception:
                resample = 2
            img = img.resize((max_w, nh), resample)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=max(20, min(95, int(quality))), optimize=False)
        return base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception:
        return ''


def http_json(url, method='GET', payload=None, timeout=6):
    data = None
    headers = {'User-Agent': 'MMacroSupport/1.0'}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
        return json.loads(raw) if raw else {}


class RelayStore:
    """In-memory room store for live Support Code relay (used by local host & cloud relay)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rooms = {}

    def _prune_locked(self):
        now = time.time()
        stale = [c for c, r in self._rooms.items() if now - r.get('updated_at', now) > SESSION_TTL_S]
        for c in stale:
            del self._rooms[c]

    def open_or_push(self, code, host_secret, snapshot, results=None):
        norm = normalize_code(code)
        with self._lock:
            self._prune_locked()
            room = self._rooms.get(norm)
            if room is None:
                room = {
                    'code': norm,
                    'host_secret': host_secret,
                    'created_at': time.time(),
                    'updated_at': time.time(),
                    'connected_dev': None,
                    'dev_connected_at': 0.0,
                    'snapshot': snapshot or {},
                    'commands': [],
                    'results': {},
                    'closed': False,
                }
                self._rooms[norm] = room
            elif room['host_secret'] != host_secret:
                return {'ok': False, 'error': 'Invalid host secret'}
            else:
                room['updated_at'] = time.time()
                room['snapshot'] = snapshot or room['snapshot']
            if isinstance(results, dict) and results:
                rmap = room.setdefault('results', {})
                for cid, rval in results.items():
                    rmap[str(cid)] = {'result': rval, 'ts': time.time()}
                if len(rmap) > 80:
                    for k in sorted(rmap.keys(), key=lambda x: rmap[x].get('ts', 0))[:len(rmap) - 50]:
                        rmap.pop(k, None)
            cmds = list(room['commands'])
            room['commands'].clear()
            return {
                'ok': True,
                'connected_dev': room.get('connected_dev'),
                'commands': cmds,
                'closed': bool(room.get('closed')),
            }

    def connect_dev(self, code, dev_username):
        norm = normalize_code(code)
        with self._lock:
            self._prune_locked()
            room = self._rooms.get(norm)
            if not room or room.get('closed'):
                return {'ok': False, 'error': 'Support code not found or expired'}
            room['connected_dev'] = str(dev_username or 'dev')
            room['dev_connected_at'] = time.time()
            room['updated_at'] = time.time()
            return {'ok': True, 'code': norm, 'snapshot': room.get('snapshot', {})}

    def pull_dev(self, code):
        norm = normalize_code(code)
        with self._lock:
            self._prune_locked()
            room = self._rooms.get(norm)
            if not room or room.get('closed'):
                return {'ok': False, 'error': 'Session closed or expired'}
            return {
                'ok': True,
                'code': norm,
                'connected_dev': room.get('connected_dev'),
                'snapshot': room.get('snapshot', {}),
                'age_s': round(max(0.0, time.time() - room.get('updated_at', time.time())), 1),
            }

    def enqueue_cmd(self, code, dev_username, cmd, args=None):
        norm = normalize_code(code)
        if cmd not in ALLOWED_CMDS:
            return {'ok': False, 'error': 'Command not allowed: %s' % cmd}
        with self._lock:
            self._prune_locked()
            room = self._rooms.get(norm)
            if not room or room.get('closed'):
                return {'ok': False, 'error': 'Session closed or expired'}
            if len(room['commands']) >= MAX_COMMANDS:
                room['commands'].pop(0)
            cmd_id = secrets.token_hex(4)
            room['commands'].append({
                'id': cmd_id,
                'dev': str(dev_username or 'dev'),
                'cmd': cmd,
                'args': args or {},
                'ts': time.time(),
            })
            room['updated_at'] = time.time()
            return {'ok': True, 'cmd_id': cmd_id}

    def store_result(self, code, cmd_id, result):
        norm = normalize_code(code)
        with self._lock:
            room = self._rooms.get(norm)
            if not room or room.get('closed'):
                return {'ok': False, 'error': 'Session closed or expired'}
            rmap = room.setdefault('results', {})
            rmap[str(cmd_id)] = {'result': result, 'ts': time.time()}
            if len(rmap) > 80:
                for k in sorted(rmap.keys(), key=lambda x: rmap[x].get('ts', 0))[:len(rmap) - 50]:
                    rmap.pop(k, None)
            return {'ok': True}

    def get_result(self, code, cmd_id):
        norm = normalize_code(code)
        with self._lock:
            self._prune_locked()
            room = self._rooms.get(norm)
            if not room or room.get('closed'):
                return {'ok': False, 'error': 'Session closed or expired'}
            rmap = room.get('results') or {}
            item = rmap.pop(str(cmd_id), None)
            if item is None:
                return {'ok': False, 'pending': True}
            return {'ok': True, 'result': item.get('result')}

    def close_room(self, code):
        norm = normalize_code(code)
        with self._lock:
            room = self._rooms.get(norm)
            if room:
                room['closed'] = True
                room['commands'].clear()
                return {'ok': True}
        return {'ok': True}
