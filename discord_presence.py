"""Discord Rich Presence over the local Discord IPC pipe.

Shows the current biome on the user's Discord profile while Veyra is open.
The application id is built into Veyra. Users do not create their own.
"""
import json
import os
import struct
import threading
import time
import uuid
from ctypes import wintypes
import ctypes

_OP_HANDSHAKE = 0
_OP_FRAME = 1
_OP_CLOSE = 2
_OP_PING = 3
_OP_PONG = 4

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_ERROR_FILE_NOT_FOUND = 2
_ERROR_BROKEN_PIPE = 109
_ERROR_PIPE_BUSY = 231

_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_kernel.CreateFileW.restype = wintypes.HANDLE
_kernel.WriteFile.argtypes = [
    wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
_kernel.WriteFile.restype = wintypes.BOOL
_kernel.ReadFile.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
_kernel.ReadFile.restype = wintypes.BOOL
_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel.CloseHandle.restype = wintypes.BOOL
_kernel.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_kernel.WaitNamedPipeW.restype = wintypes.BOOL
_kernel.PeekNamedPipe.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
]
_kernel.PeekNamedPipe.restype = wintypes.BOOL
_kernel.GetLastError.argtypes = []
_kernel.GetLastError.restype = wintypes.DWORD


def _valid_id(client_id):
    return client_id.isdigit() and 17 <= len(client_id) <= 20


def _bad_handle(handle):
    if handle is None:
        return True
    value = getattr(handle, 'value', handle)
    if value is None or value == 0:
        return True
    return value in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


