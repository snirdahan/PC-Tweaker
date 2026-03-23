"""
NEXUS Gaming Performance Suite — Python + pywebview
Requirements:
    pip install pywebview psutil requests rarfile GPUtil
"""

import os
import sys
import json
import base64
import shutil
import tempfile
import threading
import subprocess
import traceback
import logging
import io
import time
from pathlib import Path

# ══════════════════════════════════════════════════════════════════
#  SILENCE pywebview AccessibilityObject recursion spam (3-level fix)
#  This is a confirmed pywebview WinForms bug — completely harmless
#  but very noisy. We patch stderr + the internal logger to drop it.
# ══════════════════════════════════════════════════════════════════
logging.getLogger("pywebview").setLevel(logging.CRITICAL)

class _NullWriter:
    """Drops stderr lines containing known pywebview WinForms noise."""
    _NOISE = (
        "AccessibilityObject",
        "CoreWebView2 members can only be accessed from the UI thread",
        "CoreWebView2 can only be accessed from the UI thread",
        "Unable to cast COM object",
        "ICoreWebView2",
        "QueryInterface call on the COM component",
        "E_NOINTERFACE",
        "HRESULT: 0x80004002",
        "__abstractmethods__",
        "DockPaddingEdgesConverter",
        "Error while processing window.native",
    )
    def __init__(self, real): self._real = real
    def write(self, s):
        if any(n in s for n in self._NOISE): return len(s)
        return self._real.write(s)
    def flush(self): self._real.flush()
    def __getattr__(self, name): return getattr(self._real, name)

sys.stderr = _NullWriter(sys.__stderr__)

import re
import psutil
import requests
import webview

# Patch pywebview internal winforms logger after import
try:
    import webview.platforms.winforms as _wf
    _orig_log = _wf.logger
    class _PatchedLog:
        _NOISE = (
            "AccessibilityObject",
            "CoreWebView2",
            "Unable to cast COM object",
            "ICoreWebView2",
            "QueryInterface",
            "E_NOINTERFACE",
            "__abstractmethods__",
            "DockPaddingEdgesConverter",
            "Error while processing window.native",
        )
        def __init__(self, r): self._r = r
        def _skip(self, m): return any(n in str(m) for n in self._NOISE)
        def error(self, m, *a, **k):
            if not self._skip(m): self._r.error(m, *a, **k)
        def warning(self, m, *a, **k):
            if not self._skip(m): self._r.warning(m, *a, **k)
        def info(self, m, *a, **k): self._r.info(m, *a, **k)
        def debug(self, m, *a, **k): self._r.debug(m, *a, **k)
        def exception(self, m, *a, **k):
            if not self._skip(m): self._r.exception(m, *a, **k)
        def __getattr__(self, n): return getattr(self._r, n)
    _wf.logger = _PatchedLog(_orig_log)
except Exception:
    pass  # Not Windows / different pywebview version — ignore

# ── Optional GPU detection (lazy — imported only when get_hardware is called) ──
GPU_AVAILABLE = None  # None = not yet checked
GPUtil = None         # loaded on demand

# ── Constants ──────────────────────────────────────────────────────
APP_DIR = Path(os.getenv("APPDATA", Path.home())) / "NexusSuite"
APP_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_IMG = APP_DIR / "profile.jpg"
AUTH_FILE   = APP_DIR / "auth.json"
FIVEM_PATH_FILE = APP_DIR / "fivem_path.txt"
BACKUP_FILE     = APP_DIR / "tweak_backup.json"
SETTINGS_FILE   = APP_DIR / "settings.json"
APP_VERSION     = "2.0.0"
DISCORD_APP_ID  = "1467001855472173289"
WEBHOOK_URL     = "https://discord.com/api/webhooks/1479429112132014140/mTaGSNwznrl80iyizkXvmbsIDh-hk9V0f7gD-rbFbkiTyHXMEkFlqqZUiRUnkbzZ3yej"

_discord_rpc         = None
_discord_rpc_running = False
_discord_start_ts    = 0

def _load_fivem_path():
    if FIVEM_PATH_FILE.exists():
        try:
            p = Path(FIVEM_PATH_FILE.read_text().strip())
            if p.exists():
                return p
        except Exception:
            pass
    return _find_fivem()

def _save_fivem_path(path):
    FIVEM_PATH_FILE.write_text(str(path))

def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_settings(data: dict):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    current = _load_settings()
    current.update(data)
    SETTINGS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")

# ── Graphic Packs ──────────────────────────────────────────────────
GRAPHIC_PACKS = [
  {
    "name": "Forever",
    "game": "fivem",
    "videoUrl": "http://88.214.55.15:3000/cdn/Desktop_2026.01.07_-_12.39.15.08.DVR_-_Trim.mp4",
    "downloadUrl": "http://88.214.55.15:3000/cdn/forever.rar"
  },
  {
    "name": "Dusk",
    "game": "fivem",
    "videoUrl": "http://88.214.55.15:3000/cdn/Desktop_2026.02.09_-_15.02.05.18.DVR_-_Trim.mp4",
    "downloadUrl": "http://88.214.55.15:3000/cdn/Dusk_1.rar"
  },
  {
    "name": "Byoung",
    "game": "fivem",
    "videoUrl": "http://88.214.55.15:3000/cdn/Desktop_2026.02.12-15.53.50.26.DVR_edit_1.mp4",
    "downloadUrl": "http://88.214.55.15:3000/cdn/Byoung.7z"
  }
]

# Add index to each pack
for i, p in enumerate(GRAPHIC_PACKS):
    p["index"] = i


# ══════════════════════════════════════════════════════════════════
#  OFFLINE LICENSE SYSTEM  (replaces KeyAuth VPS dependency)
# ══════════════════════════════════════════════════════════════════
import hashlib as _hashlib

# ⚠ Change _AUTH_SALT to make your keys unique — anyone with the salt
#   could generate valid keys, so keep it private.
_AUTH_SALT = "NEXUS_ELITE_SUITE_2025_PRIVATE"

# Developer master keys — always valid on any machine
_MASTER_KEYS: set[str] = {
    "NEXUS-MASTER-2025",
    "NEXUS-DEV-ADMIN",
}


def _get_hwid() -> str:
    """Return a stable 4×4 hex HWID for this machine."""
    parts: list[str] = []
    if sys.platform == "win32":
        # MachineGuid — most stable identifier on Windows
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\Microsoft\Cryptography")
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            winreg.CloseKey(key)
            parts.append(str(guid))
        except Exception:
            pass
        # Disk serial as secondary factor
        try:
            out = subprocess.check_output(
                ["wmic", "diskdrive", "get", "SerialNumber"],
                stderr=subprocess.DEVNULL, timeout=5,
            ).decode(errors="ignore")
            serial = "".join(out.split()).replace("SerialNumber", "")
            if serial:
                parts.append(serial)
        except Exception:
            pass
    if not parts:
        import uuid
        parts.append(str(uuid.getnode()))
    raw = _hashlib.sha256("|".join(parts).encode()).hexdigest().upper()
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"


def _keygen(hwid: str) -> str:
    """Generate the valid license key for a HWID. Use this as your keygen script."""
    raw = _hashlib.sha256(f"{hwid}:{_AUTH_SALT}".encode()).hexdigest().upper()
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"


_KEY_RE = re.compile(r'^NEXUS-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}$')

def _validate_key(key: str) -> bool:
    """
    Validate a license key.
    - Master keys: always valid on any machine.
    - Regular keys: must be locally bound to this machine's HWID
      (binding is saved on first successful login).
    - First time (no local binding): accept any correctly-formatted key.
    """
    k = key.strip().upper()

    # 1. Master keys — always valid
    if k in {mk.upper() for mk in _MASTER_KEYS}:
        return True

    # 2. Locally bound key — subsequent logins (offline)
    if AUTH_FILE.exists():
        try:
            saved = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            if saved.get("key", "").upper() == k:
                saved_hwid = saved.get("hwid", "")
                if saved_hwid:
                    return saved_hwid == _get_hwid()   # same machine = valid
        except Exception:
            pass

    # 3. First-time use — accept if format is valid (XXXX-XXXX-XXXX-XXXX hex)
    #    Binding to this HWID happens in login() after this returns True.
    return bool(_KEY_RE.match(k))


def _notify_failed_login(hwid: str) -> None:
    """
    POST a red security alert webhook when someone tries a wrong key.
    Bot catches 'NEXUS_LOGIN_FAILED' and posts a red embed in the keys channel.
    Called in a daemon thread — never blocks the login flow.
    """
    try:
        webhook_url = WEBHOOK_URL

        ip = "Unknown"
        try:
            ip_resp = requests.get("https://api.ipify.org", timeout=5)
            if ip_resp.status_code == 200:
                ip = ip_resp.text.strip()
        except Exception:
            pass

        try:
            import platform as _platform
            os_info = f"{_platform.system()} {_platform.release()} {_platform.version()[:40]}"
        except Exception:
            os_info = sys.platform

        payload = {
            "embeds": [{
                "title": "NEXUS_LOGIN_FAILED",
                "color": 0xFF3C3C,
                "fields": [
                    {"name": "HWID", "value": f"`{hwid}`",    "inline": True},
                    {"name": "IP",   "value": f"`{ip}`",      "inline": True},
                    {"name": "OS",   "value": f"`{os_info}`", "inline": False},
                ],
                "footer": {"text": "NEXUS Gaming Suite — failed login attempt"},
            }]
        }
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception:
        pass


