"""
keyauth.py — KeyAuth Client Library
====================================
Ultra-simple drop-in authentication for any Python application.

Usage
-----
    from keyauth import KeyAuth

    auth = KeyAuth(server="http://your-vps:5000")
    result = auth.login(key="YOUR-KEY-XXXX")

    if result.success:
        print("Access granted!")
    else:
        print(f"Access denied: {result.message}")
        # result.error_code is one of:
        #   INVALID_KEY    — key doesn't exist
        #   KEY_BANNED     — key is banned
        #   KEY_EXPIRED    — key's time has expired
        #   HWID_MISMATCH  — max devices reached, this device not registered

Quick one-liner
---------------
    from keyauth import quick_auth
    quick_auth("http://your-vps:5000", "YOUR-KEY")   # raises SystemExit on failure

Environment variable support
-----------------------------
    Set KEYAUTH_SERVER and/or KEYAUTH_KEY before running.
    from keyauth import KeyAuth
    auth = KeyAuth()   # reads KEYAUTH_SERVER from env
    auth.login()       # reads KEYAUTH_KEY from env
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional


# ─── HWID generation ──────────────────────────────────────────────────────────

def _get_hwid() -> str:
    """
    Generate a stable hardware identifier from system info.
    This is NOT trivially spoofable since it uses multiple sources.
    """
    parts: list[str] = []

    system = platform.system()

    if system == 'Windows':
        # Windows: use MachineGuid from registry + disk serial
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r'SOFTWARE\Microsoft\Cryptography')
            guid, _ = winreg.QueryValueEx(key, 'MachineGuid')
            parts.append(guid)
        except Exception:
            pass
        try:
            out = subprocess.check_output(
                ['wmic', 'diskdrive', 'get', 'SerialNumber'],
                stderr=subprocess.DEVNULL, timeout=5
            ).decode(errors='ignore')
            serial = ''.join(out.split()).replace('SerialNumber', '')
            if serial:
                parts.append(serial)
        except Exception:
            pass

    elif system == 'Linux':
        # Linux: machine-id
        for path in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
            try:
                with open(path) as f:
                    mid = f.read().strip()
                if mid:
                    parts.append(mid)
                    break
            except Exception:
                pass

    elif system == 'Darwin':
        # macOS: IOPlatformUUID
        try:
            out = subprocess.check_output(
                ['ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
                stderr=subprocess.DEVNULL, timeout=5
            ).decode(errors='ignore')
            for line in out.splitlines():
                if 'IOPlatformUUID' in line:
                    parts.append(line.split('"')[-2])
                    break
        except Exception:
            pass

    # Fallback: hostname + processor
    parts.append(socket.gethostname())
    parts.append(platform.processor() or 'unknown')
    parts.append(platform.machine())

    raw = '|'.join(filter(None, parts))
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class AuthResult:
    success: bool
    message: str
    error_code: Optional[str] = None  # INVALID_KEY / KEY_BANNED / KEY_EXPIRED / HWID_MISMATCH

    def __bool__(self) -> bool:
        return self.success


# ─── Main class ───────────────────────────────────────────────────────────────

class KeyAuth:
    """
    KeyAuth client.

    Parameters
    ----------
    server : str
        Base URL of the Flask server, e.g. ``"http://123.45.67.89:5000"``
        Falls back to environment variable ``KEYAUTH_SERVER``.
    timeout : int
        HTTP request timeout in seconds (default 10).
    """

    _ERROR_MESSAGES = {
        'INVALID_KEY':   'Invalid license key.',
        'KEY_BANNED':    'This license key has been banned.',
        'KEY_EXPIRED':   'This license key has expired.',
        'HWID_MISMATCH': 'Maximum devices reached. Your hardware ID is not registered for this key.\n'
                         'Contact the seller to reset your HWID.',
        'NETWORK_ERROR': 'Could not reach the authentication server. Check your internet connection.',
        'SERVER_ERROR':  'The authentication server returned an unexpected error.',
    }

    def __init__(self, server: str = '', timeout: int = 10):
        self.server  = (server or os.environ.get('KEYAUTH_SERVER', '')).rstrip('/')
        self.timeout = timeout
        self.hwid    = _get_hwid()
        self._authenticated = False

    # ── public API ────────────────────────────────────────────────────────────

    def login(self, key: str = '') -> AuthResult:
        """
        Authenticate with the given key.

        Parameters
        ----------
        key : str
            The license key to verify.
            Falls back to environment variable ``KEYAUTH_KEY``.

        Returns
        -------
        AuthResult
            .success   → True/False
            .message   → Human-readable message
            .error_code→ Machine-readable error code (None on success)
        """
        key = (key or os.environ.get('KEYAUTH_KEY', '')).strip()

        if not self.server:
            return AuthResult(False, 'No server URL configured.', 'CONFIG_ERROR')
        if not key:
            return AuthResult(False, 'No license key provided.', 'CONFIG_ERROR')

        payload = json.dumps({'key': key, 'hwid': self.hwid}).encode()
        req = urllib.request.Request(
            url=f'{self.server}/auth/verify',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except Exception:
                return AuthResult(False, self._ERROR_MESSAGES['SERVER_ERROR'], 'SERVER_ERROR')
        except Exception:
            return AuthResult(False, self._ERROR_MESSAGES['NETWORK_ERROR'], 'NETWORK_ERROR')

        if body.get('success'):
            self._authenticated = True
            return AuthResult(True, 'Authentication successful.')

        err_code = body.get('error', 'SERVER_ERROR')
        msg      = self._ERROR_MESSAGES.get(err_code, body.get('error', 'Unknown error'))
        return AuthResult(False, msg, err_code)

    @property
    def authenticated(self) -> bool:
        """True if ``login()`` has succeeded in this session."""
        return self._authenticated


# ─── Convenience one-liner ───────────────────────────────────────────────────

def quick_auth(
    server: str,
    key: str,
    exit_on_failure: bool = True,
    show_hwid_on_failure: bool = True,
) -> AuthResult:
    """
    One-liner auth helper. Prints a friendly error and exits if auth fails.

    Example
    -------
        from keyauth import quick_auth
        quick_auth("http://your-vps:5000", input("Enter your license key: "))
        # ... rest of your program runs only if authenticated
    """
    auth   = KeyAuth(server=server)
    result = auth.login(key=key)

    if not result.success:
        print(f'\n[KeyAuth] Access Denied: {result.message}')
        if show_hwid_on_failure and result.error_code == 'HWID_MISMATCH':
            print(f'[KeyAuth] Your HWID: {auth.hwid[:16]}...')
        if exit_on_failure:
            sys.exit(1)

    return result