class DiscordPresence:
    def __init__(self, log=None):
        self._log = log or (lambda _msg: None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._enabled = False
        self._client_id = ''
        self._want = None
        self._sent = None
        self._images_ok = True
        self._image_retry_at = 0.0
        self._status = 'off'
        self._detail = 'Discord activity is off'
        self._logged = None
        self._thread = threading.Thread(target=self._run, name='discord-presence', daemon=True)
        self._thread.start()

    def configure(self, enabled, client_id):
        client_id = ''.join(str(client_id or '').split())
        with self._lock:
            changed = bool(enabled) != self._enabled or client_id != self._client_id
            turned_on = bool(enabled) and not self._enabled
            self._enabled = bool(enabled)
            self._client_id = client_id
            if changed or turned_on:
                self._sent = None
                self._images_ok = True
        if changed or turned_on:
            self._wake.set()

    def set_activity(self, activity):
        """activity is a SET_ACTIVITY body, or None to clear it."""
        with self._lock:
            if activity == self._want:
                return
            self._want = activity
            self._sent = None
        self._wake.set()

    def status(self):
        with self._lock:
            return {'status': self._status, 'detail': self._detail}

    def close(self):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2.0)

    def _set_status(self, status, detail):
        with self._lock:
            changed = status != self._status or detail != self._detail
            self._status = status
            self._detail = detail
        if not changed:
            return
        if status == 'connected' and detail.startswith('Discord is showing'):
            if self._logged != detail:
                self._logged = detail
                self._log(detail)
            return
        if status in ('no_discord', 'bad_id', 'need_id', 'off') and self._logged != status:
            self._logged = status
            self._log(detail)

    def _snapshot(self):
        with self._lock:
            return self._enabled, self._client_id, self._want, self._images_ok, self._sent

    def _run(self):
        handle = None
        live_id = ''
        buf = b''
        while not self._stop.is_set():
            enabled, client_id, want, images_ok, sent = self._snapshot()
            if not images_ok and time.monotonic() >= self._image_retry_at:
                with self._lock:
                    self._images_ok = True
                    self._sent = None
                images_ok = True                          # try the images again after a cooldown
            if not enabled or not _valid_id(client_id):
                handle = _drop(handle, clear=bool(handle))
                buf = b''
                live_id = ''
                if not enabled:
                    self._set_status('off', 'Discord activity is off')
                    self._wake.wait(1.0)
                elif not client_id:
                    self._set_status('need_id', 'Discord activity is unavailable')
                    self._wake.wait(1.0)
                else:
                    self._set_status('bad_id', 'Discord activity could not connect')
                    self._wake.wait(2.0)
                self._wake.clear()
                continue
            if handle is None or live_id != client_id:
                handle = _drop(handle, clear=False)
                buf = b''
                self._set_status('connecting', 'Connecting to Discord...')
                opened, reason, buf = self._handshake(client_id)
                if opened is None:
                    if reason == 'bad_id':
                        self._set_status('bad_id', 'Discord activity could not connect')
                        self._wake.wait(20.0)
                    else:
                        self._set_status('no_discord', 'Discord desktop is not open')
                        self._wake.wait(4.0)
                    self._wake.clear()
                    continue
                handle = opened
                live_id = client_id
                with self._lock:
                    self._sent = None
                sent = None
                self._set_status('connected', 'Connected to Discord')
            try:
                changed, buf = self._pump(handle, buf, want, images_ok, sent)
                if changed == 'dead':
                    handle = _drop(handle, clear=False)
                    buf = b''
                    self._wake.wait(2.0)
                    self._wake.clear()
                    continue
                if changed == 'strip':
                    with self._lock:
                        self._images_ok = False
                        self._sent = None
                        self._image_retry_at = time.monotonic() + 60.0
            except OSError:
                handle = _drop(handle, clear=False)
                buf = b''
                self._wake.wait(2.0)
                self._wake.clear()
                continue
            self._wake.wait(0.25)
            self._wake.clear()
        _drop(handle, clear=True)

    def _handshake(self, client_id):
        handle = _open_pipe()
        if handle is None:
            return None, 'no_discord', b''
        try:
            _send(handle, _OP_HANDSHAKE, {'v': 1, 'client_id': client_id})
            buf = b''
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not self._stop.is_set():
                frames, buf = _pull(handle, buf)
                for op, raw in frames:
                    if op == _OP_PING:
                        _send_raw(handle, _OP_PONG, raw)
                        continue
                    if op == _OP_CLOSE:
                        _drop(handle, clear=False)
                        return None, 'bad_id', b''
                    if op != _OP_FRAME or not raw:
                        continue
                    msg = _json(raw)
                    if msg.get('evt') == 'READY':
                        return handle, 'ok', buf
                    data = msg.get('data')
                    if msg.get('evt') == 'ERROR' or (isinstance(data, dict) and data.get('code') == 4000):
                        _drop(handle, clear=False)
                        return None, 'bad_id', b''
                time.sleep(0.02)
        except OSError:
            _drop(handle, clear=False)
            return None, 'no_discord', b''
        _drop(handle, clear=False)
        return None, 'bad_id', b''

    def _pump(self, handle, buf, want, images_ok, sent):
        wire = _wire_activity(want, images_ok)
        blob = json.dumps(wire, sort_keys=True, separators=(',', ':'))
        if blob != sent:
            _send(handle, _OP_FRAME, {
                'cmd': 'SET_ACTIVITY',
                'args': {'pid': os.getpid(), 'activity': wire},
                'nonce': uuid.uuid4().hex,
            })
            with self._lock:
                self._sent = blob
            if want:
                name = str(want.get('details') or 'biome')
                self._set_status('connected', 'Discord is showing %s' % name)
        frames, buf = _pull(handle, buf)
        strip = False
        for op, raw in frames:
            if op == _OP_PING:
                _send_raw(handle, _OP_PONG, raw)
                continue
            if op == _OP_CLOSE:
                return 'dead', buf
            if op != _OP_FRAME or not raw:
                continue
            msg = _json(raw)
            if msg.get('evt') != 'ERROR':
                continue
            text = json.dumps(msg).lower()
            if images_ok and ('asset' in text or 'image' in text or 'large_image' in text):
                strip = True
                continue
            if msg.get('cmd') == 'SET_ACTIVITY':
                continue
            return 'dead', buf
        return ('strip' if strip else 'ok'), buf


def _json(raw):
    try:
        msg = json.loads(raw.decode('utf-8', errors='replace'))
    except ValueError:
        return {}
    return msg if isinstance(msg, dict) else {}