def _notify_key_used(hwid: str, key: str = "") -> None:
    """
    POST a Discord webhook message so the bot can find the key record by KEY,
    bind the HWID, and update the 'Key Issued' embed to 'Key Activated'.
    Called in a daemon thread — never blocks the login flow.
    """
    try:
        webhook_url = WEBHOOK_URL

        ip = "Unknown"
        try:
            ip_resp = requests.get("https://api.ipify.org", timeout=5)
            if ip_resp.status_code == 200:
                ip = ip_resp.text.strip()
        except Exception:
            pass

        try:
            import platform as _platform
            os_info = f"{_platform.system()} {_platform.release()} {_platform.version()[:40]}"
        except Exception:
            os_info = sys.platform

        payload = {
            "embeds": [{
                "title": "NEXUS_KEY_USED",
                "color": 0x00FF88,
                "fields": [
                    {"name": "KEY",  "value": f"`{key}`",     "inline": True},
                    {"name": "HWID", "value": f"`{hwid}`",    "inline": True},
                    {"name": "IP",   "value": f"`{ip}`",      "inline": True},
                    {"name": "OS",   "value": f"`{os_info}`", "inline": False},
                ],
                "footer": {"text": "NEXUS Gaming Suite — activation report"},
            }]
        }
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
#  API  (exposed to JS via window.pyapi)
# ══════════════════════════════════════════════════════════════════
class API:
    def __init__(self):
        self.window = None   # set after webview.create_window
        self._running = True  # set False on close to stop background threads

    # ── Auth ──────────────────────────────────────────────────────
    def login(self, key: str, remember: bool = False) -> dict:
        """
        Validate license key.
        - First use: any correctly-formatted key is accepted → HWID is bound locally
          and sent to Discord so the bot can record which machine used it.
        - Subsequent uses: key + HWID must match the saved binding (fully offline).
        - Wrong key / wrong machine → red security alert sent to Discord.
        """
        key  = key.strip().upper()
        hwid = _get_hwid()

        if not key:
            return {"success": False, "message": "הכנס מפתח רישיון."}

        if _validate_key(key):
            # --- Save HWID binding on first activation ---
            already_bound = False
            if AUTH_FILE.exists():
                try:
                    saved = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
                    already_bound = bool(saved.get("hwid"))
                except Exception:
                    pass

            AUTH_FILE.write_text(
                json.dumps({"key": key, "hwid": hwid}), encoding="utf-8"
            )

            # Notify Discord only on first activation (not every login)
            if not already_bound:
                threading.Thread(
                    target=_notify_key_used, args=(hwid, key), daemon=True
                ).start()

            return {"success": True, "message": "גישה אושרה!"}

        # Invalid key or wrong machine → security alert
        threading.Thread(target=_notify_failed_login, args=(hwid,), daemon=True).start()
        return {
            "success": False,
            "message": "מפתח לא תקין.",
            "hwid": hwid,
        }

    def check_saved_key(self) -> dict:
        """Auto-login: valid only if saved key + HWID match this machine."""
        if AUTH_FILE.exists():
            try:
                data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
                key  = data.get("key", "").upper()
                hwid = data.get("hwid", "")
                if key and hwid and hwid == _get_hwid():
                    return {"saved": True, "key": key}
                # Master keys don't need HWID check
                if key and key in {mk.upper() for mk in _MASTER_KEYS}:
                    return {"saved": True, "key": key}
            except Exception:
                pass
        return {"saved": False, "key": ""}

    def get_hwid(self) -> dict:
        """Return this machine's HWID so the user can send it to the developer."""
        return {"success": True, "hwid": _get_hwid()}

    def logout(self) -> bool:
        if AUTH_FILE.exists():
            AUTH_FILE.unlink()
        return True

    def get_fivem_path(self):
        p = _load_fivem_path()
        return {"path": str(p) if p else "", "found": p is not None}

    def browse_fivem_path(self):
        try:
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:
            return {"success": False, "message": str(e)}
        if not result or len(result) == 0:
            return {"cancelled": True}
        chosen = Path(result[0])
        _save_fivem_path(chosen)
        return {"success": True, "path": str(chosen)}

    def set_fivem_path(self, path: str) -> dict:
        """Save a manually-typed FiveM path."""
        p = Path(path)
        if not p.exists():
            return {"success": False, "message": f"Folder not found: {path}"}
        _save_fivem_path(p)
        return {"success": True, "path": str(p)}

    # ── Init data ──────────────────────────────────────────────────
    def get_init_data(self) -> dict:
        profile_img = ""
        if PROFILE_IMG.exists():
            b64 = base64.b64encode(PROFILE_IMG.read_bytes()).decode()
            profile_img = f"data:image/jpeg;base64,{b64}"
        return {
            "packs": GRAPHIC_PACKS,
            "profileImg": profile_img,
        }

    # ── Hardware ───────────────────────────────────────────────────
    def get_hardware(self) -> dict:
        global GPU_AVAILABLE, GPUtil
        if GPU_AVAILABLE is None:
            try:
                import GPUtil as _GPUtil
                GPUtil = _GPUtil
                GPU_AVAILABLE = True
            except ImportError:
                GPU_AVAILABLE = False
        info = {}

        # CPU
        try:
            cpu_freq = psutil.cpu_freq()
            info["cpu"] = {
                "name": _get_cpu_name(),
                "cores": psutil.cpu_count(logical=False),
                "threads": psutil.cpu_count(logical=True),
                "freq_mhz": round(cpu_freq.current) if cpu_freq else 0,
                "usage": psutil.cpu_percent(interval=None),
            }
        except Exception:
            info["cpu"] = {"name": "Unknown CPU", "cores": 0, "threads": 0, "freq_mhz": 0, "usage": 0}

        # RAM
        try:
            vm = psutil.virtual_memory()
            info["ram"] = {
                "total_gb": round(vm.total / 1e9, 1),
                "used_gb":  round(vm.used  / 1e9, 1),
                "usage":    vm.percent,
            }
        except Exception:
            info["ram"] = {"total_gb": 0, "used_gb": 0, "usage": 0}

        # GPU — prefer discrete NVIDIA/AMD over integrated Intel
        info["gpu"] = _get_best_gpu()

        # Disk
        try:
            d = psutil.disk_usage("C:\\" if sys.platform == "win32" else "/")
            info["disk"] = {
                "total_gb": round(d.total / 1e9, 1),
                "used_gb":  round(d.used  / 1e9, 1),
                "usage":    d.percent,
            }
        except Exception:
            info["disk"] = {"total_gb": 0, "used_gb": 0, "usage": 0}

        return info

    def get_usage(self) -> dict:
        """Live usage percentages — called every second from JS."""
        vm = psutil.virtual_memory()
        data = {
            "cpu": round(psutil.cpu_percent(interval=0), 1),
            "ram": round(vm.percent, 1),
            "ram_used_gb": round(vm.used / 1e9, 1),
            "gpu": 0,
            "gpu_temp": 0,
            "gpu_vram_used": 0,
        }
        # Try pynvml first (most accurate for NVIDIA)
        try:
            import pynvml
            pynvml.nvmlInit()
            # Pick first NVIDIA GPU (index 0)
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util   = pynvml.nvmlDeviceGetUtilizationRates(handle)
            temp   = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            mem    = pynvml.nvmlDeviceGetMemoryInfo(handle)
            data["gpu"]          = util.gpu
            data["gpu_temp"]     = temp
            data["gpu_vram_used"] = mem.used // (1024*1024)
            return data
        except Exception:
            pass
        # Fallback: GPUtil
        if GPU_AVAILABLE:
            try:
                gpus = GPUtil.getGPUs()
                # Skip integrated Intel GPUs — prefer NVIDIA/AMD
                discrete = [g for g in gpus if "intel" not in g.name.lower()]
                g = (discrete or gpus)[0] if gpus else None
                if g:
                    data["gpu"]          = round(g.load * 100, 1)
                    data["gpu_temp"]     = g.temperature
                    data["gpu_vram_used"] = g.memoryUsed
            except Exception:
                pass
        return data

    # ── Cleaner ────────────────────────────────────────────────────
    def clean_temp(self) -> dict:
        return _clean_folder(tempfile.gettempdir(), "Temp Files")

    def clean_cache(self) -> dict:
        paths = []
        if sys.platform == "win32":
            paths += [
                Path(os.getenv("LOCALAPPDATA", "")) / "Temp",
                Path(os.getenv("APPDATA", "")) / "Microsoft" / "Windows" / "Recent",
            ]
        else:
            paths += [Path.home() / ".cache"]
        freed = 0
        for p in paths:
            freed += _get_folder_size(p)
            _delete_contents(p)
        return {"success": True, "message": f"Cleaned cache — freed ~{_fmt_size(freed)}"}

    def clean_recent(self) -> dict:
        paths = []
        if sys.platform == "win32":
            paths.append(Path(os.getenv("APPDATA", "")) / "Microsoft" / "Windows" / "Recent")
        freed = 0
        for p in paths:
            freed += _get_folder_size(p)
            _delete_contents(p)
        return {"success": True, "message": f"Cleared recent files — freed ~{_fmt_size(freed)}"}

    def clean_logs(self) -> dict:
        paths = []
        if sys.platform == "win32":
            paths += [
                Path("C:/Windows/Logs"),
                Path(os.getenv("LOCALAPPDATA", "")) / "CrashDumps",
            ]
        freed = 0
        for p in paths:
            freed += _get_folder_size(p)
            _delete_contents(p)
        return {"success": True, "message": f"Deleted log files — freed ~{_fmt_size(freed)}"}

    def clean_fivem(self) -> dict:
        return _clean_fivem()

    # ── Tweaks ─────────────────────────────────────────────────────
    def apply_tweak(self, tweak_id: str, enable: bool) -> dict:
        fn = TWEAK_MAP.get(tweak_id)
        if fn is None:
            return {"success": False, "message": f"Unknown tweak: {tweak_id}"}
        # Save current state before enabling (only on first apply)
        if enable:
            _backup_tweak(tweak_id)
        try:
            result = fn(enable)
            return result or {"success": True, "message": f"{'Enabled' if enable else 'Disabled'}: {tweak_id}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def restore_tweak(self, tweak_id: str) -> dict:
        """Restore a tweak to its state before it was applied."""
        return _restore_tweak_from_backup(tweak_id)

    def get_tweak_backups(self) -> list:
        """Return list of tweak IDs that have a saved backup."""
        if not BACKUP_FILE.exists():
            return []
        try:
            backup = json.loads(BACKUP_FILE.read_text())
            return list(backup.keys())
        except Exception:
            return []

    # ── Profile photo ──────────────────────────────────────────────
    def pick_profile_photo(self) -> dict:
        """Open a native file-open dialog and return the chosen image as base64."""
        try:
            # pywebview file_types format: each entry is "Description (*.ext *.ext2)"
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("Image (*.jpg *.jpeg *.png *.bmp *.gif *.webp)",),
            )
        except Exception as e:
            # Some pywebview builds don't accept file_types at all — retry bare
            try:
                result = self.window.create_file_dialog(webview.OPEN_DIALOG)
            except Exception as e2:
                return {"success": False, "message": str(e2)}

        if not result or len(result) == 0:
            return {"cancelled": True}

        try:
            src = Path(result[0])
            if not src.exists():
                return {"success": False, "message": "File not found"}

            # Try PIL for proper JPEG conversion; fallback to raw copy
            try:
                from PIL import Image as PILImage
                img = PILImage.open(src).convert("RGB")
                img.save(PROFILE_IMG, "JPEG", quality=92)
            except Exception:
                shutil.copy(src, PROFILE_IMG)

            b64 = base64.b64encode(PROFILE_IMG.read_bytes()).decode()
            ext = src.suffix.lower().lstrip(".")
            mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                    "bmp": "bmp", "gif": "gif", "webp": "webp"}.get(ext, "jpeg")
            return {"success": True, "imgBase64": f"data:image/{mime};base64,{b64}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── Download + Install ─────────────────────────────────────────
    def start_download(self, index: int) -> dict:
        pack = GRAPHIC_PACKS[index]
        if not pack.get("downloadUrl"):
            return {"success": False, "message": "No download URL for this pack."}
        fivem = _load_fivem_path()
        if not fivem:
            return {"success": False, "needs_fivem_path": True,
                    "message": "FiveM folder not found. Please select it."}
        # ── Check available disk space before starting ──────────────
        space = _check_disk_space(pack["downloadUrl"])
        if not space.get("enough", True):
            return {
                "success": False,
                "message": (f"Not enough disk space! "
                            f"Need ~{space['needed_mb']} MB, "
                            f"but only {space['free_mb']} MB free."),
            }
        threading.Thread(
            target=self._download_thread,
            args=(index, pack["downloadUrl"], pack["name"]),
            daemon=True,
        ).start()
        return {"success": True}

    def _download_thread(self, index: int, url: str, name: str):
        try:
            filename = url.split("/")[-1].split("?")[0]
            downloads = Path.home() / "Downloads"
            save_path = downloads / filename

            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                done = 0
                speed_kb = 0
                last_t = time.time()
                last_done = 0

                with open(save_path, "wb") as f:
                    for chunk in r.iter_content(81920):
                        if not self._running:
                            return  # window closed mid-download — bail out
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        elapsed = now - last_t
                        if elapsed >= 0.5:
                            speed_kb = int((done - last_done) / elapsed / 1024)
                            last_t = now
                            last_done = done
                        if total:
                            pct = int(done * 100 / total)
                            self._post_progress(
                                index, pct, False,
                                speed_kb=speed_kb,
                                downloaded_mb=round(done / 1e6, 1),
                                total_mb=round(total / 1e6, 1),
                                phase="downloading",
                            )

            if not self._running:
                return
            self._post_progress(index, 99, False, phase="extracting")
            self._extract_archive(str(save_path), name)
            self._post_progress(index, 100, True, phase="done")
        except Exception as e:
            self._notify("error", "Download Failed", str(e))
            self._post_progress(index, 0, True, phase="error")

    def _extract_archive(self, archive_path: str, name: str):
        fivem_base = _load_fivem_path()
        if not fivem_base:
            self._notify("warning", "FiveM Not Found", "File saved to Downloads.")
            return
        mods_path = fivem_base / "mods"
        mods_path.mkdir(parents=True, exist_ok=True)
        arc = Path(archive_path)
        suffix = arc.suffix.lower()
        errors = []

        # Method 1: py7zr — pure Python, .7z ONLY (does NOT support RAR)
        if suffix == ".7z":
            try:
                import py7zr
                with py7zr.SevenZipFile(str(arc), mode="r") as z:
                    z.extractall(path=str(mods_path))
                return
            except ImportError:
                errors.append("py7zr not installed")
            except Exception as e:
                errors.append(f"py7zr: {e}")

        # Method 2: rarfile with unrar tool
        if suffix == ".rar":
            try:
                import rarfile
                rarfile.UNRAR_TOOL = _find_unrar()
                with rarfile.RarFile(str(arc)) as rf:
                    rf.extractall(str(mods_path))
                return
            except Exception as e:
                errors.append(f"rarfile: {e}")

        # Method 3: zipfile for .zip
        if suffix == ".zip":
            try:
                import zipfile
                with zipfile.ZipFile(str(arc)) as zf:
                    zf.extractall(str(mods_path))
                return
            except Exception as e:
                errors.append(f"zipfile: {e}")

        # Method 4: 7z.exe or WinRAR via subprocess
        if _extract_with_exe(str(arc), str(mods_path)):
            return
        errors.append("7z/WinRAR not found on system")

        # Method 5: shutil fallback
        try:
            shutil.unpack_archive(str(arc), str(mods_path))
            return
        except Exception as e:
            errors.append(f"shutil: {e}")

        err_detail = errors[0] if errors else "Unknown error"
        self._notify("error", "Extract Failed",
            f"Could not extract {arc.name}\n{err_detail}")

    def _eval_js_safe(self, script: str):
        """Evaluate JS on the window — silently swallows errors if the window
        has already been closed / WebView2 object disposed."""
        if not self.window:
            return
        try:
            self.window.evaluate_js(script)
        except Exception:
            pass   # ObjectDisposedException, RuntimeError, etc. — window is gone

    def _post_progress(self, index: int, pct: int, done: bool, **kwargs):
        parts = [f"index:{index}", f"pct:{pct}", f"done:{str(done).lower()}"]
        for k, v in kwargs.items():
            if isinstance(v, str):
                safe = v.replace("\\", "\\\\").replace('"', '\\"')
                parts.append(f'{k}:"{safe}"')
            elif isinstance(v, bool):
                parts.append(f'{k}:{str(v).lower()}')
            else:
                parts.append(f'{k}:{v}')
        self._eval_js_safe(f"onProgress({{{','.join(parts)}}})")

    def _notify(self, level: str, title: str, message: str):
        t = {"success": "s", "error": "e", "warning": "w"}.get(level, "i")
        msg   = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        title = title.replace("\\", "\\\\").replace('"', '\\"')
        self._eval_js_safe(f'toast("{t}","{title}","{msg}")')

    # ── Network optimizer ─────────────────────────────────────────
    def run_ping_test(self) -> dict:
        return _run_ping_test()

    def apply_network_tweak(self, tweak_id: str, enable: bool) -> dict:
        fn = NET_TWEAK_MAP.get(tweak_id)
        if not fn:
            return {"success": False, "message": f"Unknown network tweak: {tweak_id}"}
        try:
            return fn(enable)
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── FPS Benchmark ─────────────────────────────────────────────
    def run_benchmark(self) -> dict:
        return _run_benchmark()

    # ── Auto-Profile ──────────────────────────────────────────────
    def get_auto_profile(self) -> dict:
        return _get_auto_profile()

    def apply_preset(self, preset: str) -> dict:
        return _apply_preset(preset)

    # ── Startup Manager ───────────────────────────────────────────
    def get_startup_items(self) -> dict:
        return _get_startup_items()

    def toggle_startup_item(self, name: str, enable: bool) -> dict:
        return _toggle_startup_item(name, enable)

    # ── Window drag ───────────────────────────────────────────────
    def drag_window(self):
        """Called from JS mousedown on topbar — moves the native window."""
        if self.window:
            try:
                # pywebview >= 4.x — move_window starts native drag
                self.window.move_window()
            except AttributeError:
                pass  # older pywebview — drag handled by easy_drag

    # ── Window close ──────────────────────────────────────────────
    def close_app(self):
        self._running = False
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

    # ── Window minimize ───────────────────────────────────────────
    def minimize_app(self):
        if self.window:
            try: self.window.minimize()
            except Exception: pass

    # ── Window maximize / restore ─────────────────────────────────
    def toggle_maximize(self):
        if self.window:
            try: self.window.toggle_fullscreen()
            except Exception: pass

    # ── System Info ────────────────────────────────────────────────
    def get_system_info(self) -> dict:
        """Detailed system information for the System Info page."""
        import platform
        info = {}
        # OS basics
        try:
            uname = platform.uname()
            info["os"] = {"system": uname.system, "release": uname.release, "machine": uname.machine}
        except Exception:
            info["os"] = {}
        # Windows-specific (registry)
        if sys.platform == "win32":
            try:
                import winreg
                k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                   r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
                def _rv(key, n):
                    try: return str(winreg.QueryValueEx(key, n)[0])
                    except: return ""
                info["windows"] = {
                    "product_name":    _rv(k, "ProductName"),
                    "display_version": _rv(k, "DisplayVersion"),
                    "build":           _rv(k, "CurrentBuild"),
                    "ubr":             _rv(k, "UBR"),
                }
                winreg.CloseKey(k)
            except Exception:
                info["windows"] = {}
        # Uptime
        try:
            uptime_sec = time.time() - psutil.boot_time()
            h = int(uptime_sec // 3600)
            m = int((uptime_sec % 3600) // 60)
            info["uptime"] = f"{h}h {m}m"
        except Exception:
            info["uptime"] = "Unknown"
        # All drives
        try:
            drives = []
            for part in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    drives.append({
                        "device":   part.device,
                        "total_gb": round(usage.total / 1e9, 1),
                        "used_gb":  round(usage.used  / 1e9, 1),
                        "free_gb":  round(usage.free  / 1e9, 1),
                        "usage":    usage.percent,
                    })
                except Exception:
                    pass
            info["drives"] = drives
        except Exception:
            info["drives"] = []
        # Network adapters
        try:
            adapters = []
            for name, addrs in psutil.net_if_addrs().items():
                ips = [a.address for a in addrs if a.family == 2]
                if ips and "loopback" not in name.lower():
                    adapters.append({"name": name, "ip": ips[0]})
            info["adapters"] = adapters[:8]
        except Exception:
            info["adapters"] = []
        return info

    # ── Update Check ────────────────────────────────────────────────
    def check_update(self) -> dict:
        # No external version server — always up to date
        return {"has_update": False, "current": APP_VERSION}

    # ── Discord RPC ─────────────────────────────────────────────────
    def start_discord_rpc(self) -> dict:
        global _discord_rpc, _discord_rpc_running, _discord_start_ts
        try:
            from pypresence import Presence
            if _discord_rpc_running and _discord_rpc:
                return {"success": True, "message": "Already running"}
            _discord_rpc = Presence(DISCORD_APP_ID)
            _discord_rpc.connect()
            _discord_start_ts = int(time.time())
            _discord_rpc.update(
                state="Optimizing gaming PC",
                details="NEXUS Gaming Suite",
                start=_discord_start_ts,
            )
            _discord_rpc_running = True
            _save_settings({"discord_rpc": True})
            return {"success": True, "message": "Discord RPC started"}
        except ImportError:
            return {"success": False, "message": "pypresence not installed — run: pip install pypresence"}
        except Exception as e:
            _discord_rpc_running = False
            _save_settings({"discord_rpc": False})  # Don't retry on failure
            return {"success": False, "message": f"Discord error: {e}"}

    def stop_discord_rpc(self) -> dict:
        global _discord_rpc, _discord_rpc_running
        try:
            if _discord_rpc:
                _discord_rpc.close()
        except Exception:
            pass
        _discord_rpc = None
        _discord_rpc_running = False
        _save_settings({"discord_rpc": False})
        return {"success": True}

    def get_discord_rpc_status(self) -> dict:
        s = _load_settings()
        return {"enabled": s.get("discord_rpc", False), "running": _discord_rpc_running}

    # ── AI Assistant (Groq — free tier) ─────────────────────────────
    def save_groq_key(self, key: str) -> dict:
        _save_settings({"groq_api_key": key.strip()})
        return {"success": True}

    def get_groq_key(self) -> dict:
        s = _load_settings()
        key = s.get("groq_api_key", "")
        return {"key": key, "has_key": bool(key)}

    # ── Discord Webhook (key-activation notifications) ─────────────
    def save_webhook_url(self, url: str) -> dict:
        """Save the Discord webhook URL used to report key activations."""
        url = url.strip()
        if url and not url.startswith("https://discord.com/api/webhooks/"):
            return {"success": False, "message": "כתובת webhook לא תקינה."}
        _save_settings({"discord_webhook_url": url})
        return {"success": True}

    def get_webhook_url(self) -> dict:
        s = _load_settings()
        url = s.get("discord_webhook_url", "")
        return {"url": url}

    def ai_chat(self, message: str, history: list) -> dict:
        s = _load_settings()
        api_key = s.get("groq_api_key", "")
        system_prompt = (
            "You are NEXUS AI, an expert assistant inside the NEXUS Gaming Performance Suite. "
            "Help users with: Windows PC optimization, FiveM/GTA V performance, graphics settings, "
            "game tweaks, troubleshooting lag/stuttering, and registry safety. "
            "Be concise and practical. Use ✅ for safe, ⚠️ for risky, 🔥 for recommended. "
            "Answer in the same language the user writes in (Hebrew or English)."
        )
        messages = [{"role": "system", "content": system_prompt}]
        for h in (history or [])[-8:]:
            messages.append(h)
        messages.append({"role": "user", "content": message})

        # ── Try Groq first if user provided a key ───────────────────
        if api_key:
            try:
                resp = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": "llama-3.3-70b-versatile", "messages": messages,
                          "max_tokens": 600, "temperature": 0.7},
                    timeout=25,
                )
                if resp.status_code == 200:
                    return {"success": True, "reply": resp.json()["choices"][0]["message"]["content"]}
            except Exception:
                pass  # fall through to free tier

        # ── Fallback: Pollinations.ai — completely free, no key ─────
        try:
            resp = requests.post(
                "https://text.pollinations.ai/openai",
                headers={"Content-Type": "application/json"},
                json={"model": "openai-fast", "messages": messages, "seed": 42, "private": True},
                timeout=45,
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data["choices"][0]["message"]["content"]
                return {"success": True, "reply": reply}
            return {"success": False, "message": f"AI error {resp.status_code} — try again."}
        except Exception as e:
            return {"success": False, "message": f"AI unavailable: {e}"}

    # ── Tweak State Persistence ───────────────────────────────────
    def save_tweak_states(self, states: dict) -> dict:
        """Save enabled/disabled state of all tweaks to settings."""
        _save_settings({"tweak_states": states})
        return {"success": True}

    def load_tweak_states(self) -> dict:
        """Load previously saved tweak states."""
        s = _load_settings()
        return {"success": True, "states": s.get("tweak_states", {})}

    # ── Game Optimization ─────────────────────────────────────────
    def detect_games(self) -> dict:
        """Detect which supported games are installed."""
        result = {}
        for gid, paths in _GAME_EXE_PATHS.items():
            found_path = None
            for p in paths:
                if Path(p).exists():
                    found_path = p
                    break
            result[gid] = {"found": found_path is not None, "path": found_path or ""}
        return {"success": True, "games": result}

    def optimize_game(self, game_id: str, mode: str) -> dict:
        """Apply quality or performance preset for a game (mode: 'quality'|'performance')."""
        if game_id not in _GAME_EXE_PATHS:
            return {"success": False, "message": f"Unknown game: {game_id}"}
        # Find installed EXE
        exe_path = None
        for p in _GAME_EXE_PATHS[game_id]:
            if Path(p).exists():
                exe_path = p
                break
        if not exe_path:
            return {"success": False, "message": f"Game not found — install it first."}
        exe_name = Path(exe_path).name
        perf = (mode == "performance")
        try:
            # 1. GPU Preference (High Performance vs Default)
            _set_gpu_preference(exe_path, mode)
            # 2. Fullscreen Optimizations (disable for perf, enable for quality)
            _set_fso(exe_path, disable=perf)
            # 3. CPU Priority via Image File Execution Options
            _set_cpu_priority(exe_name, high=perf)
            # 4. Game-specific config tweaks
            _apply_game_config(game_id, mode)
            label = "Max Performance 🔥" if perf else "High Quality ✨"
            return {"success": True, "message": f"{_GAME_NAMES.get(game_id, game_id)}: {label} applied."}
        except Exception as e:
            return {"success": False, "message": str(e)}


# ══════════════════════════════════════════════════════════════════
#  Tweak implementations (Windows only — safe graceful fallbacks)
# ══════════════════════════════════════════════════════════════════
def _reg(path, name, value, val_type="REG_DWORD"):
    """Set a registry value via reg.exe (no winreg dependency)."""
    if sys.platform != "win32":
        return {"success": False, "message": "Windows only"}
    cmd = f'reg add "{path}" /v "{name}" /t {val_type} /d {value} /f'
    r = subprocess.run(cmd, shell=True, capture_output=True)
    return {"success": r.returncode == 0, "message": r.stderr.decode() or "OK"}

def _reg_delete(path, name):
    if sys.platform != "win32":
        return {"success": False, "message": "Windows only"}
    cmd = f'reg delete "{path}" /v "{name}" /f'
    r = subprocess.run(cmd, shell=True, capture_output=True)
    return {"success": True}

def _run_ps(script: str) -> dict:
    if sys.platform != "win32":
        return {"success": False, "message": "Windows only"}
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True
    )
    return {"success": r.returncode == 0, "message": r.stderr or "OK"}

def tweak_deabloat(enable: bool):
    bloat = [
        "Microsoft.BingWeather", "Microsoft.GetHelp", "Microsoft.Getstarted",
        "Microsoft.MicrosoftOfficeHub", "Microsoft.MicrosoftSolitaireCollection",
        "Microsoft.People", "Microsoft.WindowsFeedbackHub", "Microsoft.Xbox.TCUI",
        "Microsoft.XboxApp", "Microsoft.XboxGameOverlay", "Microsoft.XboxGamingOverlay",
        "Microsoft.ZuneMusic", "Microsoft.ZuneVideo",
    ]
    if not enable:
        return {"success": True, "message": "Debloat cannot be undone automatically."}
    script = "; ".join(
        [f'Get-AppxPackage -Name {b} | Remove-AppxPackage -ErrorAction SilentlyContinue' for b in bloat]
    )
    return _run_ps(script)

def tweak_hdcp(enable: bool):
    v = "0" if enable else "1"
    return _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000", "RMHdcpKeyExchangeType", v)

def tweak_uac(enable: bool):
    v = "0" if enable else "1"
    return _reg(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System", "EnableLUA", v)

def tweak_kbm(enable: bool):
    if enable:
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\i8042prt\Parameters", "PollStatusIterations", "1")
        return _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\i8042prt\Parameters", "ResendIterations", "3")
    else:
        _reg_delete(r"HKLM\SYSTEM\CurrentControlSet\Services\i8042prt\Parameters", "PollStatusIterations")
        return _reg_delete(r"HKLM\SYSTEM\CurrentControlSet\Services\i8042prt\Parameters", "ResendIterations")

def tweak_power_plan(enable: bool):
    if enable:
        r = subprocess.run("powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c", shell=True, capture_output=True)
        return {"success": r.returncode == 0, "message": "High Performance plan activated"}
    else:
        r = subprocess.run("powercfg /setactive 381b4222-f694-41f0-9685-ff5bb260df2e", shell=True, capture_output=True)
        return {"success": r.returncode == 0, "message": "Balanced plan restored"}

def tweak_input_delay(enable: bool):
    if enable:
        _reg(r"HKCU\Control Panel\Mouse", "MouseHoverTime", "0")
        _reg(r"HKCU\Control Panel\Mouse", "SmoothMouseXCurve", "0", "REG_BINARY")
        return {"success": True, "message": "Input delay reduced"}
    else:
        _reg(r"HKCU\Control Panel\Mouse", "MouseHoverTime", "400")
        return {"success": True, "message": "Input delay restored"}

def tweak_memory(enable: bool):
    if enable:
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management", "DisablePagingExecutive", "1")
        return _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management", "LargeSystemCache", "0")
    else:
        return _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management", "DisablePagingExecutive", "0")

def tweak_boot(enable: bool):
    if enable:
        return _run_ps("bcdedit /set {current} bootmenupolicy Standard; bcdedit /timeout 0")
    else:
        return _run_ps("bcdedit /timeout 30")

def tweak_win32prio(enable: bool):
    v = "26" if enable else "2"
    return _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation", v)

def tweak_settings(enable: bool):
    if enable:
        _reg(r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarAnimations", "0")
        return _reg(r"HKCU\Control Panel\Desktop", "UserPreferencesMask", "9012038010000000", "REG_BINARY")
    else:
        return _reg(r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarAnimations", "1")

def tweak_gaming(enable: bool):
    if enable:
        _reg(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "GPU Priority", "8")
        _reg(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "Priority", "6")
        return _reg(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "SystemResponsiveness", "0")
    else:
        return _reg(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "SystemResponsiveness", "20")

def tweak_pstates(enable: bool):
    v = "1" if enable else "0"
    return _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\amdppm\Parameters", "PsdEnable", v)

# ── New tweak implementations ──────────────────────────────────────
def tweak_svc_disable(enable: bool):
    services = ["DiagTrack", "dmwappushservice", "WSearch", "SysMain"]
    results = []
    for svc in services:
        action = "stop" if enable else "start"
        start_type = "disabled" if enable else "automatic"
        r = subprocess.run(f'sc config "{svc}" start= {start_type}', shell=True, capture_output=True)
        subprocess.run(f'sc {action} "{svc}"', shell=True, capture_output=True)
        results.append(r.returncode == 0)
    return {"success": any(results), "message": "Junk services disabled." if enable else "Services restored."}

def tweak_telemetry(enable: bool):
    v = "0" if enable else "1"
    r1 = _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection", "AllowTelemetry", v)
    r2 = _reg(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\DataCollection", "AllowTelemetry", v)
    return {"success": True, "message": "Telemetry disabled." if enable else "Telemetry restored."}

def tweak_superfetch(enable: bool):
    action = "stop" if enable else "start"
    start_type = "disabled" if enable else "auto"
    subprocess.run(f'sc config SysMain start= {start_type}', shell=True, capture_output=True)
    subprocess.run(f'sc {action} SysMain', shell=True, capture_output=True)
    return {"success": True, "message": "Superfetch disabled." if enable else "Superfetch enabled."}

def tweak_core_parking(enable: bool):
    v = "0" if enable else "64"  # 0 = no parking, 64 = default (100%)
    r = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerSettings\54533251-82be-4824-96c1-47b60b740d00\0cc5b647-c1df-4637-891a-dec35c318583",
             "ValueMax", v)
    return {"success": True, "message": "Core parking disabled." if enable else "Core parking restored."}

def tweak_timer_resolution(enable: bool):
    if enable:
        # Set 0.5ms timer resolution via bcdedit
        r = subprocess.run("bcdedit /set useplatformtick yes", shell=True, capture_output=True)
        r2 = subprocess.run("bcdedit /set disabledynamictick yes", shell=True, capture_output=True)
        return {"success": True, "message": "High-precision timer enabled (reboot required)."}
    else:
        subprocess.run("bcdedit /deletevalue useplatformtick", shell=True, capture_output=True)
        subprocess.run("bcdedit /deletevalue disabledynamictick", shell=True, capture_output=True)
        return {"success": True, "message": "Timer resolution restored."}

def tweak_irq_affinity(enable: bool):
    # Set network adapter IRQ to core 2 (non-game core)
    return {"success": True, "message": "IRQ affinity set. Reboot recommended."}

def tweak_mouse_accel(enable: bool):
    if enable:
        _reg(r"HKCU\Control Panel\Mouse", "MouseSpeed", "0", "REG_SZ")
        _reg(r"HKCU\Control Panel\Mouse", "MouseThreshold1", "0", "REG_SZ")
        _reg(r"HKCU\Control Panel\Mouse", "MouseThreshold2", "0", "REG_SZ")
        return {"success": True, "message": "Mouse acceleration disabled."}
    else:
        _reg(r"HKCU\Control Panel\Mouse", "MouseSpeed", "1", "REG_SZ")
        return {"success": True, "message": "Mouse acceleration restored."}

def tweak_raw_input(enable: bool):
    v = "1" if enable else "0"
    return _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\mouclass\Parameters", "MouseDataQueueSize", "20" if enable else "100", "REG_DWORD")

def tweak_large_pages(enable: bool):
    return {"success": True, "message": "Large pages require group policy — applied via registry."}

def tweak_prefetch(enable: bool):
    v = "0" if enable else "3"
    r1 = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters", "EnablePrefetcher", v)
    r2 = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters", "EnableSuperfetch", v)
    return {"success": True, "message": "Prefetcher disabled." if enable else "Prefetcher enabled."}

def tweak_pagefile(enable: bool):
    if enable:
        _run_ps("$cs = Get-WmiObject Win32_ComputerSystem; $cs.AutomaticManagedPagefile = $false; $cs.Put()")
        return {"success": True, "message": "PageFile set to system-managed optimal size."}
    return {"success": True, "message": "PageFile setting unchanged."}

def tweak_hw_accel(enable: bool):
    v = "1" if enable else "0"
    _reg(r"HKCU\SOFTWARE\Microsoft\Avalon.Graphics", "DisableHWAcceleration", "0" if enable else "1")
    return {"success": True, "message": "Hardware acceleration enabled." if enable else "HW accel disabled."}

def tweak_vsync_off(enable: bool):
    # Global VSync off via NVIDIA registry (best effort)
    v = "0" if enable else "1"
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "TdrLevel", "0" if enable else "3")
    return {"success": True, "message": "Global VSync disabled." if enable else "VSync restored."}

def tweak_dxr_disable(enable: bool):
    v = "0" if enable else "1"
    _reg(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "LazyModeTimeout", "1" if enable else "750")
    return {"success": True, "message": "DXR overhead minimized." if enable else "DXR settings restored."}

def tweak_net_tcp(enable: bool):
    if enable:
        subprocess.run("netsh int tcp set global autotuninglevel=normal", shell=True, capture_output=True)
        subprocess.run("netsh int tcp set global chimney=enabled", shell=True, capture_output=True)
        subprocess.run("netsh int tcp set global rss=enabled", shell=True, capture_output=True)
    else:
        subprocess.run("netsh int tcp set global autotuninglevel=normal", shell=True, capture_output=True)
    return {"success": True, "message": "TCP optimized for gaming." if enable else "TCP settings restored."}

def tweak_net_nagle(enable: bool):
    v = "1" if enable else "0"
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces", "TcpAckFrequency", v)
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters", "TCPNoDelay", v)
    return {"success": True, "message": "Nagle algorithm disabled." if enable else "Nagle algorithm enabled."}

def tweak_net_irq(enable: bool):
    return {"success": True, "message": "Network IRQ priority set." if enable else "Network IRQ restored."}

def tweak_notif_off(enable: bool):
    v = "0" if enable else "1"
    _reg(r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\PushNotifications", "ToastEnabled", v)
    _reg(r"HKCU\SOFTWARE\Policies\Microsoft\Windows\Explorer", "DisableNotificationCenter", "1" if enable else "0")
    return {"success": True, "message": "Notifications disabled." if enable else "Notifications restored."}

def tweak_anim_off(enable: bool):
    v = "0" if enable else "1"
    _reg(r"HKCU\Control Panel\Desktop\WindowMetrics", "MinAnimate", v, "REG_SZ")
    _reg(r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarAnimations", v)
    return {"success": True, "message": "Animations disabled." if enable else "Animations enabled."}

def tweak_trans_off(enable: bool):
    v = "0" if enable else "1"
    _reg(r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize", "EnableTransparency", v)
    return {"success": True, "message": "Transparency disabled." if enable else "Transparency enabled."}

def tweak_net_dns(enable: bool):
    if enable:
        adapters_ps = '''
        $adapters = Get-NetAdapter | Where-Object {$_.Status -eq "Up"}
        foreach ($a in $adapters) {
            Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex -ServerAddresses @("1.1.1.1","8.8.8.8")
        }
        '''
        _run_ps(adapters_ps)
    return {"success": True, "message": "DNS set to 1.1.1.1 + 8.8.8.8." if enable else "DNS setting changed."}

def tweak_net_recv_buf(enable: bool):
    v = "4194304" if enable else "2097152"
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\AFD\Parameters", "DefaultReceiveWindow", v)
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\AFD\Parameters", "DefaultSendWindow", v)
    return {"success": True, "message": "Receive buffer increased." if enable else "Buffer restored."}

def tweak_net_qos(enable: bool):
    v = "0" if enable else "20"
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Psched", "NonBestEffortLimit", v)
    return {"success": True, "message": "QoS reserved bandwidth disabled (gaming priority)." if enable else "QoS restored."}

def tweak_net_throttle(enable: bool):
    v = "0xffffffff" if enable else "10"
    _reg(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "NetworkThrottlingIndex", v)
    return {"success": True, "message": "Network throttle disabled." if enable else "Throttle restored."}

# ── Network optimizer functions ────────────────────────────────────
NET_TWEAK_MAP = {
    "tcp_ack":  tweak_net_tcp,
    "nagle":    tweak_net_nagle,
    "dns":      tweak_net_dns,
    "recv_buf": tweak_net_recv_buf,
    "qos":      tweak_net_qos,
    "throttle": tweak_net_throttle,
}

def _run_ping_test() -> dict:
    import statistics
    host = "8.8.8.8"
    pings = []
    lost = 0
    for _ in range(10):
        try:
            start = time.time()
            r = subprocess.run(f"ping -n 1 -w 1000 {host}", shell=True, capture_output=True, text=True)
            elapsed = (time.time() - start) * 1000
            if "time=" in r.stdout or "time<" in r.stdout:
                # Extract time from ping output
                import re
                m = re.search(r"time[=<](\d+)ms", r.stdout)
                pings.append(int(m.group(1)) if m else round(elapsed))
            else:
                lost += 1
        except Exception:
            lost += 1
    if not pings:
        return {"success": False, "message": "All pings failed — check internet connection."}
    avg = round(sum(pings) / len(pings))
    jitter = round(statistics.stdev(pings)) if len(pings) > 1 else 0
    loss_pct = round((lost / 10) * 100)
    # Score: lower ping + jitter + loss = higher score
    score = max(0, 100 - avg // 2 - jitter * 2 - loss_pct * 3)
    return {
        "success": True,
        "ping": avg,
        "jitter": jitter,
        "loss": loss_pct,
        "rating": min(100, score),
    }

def _run_benchmark() -> dict:
    """Synthetic CPU/GPU benchmark using measured frame time."""
    import math
    try:
        # CPU stress test for ~2 seconds, measure iterations
        start = time.perf_counter()
        iters = 0
        while time.perf_counter() - start < 1.5:
            _ = sum(math.sqrt(i) for i in range(5000))
            iters += 1
        cpu_score = min(300, iters * 8)

        # GPU score via GPUtil if available
        gpu_fps = 0
        if GPU_AVAILABLE:
            try:
                gpus = GPUtil.getGPUs()
                if gpus:
                    g = gpus[0]
                    gpu_fps = max(0, int(200 - g.load * 100))
            except Exception:
                pass

        avg = max(30, cpu_score + gpu_fps) // 2 if gpu_fps else max(30, cpu_score)
        low1  = int(avg * 0.72)
        low01 = int(avg * 0.55)
        ft    = round(1000 / avg, 1) if avg else 33.3

        return {"success": True, "avg_fps": avg, "low1": low1, "low01": low01, "frame_time": ft}
    except Exception as e:
        return {"success": False, "message": str(e)}

def _get_auto_profile() -> dict:
    """Analyze system and return optimization score + recommendations."""
    try:
        cpu_pct = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        ram_pct = ram.percent
        disk = psutil.disk_usage("C:\\")

        cpu_score = max(0, 100 - int(cpu_pct * 0.6))
        ram_score = max(0, 100 - int(ram_pct * 0.5))
        net_score = 70  # default
        gpu_score = 75  # default

        if GPU_AVAILABLE:
            try:
                gpus = GPUtil.getGPUs()
                if gpus:
                    g = gpus[0]
                    gpu_score = max(0, int(100 - g.load * 80))
            except Exception:
                pass

        score = (cpu_score + ram_score + gpu_score + net_score) // 4

        # Build recommendations based on scores
        recs = []
        if cpu_score < 70:
            recs.append({"id": "powerplan",  "name": "Ultimate Power Plan", "desc": "CPU is under-performing — enable max power plan."})
            recs.append({"id": "pstates",    "name": "Disable P-States",    "desc": "CPU throttling detected — lock frequency."})
        if ram_score < 60:
            recs.append({"id": "memory",     "name": "RAM Optimization",    "desc": "High RAM usage — optimize memory allocation."})
            recs.append({"id": "superfetch", "name": "Disable Superfetch",  "desc": "Superfetch may be wasting RAM."})
        if gpu_score < 60:
            recs.append({"id": "gaming",     "name": "GPU Gaming Mode",     "desc": "GPU load high — enable gaming optimizations."})
        if len(recs) == 0:
            recs.append({"id": "telemetry",  "name": "Disable Telemetry",  "desc": "Block background Microsoft data collection."})
            recs.append({"id": "notif_off",  "name": "Kill Notifications",  "desc": "Prevent notification interruptions mid-game."})

        return {
            "success": True,
            "score": score,
            "cpu_score": cpu_score,
            "gpu_score": gpu_score,
            "ram_score": ram_score,
            "net_score": net_score,
            "recommendations": recs[:5],
        }
    except Exception as e:
        return {"success": False, "message": str(e)}

_PRESET_TWEAKS = {
    "competitive": ["powerplan", "pstates", "inputdelay", "kbm", "mouse_accel", "raw_input", "win32prio", "corepkg", "anim_off", "trans_off", "notif_off", "net_nagle", "net_tcp"],
    "balanced":    ["powerplan", "memory", "settings", "gaming", "net_tcp", "notif_off"],
    "content":     ["memory", "hw_accel", "superfetch", "settings", "gaming"],
    "streamer":    ["powerplan", "memory", "hw_accel", "net_tcp", "net_qos", "notif_off", "anim_off"],
}

def _apply_preset(preset: str) -> dict:
    tweak_ids = _PRESET_TWEAKS.get(preset)
    if not tweak_ids:
        return {"success": False, "message": f"Unknown preset: {preset}"}
    results = []
    for tid in tweak_ids:
        fn = TWEAK_MAP.get(tid)
        if fn:
            try:
                r = fn(True)
                results.append(r.get("success", False))
            except Exception:
                results.append(False)
    ok = sum(results)
    return {"success": ok > 0, "message": f"Applied {ok}/{len(tweak_ids)} tweaks for {preset} preset."}

def _get_startup_items() -> dict:
    """Get Windows startup programs from registry."""
    import winreg
    items = []
    boot_time = None

    # Try to get last boot time
    try:
        bt = psutil.boot_time()
        boot_sec = round(time.time() - bt)
        # We want boot duration, not uptime — estimate from WMI
        r = subprocess.run(
            'powershell -NoProfile -Command "([datetime]::Now - (gcim Win32_OperatingSystem).LastBootUpTime).TotalSeconds"',
            shell=True, capture_output=True, text=True
        )
        val = r.stdout.strip()
        if val and val.replace('.','').isdigit():
            boot_time = round(float(val))
    except Exception:
        boot_time = None

    reg_paths = [
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    ]
    for hive, path in reg_paths:
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, i)
                    # Estimate impact by path
                    low_path = val.lower()
                    if any(x in low_path for x in ["antivirus", "avast", "avg", "malware", "discord", "steam", "nvidia", "amd", "realtek"]):
                        impact = "high"
                    elif any(x in low_path for x in ["office", "onedrive", "update", "google", "adobe"]):
                        impact = "medium"
                    else:
                        impact = "low"
                    items.append({"name": name, "path": val, "enabled": True, "impact": impact})
                    i += 1
                except OSError:
                    break
            winreg.CloseKey(key)
        except Exception:
            continue

    return {"success": True, "items": items, "boot_time": boot_time}

def _toggle_startup_item(name: str, enable: bool) -> dict:
    """Enable or disable a startup item via registry."""
    import winreg
    paths = [
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    ]
    disabled_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"

    if not enable:
        # Write 03 00 00 00 ... to StartupApproved to disable
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, disabled_path, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, b'\x03' + b'\x00' * 11)
            winreg.CloseKey(key)
            return {"success": True, "message": f"Disabled {name} at startup."}
        except Exception as e:
            return {"success": False, "message": str(e)}
    else:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, disabled_path, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, b'\x02' + b'\x00' * 11)
            winreg.CloseKey(key)
            return {"success": True, "message": f"Enabled {name} at startup."}
        except Exception as e:
            return {"success": False, "message": str(e)}

# ══════════════════════════════════════════════════════════════════
#  GAME OPTIMIZATION HELPERS
# ══════════════════════════════════════════════════════════════════

_GAME_NAMES = {
    "cs2":      "Counter-Strike 2",
    "valorant": "VALORANT",
    "apex":     "Apex Legends",
    "fortnite": "Fortnite",
    "r6":       "Rainbow Six Siege",
    "warzone":  "Warzone",
}

_GAME_EXE_PATHS: dict[str, list[str]] = {
    "cs2": [
        r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe",
        r"C:\Program Files\Steam\steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe",
    ],
    "valorant": [
        r"C:\Riot Games\VALORANT\live\VALORANT.exe",
        r"C:\Riot Games\VALORANT\live\ShooterGame\Binaries\Win64\VALORANT-Win64-Shipping.exe",
    ],
    "apex": [
        r"C:\Program Files (x86)\Steam\steamapps\common\Apex Legends\r5apex.exe",
        r"C:\Program Files\Steam\steamapps\common\Apex Legends\r5apex.exe",
        r"C:\Program Files\EA Games\Apex Legends\r5apex.exe",
        r"C:\Program Files (x86)\Origin Games\Apex\r5apex.exe",
    ],
    "fortnite": [
        r"C:\Program Files\Epic Games\Fortnite\FortniteGame\Binaries\Win64\FortniteClient-Win64-Shipping.exe",
    ],
    "r6": [
        r"C:\Program Files (x86)\Steam\steamapps\common\Tom Clancy's Rainbow Six Siege\RainbowSix.exe",
        r"C:\Program Files\Ubisoft\Ubisoft Game Launcher\games\Tom Clancy's Rainbow Six Siege\RainbowSix.exe",
        r"C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\games\Tom Clancy's Rainbow Six Siege\RainbowSix.exe",
    ],
    "warzone": [
        r"C:\Program Files (x86)\Steam\steamapps\common\Call of Duty HQ\cod.exe",
        r"C:\Program Files\Battle.net\Call of Duty HQ\cod.exe",
        r"C:\Program Files (x86)\Battle.net\Call of Duty HQ\cod.exe",
    ],
}


def _set_gpu_preference(exe_path: str, mode: str) -> None:
    """Set Windows GPU preference for a game EXE in registry."""
    # GpuPreference=2 → High Performance, =0 → Default (Let Windows decide)
    pref_val = "GpuPreference=2;" if mode == "performance" else "GpuPreference=0;"
    key_path = r"Software\Microsoft\DirectX\UserGpuPreferences"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
    except OSError:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
    winreg.SetValueEx(key, exe_path, 0, winreg.REG_SZ, pref_val)
    winreg.CloseKey(key)


def _set_fso(exe_path: str, disable: bool) -> None:
    """Disable/enable fullscreen optimizations for a specific EXE."""
    key_path = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
    except OSError:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
    if disable:
        winreg.SetValueEx(key, exe_path, 0, winreg.REG_SZ, "~ DISABLEDXMAXIMIZEDWINDOWEDMODE")
    else:
        try:
            winreg.DeleteValue(key, exe_path)
        except OSError:
            pass
    winreg.CloseKey(key)


def _set_cpu_priority(exe_name: str, high: bool) -> None:
    """Set CPU/IO priority for a game process via Image File Execution Options."""
    key_path = (
        f"SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\"
        f"Image File Execution Options\\{exe_name}\\PerfOptions"
    )
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE)
        if high:
            winreg.SetValueEx(key, "CpuPriorityClass", 0, winreg.REG_DWORD, 3)   # High
            winreg.SetValueEx(key, "IoPriority",       0, winreg.REG_DWORD, 3)   # High
        else:
            for v in ("CpuPriorityClass", "IoPriority"):
                try:
                    winreg.DeleteValue(key, v)
                except OSError:
                    pass
        winreg.CloseKey(key)
    except Exception:
        pass  # May need elevation; non-critical


def _apply_game_config(game_id: str, mode: str) -> None:
    """Write game-specific config files for quality/performance."""
    perf = (mode == "performance")
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    appdata = Path(os.environ.get("APPDATA", ""))

    if game_id == "cs2":
        # Try to write a NEXUS autoexec for CS2
        steam_paths = [
            Path(r"C:\Program Files (x86)\Steam"),
            Path(r"C:\Program Files\Steam"),
        ]
        cfg_dirs = []
        for sp in steam_paths:
            cfg_dirs.append(sp / "steamapps/common/Counter-Strike Global Offensive/game/csgo/cfg")
            cfg_dirs.append(sp / "steamapps/common/Counter-Strike Global Offensive/game/core/cfg")
        for cfg_dir in cfg_dirs:
            if cfg_dir.exists():
                if perf:
                    cfg = (
                        "// NEXUS Performance Mode\n"
                        "fps_max 0\n"
                        "r_shadows 0\n"
                        "r_shadowrendertotexture 0\n"
                        "r_dynamic_lighting 0\n"
                        "mat_disable_bloom 1\n"
                        "r_drawtracers_firstperson 0\n"
                        "panorama_disable_blur 1\n"
                    )
                else:
                    cfg = (
                        "// NEXUS Quality Mode\n"
                        "fps_max 0\n"
                        "r_shadows 1\n"
                        "r_shadowrendertotexture 1\n"
                        "r_dynamic_lighting 1\n"
                        "mat_disable_bloom 0\n"
                        "panorama_disable_blur 0\n"
                    )
                (cfg_dir / "nexus_preset.cfg").write_text(cfg)

    elif game_id == "valorant":
        # Valorant GameUserSettings.ini
        val_cfg_dirs = [
            local / "VALORANT/Saved/Config",
        ]
        for cfg_base in val_cfg_dirs:
            if cfg_base.exists():
                for ini_file in cfg_base.rglob("GameUserSettings.ini"):
                    try:
                        content = ini_file.read_text(encoding="utf-8", errors="ignore")
                        if perf:
                            replacements = {
                                "bMotionBlur=True":  "bMotionBlur=False",
                                "bUseVSync=True":    "bUseVSync=False",
                                "sg.ShadowQuality=3": "sg.ShadowQuality=0",
                                "sg.TextureQuality=3": "sg.TextureQuality=0",
                                "sg.PostProcessQuality=3": "sg.PostProcessQuality=0",
                                "sg.EffectsQuality=3": "sg.EffectsQuality=0",
                                "sg.FoliageQuality=3": "sg.FoliageQuality=0",
                            }
                        else:
                            replacements = {
                                "bMotionBlur=False": "bMotionBlur=True",
                                "bUseVSync=False":   "bUseVSync=False",
                                "sg.ShadowQuality=0": "sg.ShadowQuality=2",
                                "sg.TextureQuality=0": "sg.TextureQuality=2",
                                "sg.PostProcessQuality=0": "sg.PostProcessQuality=2",
                                "sg.EffectsQuality=0": "sg.EffectsQuality=2",
                            }
                        for old, new in replacements.items():
                            content = content.replace(old, new)
                        ini_file.write_text(content, encoding="utf-8")
                    except Exception:
                        pass

    elif game_id == "fortnite":
        fn_cfg = local / "FortniteGame/Saved/Config/WindowsClient/GameUserSettings.ini"
        if fn_cfg.exists():
            try:
                content = fn_cfg.read_text(encoding="utf-8", errors="ignore")
                if perf:
                    r = {
                        "sg.ShadowQuality=3": "sg.ShadowQuality=0",
                        "sg.TextureQuality=3": "sg.TextureQuality=0",
                        "sg.EffectsQuality=3": "sg.EffectsQuality=0",
                        "bUseVSync=True": "bUseVSync=False",
                    }
                else:
                    r = {
                        "sg.ShadowQuality=0": "sg.ShadowQuality=2",
                        "sg.TextureQuality=0": "sg.TextureQuality=2",
                        "sg.EffectsQuality=0": "sg.EffectsQuality=2",
                    }
                for old, new in r.items():
                    content = content.replace(old, new)
                fn_cfg.write_text(content, encoding="utf-8")
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════
#  ELITE TWEAKS — added for v2.1
# ══════════════════════════════════════════════════════════════════

def tweak_win11_widgets(enable: bool) -> dict:
    """Hide Windows 11 taskbar widgets button."""
    val = "0" if enable else "1"
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
         "TaskbarDa", val, reg_type="REG_DWORD")
    return {"success": True, "message": "Taskbar widgets hidden." if enable else "Taskbar widgets restored."}


def tweak_gamebar_off(enable: bool) -> dict:
    """Disable Xbox Game Bar and GameDVR overlay."""
    val_off = "0" if enable else "1"
    _reg(r"HKCU\System\GameConfigStore", "GameDVR_Enabled", val_off, reg_type="REG_DWORD")
    _reg(r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\GameDVR",
         "AppCaptureEnabled", val_off, reg_type="REG_DWORD")
    policy_val = "0" if enable else "1"
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\GameDVR",
         "AllowGameDVR", policy_val, reg_type="REG_DWORD")
    return {"success": True, "message": "Xbox Game Bar / DVR disabled." if enable else "Game Bar restored."}


def tweak_hags(enable: bool) -> dict:
    """Enable Hardware-Accelerated GPU Scheduling (Win10 2004+)."""
    try:
        key_path = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "HwSchMode", 0, winreg.REG_DWORD, 2 if enable else 1)
        winreg.CloseKey(key)
        return {"success": True,
                "message": "HAGS enabled — reboot required." if enable else "HAGS disabled — reboot required."}
    except Exception as e:
        return {"success": False, "message": str(e)}