def _wire_activity(activity, images_ok):
    if not activity:
        return None
    body = {
        'details': str(activity.get('details') or '')[:128],
        'state': str(activity.get('state') or '')[:128],
        'instance': False,
    }
    timestamps = activity.get('timestamps')
    if isinstance(timestamps, dict) and timestamps.get('start'):
        body['timestamps'] = {'start': int(timestamps['start'])}
    # Discord renders party.size as "1 of 20" on the profile.
    party = activity.get('party')
    if isinstance(party, dict):
        size = party.get('size')
        if isinstance(size, (list, tuple)) and len(size) == 2:
            body['party'] = {'size': [int(size[0]), int(size[1])]}
            if party.get('id'):
                body['party']['id'] = str(party['id'])[:128]
    # Discord shows these as clickable buttons on the activity card (max 2).
    buttons = activity.get('buttons')
    if isinstance(buttons, (list, tuple)):
        pair = []
        for b in buttons[:2]:
            if isinstance(b, dict) and b.get('label') and b.get('url'):
                pair.append({'label': str(b['label'])[:32], 'url': str(b['url'])[:512]})
        if pair:
            body['buttons'] = pair
    assets = activity.get('assets') if images_ok else None
    if isinstance(assets, dict) and assets.get('large_image'):
        block = {
            'large_image': str(assets['large_image'])[:256],
            'large_text': str(assets.get('large_text') or '')[:128],
        }
        # Discord ignores small_text unless a small_image is sent with it.
        if assets.get('small_image'):
            block['small_image'] = str(assets['small_image'])[:256]
            if assets.get('small_text'):
                block['small_text'] = str(assets['small_text'])[:128]
        body['assets'] = block
    return body


def _drop(handle, clear):
    if handle is None or _bad_handle(handle):
        return None
    if clear:
        try:
            _send(handle, _OP_FRAME, {
                'cmd': 'SET_ACTIVITY',
                'args': {'pid': os.getpid(), 'activity': None},
                'nonce': uuid.uuid4().hex,
            })
        except OSError:
            pass
    _kernel.CloseHandle(handle)
    return None


def _open_pipe():
    for index in range(10):
        name = r'\\.\pipe\discord-ipc-%d' % index
        handle = _kernel.CreateFileW(name, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None)
        if not _bad_handle(handle):
            return handle
        err = _kernel.GetLastError()
        if err == _ERROR_PIPE_BUSY and _kernel.WaitNamedPipeW(name, 800):
            handle = _kernel.CreateFileW(name, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None)
            if not _bad_handle(handle):
                return handle
        if err not in (_ERROR_FILE_NOT_FOUND, _ERROR_PIPE_BUSY):
            continue
    return None


def _send(handle, op, payload):
    _send_raw(handle, op, json.dumps(payload, separators=(',', ':')).encode('utf-8'))


def _send_raw(handle, op, raw):
    _write(handle, struct.pack('<II', op, len(raw)) + raw)


def _write(handle, packet):
    buf = ctypes.create_string_buffer(packet)
    written = wintypes.DWORD(0)
    ok = _kernel.WriteFile(handle, buf, len(packet), ctypes.byref(written), None)
    if not ok or written.value != len(packet):
        raise OSError(_kernel.GetLastError() or _ERROR_BROKEN_PIPE, 'WriteFile')


def _pull(handle, buf):
    """Read bytes already waiting on the pipe. Never blocks on an empty pipe."""
    avail = wintypes.DWORD(0)
    ok = _kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None)
    if not ok:
        raise OSError(_kernel.GetLastError() or _ERROR_BROKEN_PIPE, 'PeekNamedPipe')
    if avail.value:
        buf += _read(handle, avail.value)
    frames = []
    while len(buf) >= 8:
        op, length = struct.unpack_from('<II', buf)
        if length > 65536:
            raise OSError(_ERROR_BROKEN_PIPE, 'discord frame too large')
        if len(buf) < 8 + length:
            break
        frames.append((op, buf[8:8 + length]))
        buf = buf[8 + length:]
    return frames, buf


def _read(handle, size):
    got_all = b''
    while len(got_all) < size:
        chunk = ctypes.create_string_buffer(size - len(got_all))
        got = wintypes.DWORD(0)
        ok = _kernel.ReadFile(handle, chunk, size - len(got_all), ctypes.byref(got), None)
        if not ok or got.value == 0:
            err = _kernel.GetLastError()
            if err in (_ERROR_BROKEN_PIPE, 232, 233):
                raise OSError(err or _ERROR_BROKEN_PIPE, 'ReadFile')
            raise OSError(err or _ERROR_BROKEN_PIPE, 'ReadFile')
        got_all += chunk.raw[:got.value]
    return got_all