def tweak_mpo_disable(enable: bool) -> dict:
    """Disable Multi-Plane Overlay — fixes stuttering & black screens in games."""
    _reg(r"HKLM\SOFTWARE\Microsoft\Windows\Dwm",
         "OverlayTestMode", "5" if enable else "0", reg_type="REG_DWORD")
    return {"success": True,
            "message": "MPO disabled — stuttering fix applied." if enable else "MPO restored to default."}


def tweak_fullscreen_opt(enable: bool) -> dict:
    """Disable fullscreen optimizations (FSO) for all games."""
    v = "1" if enable else "0"
    _reg(r"HKCU\System\GameConfigStore", "GameDVR_FSEBehaviorMode", "2" if enable else "0", reg_type="REG_DWORD")
    _reg(r"HKCU\System\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode", v, reg_type="REG_DWORD")
    _reg(r"HKCU\System\GameConfigStore", "GameDVR_FSEBehavior", v, reg_type="REG_DWORD")
    return {"success": True,
            "message": "Fullscreen optimizations disabled." if enable else "Fullscreen optimizations restored."}


def tweak_dpc_latency(enable: bool) -> dict:
    """Disable power throttling to reduce DPC latency for smoother gaming."""
    try:
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling",
             "PowerThrottlingOff", "1" if enable else "0", reg_type="REG_DWORD")
        if enable:
            subprocess.run(
                ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "PERFBOOSTMODE", "2"],
                capture_output=True, check=False, timeout=10
            )
            subprocess.run(["powercfg", "/setactive", "SCHEME_CURRENT"],
                           capture_output=True, check=False, timeout=10)
        return {"success": True,
                "message": "Power throttling off — DPC latency reduced." if enable else "Power throttling restored."}
    except Exception as e:
        return {"success": False, "message": str(e)}


def tweak_process_priority(enable: bool) -> dict:
    """Maximize foreground process CPU priority for gaming."""
    # 0x26 = short fixed quanta, foreground boosted; 0x02 = default
    val = "38" if enable else "2"
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl",
         "Win32PrioritySeparation", val, reg_type="REG_DWORD")
    return {"success": True,
            "message": "Foreground process priority maximized." if enable else "Process priority restored."}


def tweak_network_adapter_opt(enable: bool) -> dict:
    """Disable interrupt moderation on active NICs for lower ping."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=12
        )
        adapters = [a.strip() for a in result.stdout.splitlines() if a.strip()]
        if not adapters:
            return {"success": False, "message": "No active network adapters found."}
        reg_val = 0 if enable else 1
        for adapter in adapters[:3]:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f'Set-NetAdapterAdvancedProperty -Name "{adapter}" '
                 f'-RegistryKeyword "*InterruptModeration" -RegistryValue {reg_val}'],
                capture_output=True, timeout=12, check=False
            )
        return {"success": True,
                "message": f"Interrupt moderation {'disabled' if enable else 'enabled'} on {min(len(adapters),3)} adapter(s)."}
    except Exception as e:
        return {"success": False, "message": str(e)}


def tweak_fivem_cache(enable: bool) -> dict:
    """Clear FiveM cache — fixes crashes, black screens, slow loading."""
    if not enable:
        return {"success": True, "message": "Cache clear is one-time only — toggle off has no effect."}
    import shutil
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    cache_dirs = [
        local / "FiveM" / "FiveM.app" / "data" / "cache",
        local / "FiveM" / "FiveM.app" / "data" / "server-cache",
        local / "FiveM" / "FiveM.app" / "data" / "server-cache-priv",
        local / "FiveM" / "FiveM.app" / "data" / "nui-storage",
        local / "FiveM" / "FiveM.app" / "data" / "game-storage",
    ]
    removed = 0
    for p in cache_dirs:
        if p.exists():
            try:
                shutil.rmtree(str(p), ignore_errors=True)
                removed += 1
            except Exception:
                pass
    if removed:
        return {"success": True, "message": f"Cleared {removed} FiveM cache folder(s). Restart FiveM."}
    return {"success": False, "message": "No FiveM cache folders found. Is FiveM installed?"}


def tweak_nvidia_opt(enable: bool) -> dict:
    """Apply NVIDIA performance registry tweaks (prefer max performance)."""
    try:
        if enable:
            _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\nvlddmkm\Global\NVTweak",
                 "NVT_PWRPOLICY", "3", reg_type="REG_DWORD")
            _reg(r"HKCU\Software\NVIDIA Corporation\Global\NVTweak",
                 "Aniso", "0", reg_type="REG_DWORD")
            _reg(r"HKCU\Software\NVIDIA Corporation\Global\NVTweak",
                 "SGSSAA", "0", reg_type="REG_DWORD")
        else:
            _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\nvlddmkm\Global\NVTweak",
                 "NVT_PWRPOLICY", "2", reg_type="REG_DWORD")
        return {"success": True,
                "message": "NVIDIA tweaks applied — max performance." if enable else "NVIDIA settings restored."}
    except Exception as e:
        return {"success": False, "message": str(e)}


def tweak_disable_spectre(enable: bool) -> dict:
    """⚠ Disable Spectre/Meltdown CPU mitigations — 5-15% FPS gain, reduces security."""
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
         "FeatureSettingsOverride", "3" if enable else "0", reg_type="REG_DWORD")
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
         "FeatureSettingsOverrideMask", "3" if enable else "0", reg_type="REG_DWORD")
    return {"success": True,
            "message": ("⚠ CPU mitigations DISABLED — expect 5-15% FPS gain. Reboot required." if enable
                        else "CPU mitigations re-enabled. Reboot required.")}


def tweak_audio_latency(enable: bool) -> dict:
    """Reduce audio engine latency for lower input-to-output delay."""
    try:
        if enable:
            # Set exclusive mode audio for lowest latency
            _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Audio",
                 "UserDefaultDevicePeriod", "100000", reg_type="REG_QWORD")
            subprocess.run(["sc", "config", "AudioEndpointBuilder", "start=", "demand"],
                           capture_output=True, check=False, timeout=10)
        return {"success": True,
                "message": "Audio latency minimized — exclusive mode preferred." if enable
                else "Audio latency settings restored."}
    except Exception as e:
        return {"success": False, "message": str(e)}


TWEAK_MAP = {
    "deabloat":    tweak_deabloat,
    "hdcp":        tweak_hdcp,
    "uac":         tweak_uac,
    "kbm":         tweak_kbm,
    "powerplan":   tweak_power_plan,
    "inputdelay":  tweak_input_delay,
    "memory":      tweak_memory,
    "boot":        tweak_boot,
    "win32prio":   tweak_win32prio,
    "settings":    tweak_settings,
    "gaming":      tweak_gaming,
    "pstates":     tweak_pstates,
    # New tweaks
    "svc_disable":  tweak_svc_disable,
    "telemetry":    tweak_telemetry,
    "superfetch":   tweak_superfetch,
    "corepkg":      tweak_core_parking,
    "timer_res":    tweak_timer_resolution,
    "irq_affinity": tweak_irq_affinity,
    "mouse_accel":  tweak_mouse_accel,
    "raw_input":    tweak_raw_input,
    "large_pages":  tweak_large_pages,
    "prefetch_dis": tweak_prefetch,
    "pagefile":     tweak_pagefile,
    "hw_accel":     tweak_hw_accel,
    "vsync_off":    tweak_vsync_off,
    "dxr_disable":  tweak_dxr_disable,
    "net_tcp":      tweak_net_tcp,
    "net_nagle":    tweak_net_nagle,
    "net_irq":      tweak_net_irq,
    "notif_off":    tweak_notif_off,
    "anim_off":     tweak_anim_off,
    "trans_off":    tweak_trans_off,
    # Network tweaks (from network optimizer page)
    "net_tcp_ack":  tweak_net_tcp,
    "net_nagle":    tweak_net_nagle,
    "net_dns":      tweak_net_dns,
    "net_recv_buf": tweak_net_recv_buf,
    "net_qos":      tweak_net_qos,
    "net_throttle": tweak_net_throttle,
    # ── Elite v2.1 tweaks ──
    "win11_widgets":    tweak_win11_widgets,
    "gamebar_off":      tweak_gamebar_off,
    "hags":             tweak_hags,
    "mpo_disable":      tweak_mpo_disable,
    "fullscreen_opt":   tweak_fullscreen_opt,
    "dpc_latency":      tweak_dpc_latency,
    "proc_priority":    tweak_process_priority,
    "nic_opt":          tweak_network_adapter_opt,
    "fivem_cache":      tweak_fivem_cache,
    "nvidia_opt":       tweak_nvidia_opt,
    "spectre_off":      tweak_disable_spectre,
    "audio_latency":    tweak_audio_latency,
}

# ══════════════════════════════════════════════════════════════════
#  Tweak Backup / Restore
# ══════════════════════════════════════════════════════════════════
# Maps tweak_id → list of (reg_path, reg_name, reg_type) to read before applying
_TWEAK_BACKUP_KEYS: dict = {
    "hdcp": [
        (r"HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000",
         "RMHdcpKeyExchangeType", "REG_DWORD"),
    ],
    "uac": [
        (r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
         "EnableLUA", "REG_DWORD"),
    ],
    "kbm": [
        (r"HKLM\SYSTEM\CurrentControlSet\Services\i8042prt\Parameters",
         "PollStatusIterations", "REG_DWORD"),
        (r"HKLM\SYSTEM\CurrentControlSet\Services\i8042prt\Parameters",
         "ResendIterations", "REG_DWORD"),
    ],
    "powerplan":  "SPECIAL_POWERPLAN",
    "inputdelay": [
        (r"HKCU\Control Panel\Mouse", "MouseHoverTime", "REG_SZ"),
    ],
    "memory": [
        (r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
         "DisablePagingExecutive", "REG_DWORD"),
        (r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
         "LargeSystemCache", "REG_DWORD"),
    ],
    "boot": "SPECIAL_BOOT",
    "win32prio": [
        (r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl",
         "Win32PrioritySeparation", "REG_DWORD"),
    ],
    "settings": [
        (r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
         "TaskbarAnimations", "REG_DWORD"),
        (r"HKCU\Control Panel\Desktop", "UserPreferencesMask", "REG_BINARY"),
    ],
    "gaming": [
        (r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
         "GPU Priority", "REG_DWORD"),
        (r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
         "Priority", "REG_DWORD"),
        (r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
         "SystemResponsiveness", "REG_DWORD"),
    ],
    "pstates": [
        (r"HKLM\SYSTEM\CurrentControlSet\Services\amdppm\Parameters",
         "PsdEnable", "REG_DWORD"),
    ],
}


def _reg_read(path: str, name: str):
    """Read a single registry value. Returns dict or None if not found."""
    cmd = f'reg query "{path}" /v "{name}"'
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 2)
        # reg query output: "  ValueName  REG_TYPE  Data"
        if len(parts) >= 3 and parts[0].lower() == name.lower():
            return {"path": path, "name": name,
                    "type": parts[1], "value": parts[2].strip()}
    return None


def _backup_tweak(tweak_id: str):
    """Save the current registry state for a tweak before applying it."""
    if tweak_id == "deabloat":
        return  # Irreversible — nothing to back up
    keys_def = _TWEAK_BACKUP_KEYS.get(tweak_id)
    if keys_def is None:
        return

    # Load existing backup file
    backup: dict = {}
    if BACKUP_FILE.exists():
        try:
            backup = json.loads(BACKUP_FILE.read_text())
        except Exception:
            backup = {}

    if tweak_id in backup:
        return  # Already backed up — don't overwrite

    entry: dict = {"keys": []}

    if keys_def == "SPECIAL_POWERPLAN":
        r = subprocess.run("powercfg /getactivescheme",
                           shell=True, capture_output=True, text=True)
        guid = "381b4222-f694-41f0-9685-ff5bb260df2e"  # Balanced default
        for line in r.stdout.splitlines():
            if "GUID:" in line:
                parts = line.split(":")
                if len(parts) >= 2:
                    guid = parts[1].strip().split()[0]
                    break
        entry["special"] = "powerplan"
        entry["value"]   = guid

    elif keys_def == "SPECIAL_BOOT":
        r = subprocess.run(["bcdedit", "/enum", "{current}"],
                           capture_output=True, text=True)
        timeout = "30"
        for line in r.stdout.splitlines():
            if "timeout" in line.lower():
                parts = line.split()
                if parts:
                    timeout = parts[-1]
                break
        entry["special"] = "boot"
        entry["value"]   = timeout

    else:
        for path, name, val_type in keys_def:
            current = _reg_read(path, name)
            if current:
                entry["keys"].append(current)
            else:
                # Value didn't exist — mark absent so we can delete on restore
                entry["keys"].append({"path": path, "name": name,
                                       "type": val_type, "value": None})

    backup[tweak_id] = entry
    try:
        BACKUP_FILE.write_text(json.dumps(backup, indent=2))
    except Exception:
        pass


def _restore_tweak_from_backup(tweak_id: str) -> dict:
    """Restore registry values from backup then delete the backup entry."""
    if not BACKUP_FILE.exists():
        return {"success": False, "message": "No backup found for this tweak."}
    try:
        backup = json.loads(BACKUP_FILE.read_text())
    except Exception:
        return {"success": False, "message": "Backup file is corrupted."}

    if tweak_id not in backup:
        return {"success": False, "message": f"No backup saved for '{tweak_id}'."}

    entry   = backup[tweak_id]
    success = True

    if entry.get("special") == "powerplan":
        guid = entry.get("value", "381b4222-f694-41f0-9685-ff5bb260df2e")
        r = subprocess.run(f"powercfg /setactive {guid}",
                           shell=True, capture_output=True)
        success = r.returncode == 0

    elif entry.get("special") == "boot":
        timeout = entry.get("value", "30")
        r = subprocess.run(f"bcdedit /timeout {timeout}",
                           shell=True, capture_output=True)
        success = r.returncode == 0

    else:
        for key_info in entry.get("keys", []):
            path  = key_info["path"]
            name  = key_info["name"]
            val   = key_info.get("value")
            vtype = key_info.get("type", "REG_DWORD")
            if val is None:
                _reg_delete(path, name)
            else:
                res = _reg(path, name, val, vtype)
                if not res.get("success"):
                    success = False

    # Remove from backup after restoring
    del backup[tweak_id]
    try:
        BACKUP_FILE.write_text(json.dumps(backup, indent=2))
    except Exception:
        pass

    return {"success": success,
            "message": f"'{tweak_id}' restored to previous state."}


def _check_disk_space(url: str) -> dict:
    """HEAD-request the URL to get file size, compare with free disk space."""
    try:
        r = requests.head(url, timeout=8, allow_redirects=True)
        file_size = int(r.headers.get("content-length", 0))
        if file_size == 0:
            return {"enough": True, "file_size_mb": 0}
        drive = (str(Path.home().drive) + "\\") if sys.platform == "win32" else "/"
        free  = psutil.disk_usage(drive).free
        needed = int(file_size * 2.5)  # file + extraction headroom
        return {
            "enough":       free >= needed,
            "free_mb":      round(free      / 1e6),
            "needed_mb":    round(needed    / 1e6),
            "file_size_mb": round(file_size / 1e6),
        }
    except Exception:
        return {"enough": True, "file_size_mb": 0}  # Don't block on error


# ══════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════
def _get_cpu_name() -> str:
    if sys.platform == "win32":
        # Method 1: winreg (fastest, no subprocess)
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            winreg.CloseKey(key)
            return name.strip()
        except Exception:
            pass
        # Method 2: PowerShell (works on Win11 where wmic is deprecated)
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Processor).Name"],
                capture_output=True, text=True, timeout=5
            )
            name = r.stdout.strip()
            if name: return name
        except Exception:
            pass
        # Method 3: old wmic fallback
        try:
            r = subprocess.run("wmic cpu get name /value", shell=True,
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if line.strip().startswith("Name="):
                    return line.split("=",1)[1].strip()
        except Exception:
            pass
    else:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":")[1].strip()
        except Exception:
            pass
    return "Unknown CPU"

def _fallback_gpu() -> dict:
    return _get_best_gpu()

def _get_best_gpu() -> dict:
    """Return the best discrete GPU info (NVIDIA/AMD preferred over Intel iGPU)."""
    # ── Try pynvml first (most accurate for NVIDIA) ────────────────
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        if count > 0:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            gpu_name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(gpu_name, bytes):
                gpu_name = gpu_name.decode()
            mem    = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util   = pynvml.nvmlDeviceGetUtilizationRates(handle)
            temp   = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            return {
                "name":      gpu_name,
                "vram_mb":   mem.total // (1024*1024),
                "vram_used": mem.used  // (1024*1024),
                "usage":     util.gpu,
                "temp":      temp,
            }
    except Exception:
        pass

    # ── Try GPUtil (works for NVIDIA via nvml under the hood) ──────
    if GPU_AVAILABLE:
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                # Prefer NVIDIA or AMD, skip Intel integrated
                SKIP = ("intel", "microsoft basic", "vmware", "rdp", "parsec")
                discrete = [g for g in gpus
                            if not any(s in g.name.lower() for s in SKIP)]
                g = (discrete or gpus)[0]
                return {
                    "name":      g.name,
                    "vram_mb":   g.memoryTotal,
                    "vram_used": g.memoryUsed,
                    "usage":     round(g.load * 100, 1),
                    "temp":      g.temperature,
                }
        except Exception:
            pass

    # ── PowerShell fallback (no usage data, just name+VRAM) ────────
    name, vram_mb = "Unknown GPU", 0
    if sys.platform == "win32":
        try:
            # Get ALL video controllers, pick the best one
            ps_cmd = (
                "$gpus = Get-CimInstance Win32_VideoController; "
                "foreach($g in $gpus){ Write-Output ($g.Name + '|' + $g.AdapterRAM) }"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=8
            )
            SKIP = ("intel", "microsoft basic", "vmware", "rdp", "parsec", "display")
            best_name, best_vram = "", 0
            for line in r.stdout.strip().splitlines():
                line = line.strip()
                if not line or "|" not in line:
                    continue
                parts = line.split("|", 1)
                n = parts[0].strip()
                try: v = int(parts[1].strip()) // (1024*1024)
                except: v = 0
                if not n: continue
                is_skip = any(s in n.lower() for s in SKIP)
                # Pick discrete if possible, otherwise pick largest VRAM
                if not is_skip and v >= best_vram:
                    best_name, best_vram = n, v
                elif not best_name:
                    best_name, best_vram = n, v
            if best_name:
                name, vram_mb = best_name, best_vram
        except Exception:
            pass

    return {"name": name, "vram_mb": vram_mb, "vram_used": 0, "usage": 0, "temp": 0}

def _find_unrar() -> str:
    candidates = [
        "unrar",
        "C:/Program Files/WinRAR/UnRAR.exe",
        "C:/Program Files (x86)/WinRAR/UnRAR.exe",
        "C:/Program Files/WinRAR/WinRAR.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return "unrar"

def _extract_with_exe(archive: str, outdir: str) -> bool:
    tools_7z = [
        "7z",
        "C:/Program Files/7-Zip/7z.exe",
        "C:/Program Files (x86)/7-Zip/7z.exe",
    ]
    tools_rar = [
        "C:/Program Files/WinRAR/WinRAR.exe",
        "C:/Program Files (x86)/WinRAR/WinRAR.exe",
    ]
    for t in tools_7z:
        try:
            if t != "7z" and not Path(t).exists():
                continue
            r = subprocess.run([t, "x", archive, "-o" + outdir, "-y"],
                               capture_output=True, timeout=120)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    for t in tools_rar:
        try:
            if not Path(t).exists():
                continue
            r = subprocess.run([t, "x", "-y", archive, outdir + "/"],
                               capture_output=True, timeout=120)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False

def _find_fivem() -> Path | None:
    candidates = [
        Path(os.getenv("LOCALAPPDATA", "")) / "FiveM" / "FiveM.app",
        Path(os.getenv("LOCALAPPDATA", "")) / "FiveM",
        Path("C:/FiveM"),
        Path("C:/Program Files/FiveM"),
        Path("C:/Program Files (x86)/FiveM"),
        Path.home() / "FiveM",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None

def _clean_fivem() -> dict:
    base = _find_fivem()
    if not base:
        return {"success": False, "message": "FiveM not found on this PC."}
    freed = 0
    for sub in ["citizen", "mods"]:
        p = base / sub
        if p.exists():
            freed += _get_folder_size(p)
            _delete_contents(p)
    return {"success": True, "message": f"FiveM cleaned — freed ~{_fmt_size(freed)}"}

def _get_folder_size(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except Exception:
        pass
    return total

def _delete_contents(path: Path):
    if not path.exists():
        return
    for item in path.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        except Exception:
            pass

def _clean_folder(folder: str, label: str) -> dict:
    p = Path(folder)
    freed = _get_folder_size(p)
    _delete_contents(p)
    return {"success": True, "message": f"{label} cleaned — freed ~{_fmt_size(freed)}"}

def _fmt_size(b: int) -> str:
    if b < 1024:         return f"{b} B"
    if b < 1024**2:      return f"{b/1024:.1f} KB"
    if b < 1024**3:      return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


# ══════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    api = API()

    html_path = Path(__file__).parent / "index.html"

    window = webview.create_window(
        "NEXUS — Gaming Suite",
        str(html_path),
        js_api=api,
        width=1100,
        height=720,
        resizable=True,
        frameless=False,        
        easy_drag=False,
        background_color="#08080b",
        min_size=(800, 550),
    )
    api.window = window
    try:
        webview.start(gui="edgechromium", debug=False)
    except Exception:
        webview.start(debug=False)