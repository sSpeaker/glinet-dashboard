#!/usr/bin/env python3
"""Home network collector.

Polls the GL.iNet router with read-only calls, keeps the latest results in memory
and serves them next to the start page:

  GET /            -> index.html
  GET /api/config  -> start page configuration from config/services.yaml {"config", "error"}
  GET /assets/...  -> images from the config folder (background, icons/)
  GET /api/quote   -> quote of the day (ZenQuotes, FavQs as backup), cached on disk
  GET /api/history -> speed tests, traffic per day and WAN/IP events (data/history.json)
  GET /api/status  -> cached snapshot {"now", "interval", "sources": {name: {data, updated, error}}}
  POST /api/speedtest {"enable": true|false} -> start/stop the router speed test (network-quality.set_speedtest)
  POST /api/background {"url": "https://unsplash.com/photos/..."} | {"reset": true} -> wallpaper set from the page
  POST /api/wan/reconnect {} -> re-dial the WAN (LuCI interface "Restart": /sbin/ifup <wan>)
  POST /api/dns/protection {"enabled": false, "minutes": 30} | {"enabled": true} -> pause/resume AdGuard protection
  POST /api/vpn {"tunnel_id": 1234, "enabled": true|false} -> VPN client tunnel on/off (vpn-client.set_tunnel)
  GET /api/bookmarks -> quick links under the search box {"links": [{id, url, name, title, icon, state}]}
  POST /api/bookmarks {"links": [{"id"?, "url", "name", "refresh"?}]} -> save the list edited on the page
  GET /media/...   -> the downloaded wallpaper; /media/favicons/... the cached quick link icons

Sources (all read-only):
  router   GL RPC   /rpc  system.get_status             CPU, memory, load, uptime, WAN online
  quality  GL WS    /ws   network_quality.status        score, latency, loss, live rate, last speed test
  link     GL WS    /ws   cable.status                  WAN protocol, public IP, gateway
  vpn      GL WS    /ws   vpnclient.status              VPN client tunnels: enabled, connecting/connected
  health   TCP connect to each tile's addr (host:port) every HEALTH_SECONDS: status dots on the tiles
  clients  GL RPC   /rpc  clients.get_list              speed and traffic per device, new devices
  wan      LuCI     :8080/ubus  network.interface dump  WAN uptime and L3 device
                               luci-rpc getNetworkDevices  WAN byte counters
  dns      AdGuard  :3000/control/stats, /control/status  (GL sid sent as the Admin-Token cookie)
"""
import base64
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener, urlopen

import websocket
import yaml

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
CONFIG = Path(os.environ.get("CONFIG", ROOT / "config" / "services.yaml"))

LOG = logging.getLogger("collector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# Colours in page.title: the theme accent, a hex colour or a CSS colour name
COLOR = re.compile(r"^(accent|#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,30})$")
# Images served from the config folder at /assets/<name> or /assets/<folder>/<name>
ASSET = re.compile(r"^(?:[\w-]+/)?[\w.-]+\.(jpe?g|png|webp|avif|gif|svg|ico)$", re.IGNORECASE)
ASSET_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
               "avif": "image/avif", "gif": "image/gif", "svg": "image/svg+xml", "ico": "image/x-icon"}


def asset_url(value, where, folder=""):
    """An image file in the config folder -> versioned /assets URL; http(s) URLs pass through unchanged."""
    value = str(value).strip()
    if value.startswith(("https://", "http://")):
        return value
    rel = f"{folder}/{value}" if folder else value
    if not ASSET.match(rel):
        raise ValueError(f"{where}: {value!r} must be an image file (jpg, png, webp, avif, gif, svg, ico) or an http(s) URL")
    path = CONFIG.parent / rel
    if not path.is_file():
        raise ValueError(f"{where}: {rel} not found in the config folder")
    return f"assets/{rel}?v={path.stat().st_mtime_ns}"


def number(value, where, low, high):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where} must be a number") from None
    if not low <= result <= high:
        raise ValueError(f"{where} must be between {low:g} and {high:g}")
    return result


def normalize_page(page):
    if not isinstance(page, dict):
        raise ValueError("page must be a mapping")
    title = page.get("title") or "Start"
    items = title if isinstance(title, list) else [title]
    parts = []
    for i, item in enumerate(items, 1):
        item = item if isinstance(item, dict) else {"text": item}
        color = item.get("color")
        if color is not None and not COLOR.match(str(color)):
            raise ValueError(f"page.title part #{i}: color must be accent, a #hex colour or a CSS colour name")
        parts.append({"text": str(item.get("text") or ""), "color": str(color) if color else None})
    background = page.get("background")
    if background:
        background = background if isinstance(background, dict) else {"image": background}
        if not background.get("image"):
            raise ValueError("page.background: 'image' is required")
        background = {
            "image": asset_url(background["image"], "page.background.image"),
            "shade": number(background.get("shade", 0.45), "page.background.shade", 0, 0.9),
            "blur": number(background.get("blur", 0), "page.background.blur", 0, 40),
            "tone": str(background.get("tone") or "auto"),
            "fit": str(background.get("fit") or "cover"),
            "credit": str(background.get("credit") or ""),
            "credit_url": str(background.get("credit_url") or ""),
            "location": str(background.get("location") or ""),
        }
        if background["credit_url"] and not background["credit_url"].startswith("https://"):
            raise ValueError("page.background.credit_url must start with https://")
        if background["tone"] not in ("auto", "dark", "light"):
            raise ValueError("page.background.tone must be auto, dark or light")
        if background["fit"] not in ("cover", "contain", "fill"):
            raise ValueError("page.background.fit must be cover, contain or fill")
    theme = str(page.get("theme") or "auto")
    if theme not in ("auto", "light", "dark"):
        raise ValueError("page.theme must be auto, light or dark")
    return {
        "title": parts,
        "tab_title": str(page.get("tab_title") or "".join(part["text"] for part in parts)),
        "theme": theme,
        "favicon": asset_url(page["favicon"], "page.favicon") if page.get("favicon") else None,
        "background": background or None,
        "hint_delay": int(number(page.get("hint_delay", 600), "page.hint_delay", 0, 5000)),
        "world_clock": normalize_world_clock(page.get("world_clock")),
    }


TIMEZONE = re.compile(r"^[A-Za-z]+(?:/[A-Za-z0-9_+-]+){0,2}$")


def normalize_world_clock(value):
    """page.world_clock: [{name, timezone}] shown when hovering the clock; off / [] / missing = no hint.
    Zone names (IANA, e.g. Europe/Kyiv) are checked by the browser, which owns the time zone data."""
    if value in (None, False, []):
        return []
    if not isinstance(value, list):
        raise ValueError("page.world_clock must be a list of {name, timezone} (or off)")
    cities = []
    for i, city in enumerate(value, 1):
        where = f"page.world_clock #{i}"
        if not isinstance(city, dict) or not city.get("name") or not city.get("timezone"):
            raise ValueError(f"{where}: 'name' and 'timezone' are required, e.g. {{name: Kyiv, timezone: Europe/Kyiv}}")
        zone = str(city["timezone"])
        if not TIMEZONE.match(zone):
            raise ValueError(f"{where}: timezone must be an IANA zone name such as Europe/Kyiv or America/New_York")
        cities.append({"name": str(city["name"])[:40], "timezone": zone})
    return cities[:6]


def normalize_schedule(value):
    """speedtest.schedule as "HH:MM" or None. Unquoted 03:00 reaches us as 180: YAML 1.1 reads it as base-60."""
    if value in (None, "", False):
        return None
    if isinstance(value, int) and 0 <= value < 24 * 60:
        value = f"{value // 60:02d}:{value % 60:02d}"
    value = str(value).strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError(f'speedtest.schedule must be a time like "03:00", got {value!r}')
    return value


HOST_PORT = re.compile(r"[\w.-]+:\d{1,5}")


def health_target(service, where, url):
    """host:port probed for the tile's status dot, or "" for no check.

    Default: the service's addr when it is host:port (LAN backends). check: false turns it off,
    check: url probes the host of url (port 443/80 by scheme), check: host:port probes that.
    """
    check = service.get("check", True)
    addr = str(service.get("addr") or "").strip()
    if check is True or check is None:
        return addr if HOST_PORT.fullmatch(addr) else ""
    if check is False:
        return ""
    if str(check) == "url":
        parts = urlsplit(url)
        return f"{parts.hostname}:{parts.port or (443 if parts.scheme == 'https' else 80)}"
    if HOST_PORT.fullmatch(str(check)):
        return str(check)
    raise ValueError(f"{where}: check must be true, false, url or host:port")


# Alert thresholds (alerts: in services.yaml); a level set to off/false/null is not checked
ALERT_DEFAULTS = {
    "cpu_temp": {"warning": 80, "critical": 90},      # router CPU, °C
    "memory": {"warning": 85, "critical": 95},        # router memory used, %
    "load": {"warning": 3.0, "critical": 4.0},        # router 5-minute load average (Flint 2: 4 cores)
    "dns_avg_ms": {"warning": 100, "critical": None},       # AdGuard average processing time, ms
    "dns_upstream_ms": {"warning": 150, "critical": None},  # average answer time of any upstream, ms
}
ALERT_FLAGS = {"router_unreachable": True, "wan_down": True}
ALERT_MINUTES = {"router_restart_minutes": 15, "wan_event_minutes": 15}
# Last speed test against the internet plan: Mbps of the plan, alert below these % of it
SPEEDTEST_ALERT = {"download": None, "upload": None, "warning": 50, "critical": 25}


def normalize_alerts(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("alerts must be a mapping")
    unknown = set(raw) - set(ALERT_DEFAULTS) - set(ALERT_FLAGS) - set(ALERT_MINUTES) - {"speedtest"}
    if unknown:
        raise ValueError(f"alerts: unknown key {sorted(unknown)[0]!r}")
    result = {}
    for key, default in ALERT_DEFAULTS.items():
        value = raw.get(key, default)
        if value in (False, None):
            value = {"warning": None, "critical": None}
        if not isinstance(value, dict) or set(value) - {"warning", "critical"}:
            raise ValueError(f"alerts.{key} must be a mapping with warning and/or critical (or off)")
        levels = {}
        for level in ("warning", "critical"):
            v = value.get(level, default[level]) if key in raw else default[level]
            levels[level] = None if v in (False, None) else number(v, f"alerts.{key}.{level}", 0, 100000)
        result[key] = levels
    for key, default in ALERT_FLAGS.items():
        value = raw.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"alerts.{key} must be true or false")
        result[key] = value
    for key, default in ALERT_MINUTES.items():
        value = raw.get(key, default)
        result[key] = 0 if value is False else int(number(value, f"alerts.{key}", 0, 1440))
    speed = raw.get("speedtest") or {}
    if not isinstance(speed, dict) or set(speed) - set(SPEEDTEST_ALERT):
        raise ValueError("alerts.speedtest must be a mapping with download, upload, warning, critical")
    result["speedtest"] = {}
    for key, default in SPEEDTEST_ALERT.items():
        value = speed.get(key, default)
        high = 100000 if key in ("download", "upload") else 100
        result["speedtest"][key] = None if value in (False, None) else number(value, f"alerts.speedtest.{key}", 1, high)
    return result


def normalize_config(raw):
    """Validate services.yaml and fill defaults; raises ValueError with a readable location."""
    if not isinstance(raw, dict):
        raise ValueError("the file must be a mapping with page, network and groups")
    network = raw.get("network") or {}
    if not isinstance(network, dict):
        raise ValueError("network must be a mapping")
    groups = []
    for gi, group in enumerate(raw.get("groups") or [], 1):
        if not isinstance(group, dict) or not group.get("name"):
            raise ValueError(f"groups #{gi}: 'name' is required")
        services = []
        for si, service in enumerate(group.get("services") or [], 1):
            where = f"{group['name']} / service #{si}"
            if not isinstance(service, dict) or not service.get("name") or not service.get("url"):
                raise ValueError(f"{where}: 'name' and 'url' are required")
            name, url = str(service["name"]), str(service["url"])
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"{where} ({name}): url must start with http:// or https://")
            initials = "".join(word[0] for word in name.split()[:2]) if " " in name else name[:2]
            services.append({
                "name": name,
                "mark": str(service.get("mark") or initials).upper(),
                "url": url,
                "host": str(service.get("host") or ""),
                "addr": str(service.get("addr") or ""),
                "icon": asset_url(service["icon"], f"{where} ({name}): icon", "icons") if service.get("icon") else "",
                "icon_dark": (asset_url(service["icon_dark"], f"{where} ({name}): icon_dark", "icons")
                              if service.get("icon_dark") else ""),
                "check": health_target(service, f"{where} ({name})", url),
            })
        groups.append({"name": str(group["name"]), "services": services})
    return {
        "page": normalize_page(raw.get("page") or {}),
        "network": {key: str(network.get(key) or "") for key in ("subnet", "label", "router")},
        "groups": groups,
        "speedtest": {"schedule": normalize_schedule((raw.get("speedtest") or {}).get("schedule"))},
        "alerts": normalize_alerts(raw.get("alerts")),
    }


class SiteConfig:
    """services.yaml, re-read whenever its mtime changes; the last valid version is kept on errors.

    While the file is invalid it is re-checked every few seconds too, so an error such as a
    missing icon clears as soon as the file is added, without touching the YAML.
    """

    RETRY_SECONDS = 5

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = None
        self._checked = 0.0
        self._config = None
        self._error = None

    def get(self):
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime_ns
            except OSError as exc:
                self._error = f"{self.path}: {exc.strerror}"
                return {"config": self._config, "error": self._error}
            retry = self._error and time.monotonic() - self._checked > self.RETRY_SECONDS
            if mtime != self._mtime or retry:
                self._mtime, self._checked = mtime, time.monotonic()
                previous = self._error
                try:
                    self._config = normalize_config(yaml.safe_load(self.path.read_text(encoding="utf-8")))
                    self._error = None
                    LOG.info("loaded %s", self.path)
                except (yaml.YAMLError, ValueError) as exc:
                    self._error = f"{self.path.name}: {exc}"
                    if self._error != previous:
                        LOG.warning("%s", self._error)
            return {"config": self._config, "error": self._error}


SITE = SiteConfig(CONFIG)

# Router address: GL_HOST env overrides network.router from services.yaml
GL_HOST = (os.environ.get("GL_HOST")
           or ((SITE.get()["config"] or {}).get("network") or {}).get("router")
           or "192.168.8.1")  # GL.iNet factory default
GL_USER = os.environ.get("GL_USER", "root")
LUCI_PORT = int(os.environ.get("LUCI_PORT", "8080"))
ADGUARD_PORT = int(os.environ.get("ADGUARD_PORT", "3000"))
WAN_INTERFACE = os.environ.get("WAN_INTERFACE", "wan")
POLL_SECONDS = min(60, max(5, int(os.environ.get("POLL_SECONDS", "5"))))
HEALTH_SECONDS = min(600, max(15, int(os.environ.get("HEALTH_SECONDS", "60"))))
TOP_N = int(os.environ.get("TOP_N", "10"))
PORT = int(os.environ.get("PORT", "3100"))

# WebSocket topic -> source name in the snapshot
WS_TOPICS = {"network_quality.status": "quality", "cable.status": "link", "vpnclient.status": "vpn"}


class AuthError(Exception):
    """The router rejected the credentials or the session."""


def post_json(url, payload, timeout=8):
    req = Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def router_password():
    value = os.environ.get("GL_PASS", "").rstrip("\r\n")
    if not value:
        raise AuthError("GL_PASS is not set")
    return value


def unix_crypt(secret, alg, salt):
    """crypt(3) hash as `openssl passwd -<alg>` computes it; the secret is passed on stdin, never in argv."""
    if alg not in ("1", "5", "6"):
        raise RuntimeError(f"unsupported crypt algorithm {alg!r}")
    result = subprocess.run(
        ["openssl", "passwd", "-" + alg, "-salt", salt, "-stdin"],
        input=secret, capture_output=True, text=True, check=True, timeout=10,
    )
    return result.stdout.strip()


class Session:
    """A router login shared by all threads.

    Failed logins back off exponentially (15 s .. 10 min) so a wrong password
    cannot trip the router's brute-force lockout.
    """

    name = "session"

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._failures = 0
        self._retry_at = 0.0

    def _login(self):
        raise NotImplementedError

    def token(self):
        with self._lock:
            if self._token:
                return self._token
            if time.monotonic() < self._retry_at:
                raise AuthError(f"{self.name} login paused after a failed attempt")
            try:
                self._token = self._login()
            except Exception:
                self._failures += 1
                self._retry_at = time.monotonic() + min(600, 15 * 2 ** (self._failures - 1))
                raise
            self._failures = 0
            LOG.info("%s login ok", self.name)
            return self._token

    def drop(self, token):
        with self._lock:
            if self._token == token:
                self._token = None

    def run(self, fn):
        """Call fn(token); if the session was rejected, log in again once and retry."""
        for attempt in (1, 2):
            token = self.token()
            try:
                return fn(token)
            except AuthError:
                self.drop(token)
                if attempt == 2:
                    raise


class GLSession(Session):
    """GL.iNet 4.x JSON-RPC session (challenge -> crypt -> digest -> login)."""

    name = "GL"

    def _rpc(self, method, params, timeout=8):
        resp = post_json(f"http://{GL_HOST}/rpc", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                         timeout=timeout)
        err = resp.get("error")
        if err:
            if err.get("message") == "Access denied":  # wrong hash or expired sid
                raise AuthError(f"GL {method}: access denied")
            raise RuntimeError(f"GL {method}: {err.get('message')} ({err.get('code')})")
        return resp.get("result")

    def _login(self):
        challenge = self._rpc("challenge", {"username": GL_USER})
        cipher = unix_crypt(router_password(), str(challenge["alg"]), challenge["salt"])
        digest = hashlib.new(challenge.get("hash-method", "md5"))
        digest.update(f"{GL_USER}:{cipher}:{challenge['nonce']}".encode())
        return self._rpc("login", {"username": GL_USER, "hash": digest.hexdigest()})["sid"]

    def call(self, module, method, params=None, timeout=8):
        return self.run(lambda sid: self._rpc("call", [sid, module, method, params or {}], timeout))

    def adguard(self, path, body=None):
        """AdGuard Home runs with --glinet: it accepts the GL session as the Admin-Token cookie.
        With a body the request is a JSON POST (AdGuard answers those with an empty body)."""
        def fetch(sid):
            headers = {"Cookie": "Admin-Token=" + sid}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            req = Request(f"http://{GL_HOST}:{ADGUARD_PORT}{path}", data=data, headers=headers)
            try:
                with urlopen(req, timeout=8) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw.strip().startswith((b"{", b"[")) else None
            except HTTPError as exc:
                if exc.code in (401, 403):
                    raise AuthError(f"AdGuard {path}: HTTP {exc.code}") from None
                raise
        return self.run(fetch)


class LuciSession(Session):
    """rpcd/ubus session via LuCI; only used for interface state and byte counters."""

    name = "LuCI"

    def _ubus(self, sid, obj, method, args=None):
        resp = post_json(f"http://{GL_HOST}:{LUCI_PORT}/ubus",
                         {"jsonrpc": "2.0", "id": 1, "method": "call", "params": [sid, obj, method, args or {}]})
        err = resp.get("error")
        if err:
            if err.get("code") == -32002:  # "Access denied": session expired
                raise AuthError(f"ubus {obj}.{method}: access denied")
            raise RuntimeError(f"ubus {obj}.{method}: {err.get('message')}")
        status, *data = resp["result"]
        if status == 6:  # UBUS_STATUS_PERMISSION_DENIED (e.g. wrong password on session.login)
            raise AuthError(f"ubus {obj}.{method}: permission denied")
        if status != 0:
            raise RuntimeError(f"ubus {obj}.{method}: status {status}")
        return data[0] if data else None

    def _login(self):
        result = self._ubus("0" * 32, "session", "login", {"username": GL_USER, "password": router_password()})
        return result["ubus_rpc_session"]

    def call(self, obj, method, args=None):
        return self.run(lambda sid: self._ubus(sid, obj, method, args))


class State:
    """Latest sample per source; on failure the last good data is kept and flagged with the error."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sources = {}
        self._logged = {}

    def ok(self, name, data):
        with self._lock:
            self._sources[name] = {"data": data, "updated": time.time(), "error": None}
            recovered = self._logged.pop(name, None)
        if recovered:
            LOG.info("%s recovered", name)

    def fail(self, name, exc):
        message = str(exc) or type(exc).__name__
        with self._lock:
            self._sources.setdefault(name, {"data": None, "updated": None})["error"] = message
            repeated = self._logged.get(name) == message
            self._logged[name] = message
        if not repeated:
            LOG.warning("%s: %s", name, message)

    def data(self, name):
        with self._lock:
            return (self._sources.get(name) or {}).get("data")

    def source(self, name):
        with self._lock:
            return dict(self._sources.get(name) or {})

    def snapshot(self):
        with self._lock:
            return json.dumps({"now": time.time(), "interval": POLL_SECONDS, "sources": self._sources})


def poll_router(gl):
    status = gl.call("system", "get_status")
    system = status.get("system") or {}
    wan = next((n for n in status.get("network") or [] if n.get("interface") == WAN_INTERFACE), {})
    total = system.get("memory_total") or 0
    free = (system.get("memory_free") or 0) + (system.get("memory_buff_cache") or 0)
    clients = (status.get("client") or [{}])[0]
    return {
        "uptime": system.get("uptime"),
        "load": system.get("load_average"),
        "cpu_temp": (system.get("cpu") or {}).get("temperature"),
        "memory_used": round(1 - free / total, 3) if total else None,
        "clients": (clients.get("cable_total") or 0) + (clients.get("wireless_total") or 0),
        "wan_online": bool(wan.get("up") and wan.get("online")),
        "netnat_enabled": system.get("netnat_enabled"),
        "tzoffset": system.get("tzoffset"),  # router local time offset, e.g. "+0200" (follows DST)
        "network_quality_enabled": system.get("network_quality_enabled"),
    }


def router_now():
    """Current router local time from its tzoffset (follows DST); container local time until the router answered."""
    offset = re.fullmatch(r"([+-])(\d\d)(\d\d)", str((STATE.data("router") or {}).get("tzoffset") or ""))
    if not offset:
        return datetime.now()
    seconds = (int(offset[2]) * 3600 + int(offset[3]) * 60) * (-1 if offset[1] == "-" else 1)
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(tzinfo=None)


class History:
    """Long-term history in data/history.json, shown in the page's History window.

    - speedtests: one entry per finished test (time, download/upload Mbps, ping, bufferbloat);
    - traffic: bytes received/sent per router-local day, summed from WAN counter deltas, so a
      PPPoE reconnect (counters back to 0) does not lose the day;
    - events: WAN down/up/reconnect and public IP changes.
    Written at most once a minute (plus right after a new speed test or event).
    """

    KEEP_TESTS, KEEP_DAYS, KEEP_EVENTS = 400, 400, 300
    FLUSH_SECONDS = 60

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._dirty = False
        self._flushed = 0.0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            LOG.warning("history unreadable, starting empty: %s", exc)
            data = {}
        self._tests = list(data.get("speedtests") or [])
        self._traffic = dict(data.get("traffic") or {})
        self._events = list(data.get("events") or [])
        self._last_ip = data.get("last_ip")

    def _save(self, force=False):
        """Caller holds the lock."""
        if not self._dirty or (not force and time.monotonic() - self._flushed < self.FLUSH_SECONDS):
            return
        days = sorted(self._traffic)[-self.KEEP_DAYS:]
        data = {"speedtests": self._tests[-self.KEEP_TESTS:], "traffic": {d: self._traffic[d] for d in days},
                "events": self._events[-self.KEEP_EVENTS:], "last_ip": self._last_ip}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.path)
            self._dirty, self._flushed = False, time.monotonic()
        except OSError as exc:
            LOG.warning("history not saved: %s", exc)

    def add_traffic(self, rx, tx):
        day = router_now().strftime("%Y-%m-%d")
        with self._lock:
            total = self._traffic.setdefault(day, [0, 0])
            total[0] += rx
            total[1] += tx
            self._dirty = True
            self._save()

    def add_speedtest(self, finished_at, test):
        entry = {"t": round(finished_at), "down": round(float(test.get("download") or 0), 1),
                 "up": round(float(test.get("upload") or 0), 1), "ping": test.get("ping"),
                 "bufferbloat": test.get("bufferbloat")}
        with self._lock:
            self._tests.append(entry)
            self._dirty = True
            self._save(force=True)

    def last_speedtest(self):
        with self._lock:
            return dict(self._tests[-1]) if self._tests else None

    def recent(self, kinds, seconds):
        """Newest event of one of these kinds from the last `seconds`, or None."""
        since = time.time() - seconds
        with self._lock:
            return next((dict(e) for e in reversed(self._events) if e["t"] >= since and e.get("kind") in kinds), None)

    def event(self, kind, text):
        with self._lock:
            self._events.append({"t": round(time.time()), "kind": kind, "text": text})
            self._dirty = True
            self._save(force=True)
        LOG.info("history event: %s", text)

    def observe_ip(self, ip):
        if not ip:
            return
        with self._lock:
            previous, self._last_ip = self._last_ip, ip
            if previous == ip:
                return
            self._dirty = True
        if previous:
            self.event("ip", f"Public IP changed: {previous} → {ip}")
        else:
            with self._lock:
                self._save(force=True)

    def snapshot(self):
        with self._lock:
            days = sorted(self._traffic)[-62:]
            return {"speedtests": self._tests[-120:], "traffic": [[d, *self._traffic[d]] for d in days],
                    "events": self._events[-60:][::-1], "today": router_now().strftime("%Y-%m-%d")}


class WanCounters:
    """WAN byte counters from the L3 device (pppoe-wan for PPPoE); rates are derived between polls.
    Also feeds the history: bytes per day and WAN down/up/reconnect events."""

    def __init__(self):
        self._prev = None
        self._link = None  # (up, uptime, time) of the previous poll, for down/up/reconnect events
        self._down_since = None

    def poll(self, luci):
        interfaces = luci.call("network.interface", "dump")["interface"]
        iface = next((i for i in interfaces if i.get("interface") == WAN_INTERFACE), None)
        if iface is None:
            raise RuntimeError(f"interface {WAN_INTERFACE!r} not found")
        device = iface.get("l3_device") or iface.get("device")
        stats = ((luci.call("luci-rpc", "getNetworkDevices") or {}).get(device) or {}).get("stats") or {}
        rx, tx, now = stats.get("rx_bytes"), stats.get("tx_bytes"), time.monotonic()
        rx_rate = tx_rate = None
        prev = self._prev
        # Counters restart when PPPoE reconnects; skip the rate for that interval.
        if prev and prev[0] == device and rx is not None and rx >= prev[1] and tx >= prev[2]:
            elapsed = now - prev[3]
            rx_rate, tx_rate = (rx - prev[1]) / elapsed, (tx - prev[2]) / elapsed
        # Daily totals: a counter that went backwards restarted from 0 (reconnect), so count from 0
        if prev and rx is not None and tx is not None and prev[1] is not None:
            same = prev[0] == device and rx >= prev[1] and tx >= prev[2]
            HISTORY.add_traffic(rx - prev[1] if same else rx, tx - prev[2] if same else tx)
        self._prev = (device, rx, tx, now) if rx is not None else None
        self._track_link(bool(iface.get("up")), iface.get("uptime"))
        return {
            "interface": WAN_INTERFACE,
            "device": device,
            "proto": iface.get("proto"),
            "up": iface.get("up"),
            "uptime": iface.get("uptime"),
            "rx_bytes": rx,
            "tx_bytes": tx,
            "rx_rate": rx_rate,
            "tx_rate": tx_rate,
        }

    def _track_link(self, up, uptime):
        """WAN events for the history: down, back up (with how long it was down), silent reconnect."""
        previous, self._link = self._link, (up, uptime, time.time())
        if previous is None:
            return
        was_up, was_uptime, was_at = previous
        if was_up and not up:
            self._down_since = was_at
            HISTORY.event("down", "WAN went down")
        elif not was_up and up:
            since = self._down_since
            HISTORY.event("up", f"WAN is back up (down for {_duration(time.time() - since)})" if since else "WAN is back up")
        elif up and uptime is not None and was_uptime is not None and uptime < was_uptime:
            HISTORY.event("reconnect", f"WAN reconnected (was up {_duration(was_uptime)})")


def _duration(seconds):
    """Human duration for event texts: "6 d 22 h", "3 h 5 min", "1 min 2 s", "40 s"."""
    seconds = int(seconds)
    if seconds >= 86400:
        return f"{seconds // 86400} d {seconds % 86400 // 3600} h"
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


def poll_dns(gl):
    stats = gl.adguard("/control/stats")
    status = gl.adguard("/control/status")

    def top(key):
        return [[name, count] for item in (stats.get(key) or [])[:TOP_N] for name, count in item.items()]

    avg_times = {name: t for item in stats.get("top_upstreams_avg_time") or [] for name, t in item.items()}
    return {
        "running": status.get("running"),
        "protection_enabled": status.get("protection_enabled"),
        # Remaining pause in ms when protection was paused for a while (0 = off until turned on again)
        "protection_paused_ms": status.get("protection_disabled_duration") or 0,
        "version": status.get("version"),
        "time_units": stats.get("time_units"),
        "queries": stats.get("num_dns_queries"),
        "blocked": stats.get("num_blocked_filtering"),
        "avg_ms": round((stats.get("avg_processing_time") or 0) * 1000, 2),
        "queries_series": stats.get("dns_queries") or [],
        "blocked_series": stats.get("blocked_filtering") or [],
        "top_queried": top("top_queried_domains"),
        "top_blocked": top("top_blocked_domains"),
        "upstreams": [
            {"name": name, "responses": count, "avg_ms": round(avg_times.get(name, 0) * 1000, 1)}
            for item in stats.get("top_upstreams_responses") or [] for name, count in item.items()
        ],
    }


def _ago(seconds):
    return "just now" if seconds < 60 else f"{_duration(seconds)} ago"


def compute_alerts(state):
    """Warnings and critical conditions shown next to "Live" on the page, most severe first.

    [{"id", "level": "warning"|"critical", "text"}]; thresholds come from alerts: in services.yaml.
    """
    cfg = (SITE.get()["config"] or {}).get("alerts") or normalize_alerts({})
    alerts = []

    def check(key, value, text):
        levels = cfg[key]
        for level in ("critical", "warning"):
            if value is not None and levels[level] is not None and value >= levels[level]:
                alerts.append({"id": key, "level": level, "text": text})
                return

    router = state.source("router")
    stale = not router.get("updated") or time.time() - router["updated"] > POLL_SECONDS * 3
    if router.get("error") or stale:
        if cfg["router_unreachable"]:
            alerts.append({"id": "router_unreachable", "level": "critical",
                           "text": f"No data from the router: {router.get('error') or 'no answer'}"})
    else:
        r = router.get("data") or {}
        if cfg["wan_down"] and r.get("wan_online") is False:
            alerts.append({"id": "wan_down", "level": "critical", "text": "Internet is down: the WAN is offline"})
        check("cpu_temp", r.get("cpu_temp"), f"Router CPU is hot: {r.get('cpu_temp')} °C")
        if r.get("memory_used") is not None:
            check("memory", r["memory_used"] * 100, f"Router memory {round(r['memory_used'] * 100)} % used")
        load = r.get("load") or []
        if len(load) > 1:
            check("load", load[1], f"Router load {load[1]:.2f} (5 min average)")
        minutes = cfg["router_restart_minutes"]
        if minutes and r.get("uptime") is not None and r["uptime"] < minutes * 60:
            alerts.append({"id": "router_restart", "level": "warning", "text": f"Router restarted {_ago(r['uptime'])}"})
    minutes = cfg["wan_event_minutes"]
    event = minutes and HISTORY.recent(("reconnect", "up", "ip"), minutes * 60)
    if event and not any(a["id"] == "wan_down" for a in alerts):
        alerts.append({"id": "wan_event", "level": "warning", "text": f"{event['text']} · {_ago(time.time() - event['t'])}"})
    speed, test = cfg["speedtest"], HISTORY.last_speedtest()
    if test:
        # The worse of download/upload as % of the plan decides the level; shown until the next test
        shares = [(test[k] / speed[key] * 100, key, test[k]) for k, key in (("down", "download"), ("up", "upload"))
                  if speed[key] and test.get(k) is not None]
        if shares:
            share, key, mbps = min(shares)
            for level in ("critical", "warning"):
                if speed[level] is not None and share < speed[level]:
                    alerts.append({"id": "speedtest", "level": level,
                                   "text": f"Slow speed test: {key} {round(mbps)} Mbps, {round(share)} % of "
                                           f"{speed[key]:g} Mbps ({_ago(time.time() - test['t'])})"})
                    break
    dns = state.source("dns")
    if dns.get("data") and not dns.get("error"):
        d = dns["data"]
        check("dns_avg_ms", d.get("avg_ms"), f"Slow DNS: {d.get('avg_ms')} ms average")
        for up in d.get("upstreams") or []:
            check("dns_upstream_ms", up.get("avg_ms"), f"Slow DNS upstream {up['name'].removesuffix(':53')}: {up.get('avg_ms')} ms")
    alerts.sort(key=lambda a: a["level"] != "critical")
    return alerts


def poll_loop(gl, luci, state):
    wan = WanCounters()
    jobs = (("router", lambda: poll_router(gl)), ("wan", lambda: wan.poll(luci)), ("dns", lambda: poll_dns(gl)),
            ("clients", lambda: DEVICES.poll(gl)))
    while True:
        started = time.monotonic()
        for name, job in jobs:
            try:
                state.ok(name, job())
            except Exception as exc:  # keep polling whatever a single source does
                state.fail(name, exc)
        try:
            state.ok("alerts", compute_alerts(state))
        except Exception as exc:
            state.fail("alerts", exc)
        time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))


def stream_loop(gl, state):
    """Subscribe to the GL UI WebSocket; the router pushes network_quality.status every second.

    The router's stream sometimes goes silent without closing (seen every ~15 minutes), so:
    - silence longer than STALL_SECONDS counts as a stall and we reconnect at once
      (well inside the page's 15 s staleness threshold);
    - a ping every PING_SECONDS keeps the connection from looking idle;
    - sources are only marked as failed when reconnecting fails, not on a single stall.
    """
    STALL_SECONDS, QUIET_STALL_SECONDS, PING_SECONDS = 5, 60, 10
    delay, failures = 2, 0
    while True:
        try:
            sid = gl.token()
            try:
                ws = websocket.create_connection(f"ws://{GL_HOST}/ws?sid={sid}", timeout=10)
            except websocket.WebSocketBadStatusException as exc:
                if exc.status_code == 401:
                    gl.drop(sid)
                raise
            try:
                for topic in WS_TOPICS:
                    ws.send(json.dumps({"cmd": "subscribe", "name": topic}))
                # Without Network Quality the router pushes nothing: only cable.status changes arrive
                quiet = (state.data("router") or {}).get("network_quality_enabled") is False
                ws.settimeout(QUIET_STALL_SECONDS if quiet else STALL_SECONDS)
                last_ping = time.monotonic()
                while True:
                    message = json.loads(ws.recv())
                    if failures:  # data flows again: the failure streak is over
                        if failures > 2:
                            LOG.info("websocket recovered after %d failed attempt(s)", failures)
                        delay, failures = 2, 0
                    name = WS_TOPICS.get(message.get("name"))
                    if name == "quality":
                        SPEEDTEST_TIMES.observe(message.get("data"))
                    elif name == "link":
                        ip = str(((message.get("data") or {}).get("ipv4") or {}).get("ip") or "").split("/")[0]
                        HISTORY.observe_ip(ip)
                    if name:
                        state.ok(name, message.get("data"))
                    if time.monotonic() - last_ping > PING_SECONDS:
                        ws.ping()
                        last_ping = time.monotonic()
            finally:
                ws.close()
        except websocket.WebSocketTimeoutException:
            # Silent stream: reconnect right away; the data only turns stale if that fails too
            LOG.debug("websocket stalled, reconnecting")
            failures += 1
            if failures > 2:
                for name in WS_TOPICS.values():
                    state.fail(name, "websocket: the router stopped sending data")
                time.sleep(delay)
                delay = min(60, delay * 2)
        except Exception as exc:  # connection refused, auth error, closed by the router ...
            failures += 1
            if failures > 1:
                for name in WS_TOPICS.values():
                    state.fail(name, f"websocket: {exc}")
            time.sleep(delay)
            delay = min(60, delay * 2)


class Conflict(Exception):
    """The request is valid but cannot run right now."""


class Speedtest:
    """Starts or stops the router speed test: the same call as the button on the GL network quality page.

    This is the only non-read call the collector makes; it runs a test and changes no settings.
    Starts are limited to one per cooldown so a stuck page or double click cannot flood the line.
    """

    COOLDOWN = 60

    def __init__(self, gl, state):
        self._gl = gl
        self._state = state
        self._lock = threading.Lock()
        self._last_start = 0.0

    def set(self, enable):
        running = ((self._state.data("quality") or {}).get("speedtest") or {}).get("state") == "running"
        if enable:
            if running:
                raise Conflict("a speed test is already running")
            with self._lock:
                wait = self._last_start + self.COOLDOWN - time.monotonic()
                if wait > 0:
                    raise Conflict(f"a speed test was started recently, try again in {wait:.0f} s")
                self._last_start = time.monotonic()
        try:
            # The GL UI allows this call up to 60 s
            result = self._gl.call("network-quality", "set_speedtest", {"enable": enable}, timeout=60)
        except Exception:
            if enable:
                with self._lock:
                    self._last_start = 0.0  # a failed start should not block a retry
            raise
        if enable:
            SPEEDTEST_TIMES.started()
        LOG.info("speed test %s", "started" if enable else "stopped")
        return result


def get_json(url, timeout=8):
    with urlopen(Request(url, headers={"User-Agent": "glinet-dashboard/1.0", "Accept": "application/json"}), timeout=timeout) as resp:
        return json.load(resp)


def zenquotes():
    # Free tier: attribution with a link to zenquotes.io is required (shown in the page footer)
    item = get_json("https://zenquotes.io/api/today")[0]
    return {"text": item["q"], "author": item.get("a") or "", "source": "ZenQuotes API", "link": "https://zenquotes.io/"}


def favqs():
    item = get_json("https://favqs.com/api/qotd")["quote"]
    return {"text": item["body"], "author": item.get("author") or "", "source": "FavQs", "link": item.get("url") or "https://favqs.com/"}


class QuoteOfTheDay:
    """Quote of the day from an online provider, cached on disk.

    One quote per UTC day (the providers' day). Every fetched quote is kept in the cache file,
    so a provider outage or a container restart still shows the latest cached quote.
    """

    PROVIDERS = (zenquotes, favqs)
    CHECK_SECONDS = 600       # how often the background thread checks for a new day / retries
    KEEP_DAYS = 400

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._days = self._load()

    def _load(self):
        try:
            days = json.loads(self.path.read_text(encoding="utf-8")).get("days") or {}
            LOG.info("quote cache: %d days in %s", len(days), self.path)
            return days
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            LOG.warning("quote cache unreadable, starting empty: %s", exc)
            return {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"days": self._days}, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:  # keep working from memory if the volume is not writable
            LOG.warning("quote cache not saved: %s", exc)

    @staticmethod
    def today():
        return time.strftime("%Y-%m-%d", time.gmtime())

    def refresh(self):
        """Fetch today's quote unless it is already cached; providers are tried in order."""
        day = self.today()
        with self._lock:
            if day in self._days:
                return
        errors = []
        for provider in self.PROVIDERS:
            try:
                quote = provider()
                if not str(quote["text"]).strip():
                    raise ValueError("empty quote")
            except Exception as exc:  # network error, rate limit, changed format
                errors.append(f"{provider.__name__}: {exc}")
                continue
            with self._lock:
                self._days[day] = quote
                self._days = dict(sorted(self._days.items())[-self.KEEP_DAYS:])
                self._save()
            LOG.info("quote of %s from %s", day, quote["source"])
            return
        LOG.warning("quote of %s not fetched (showing the cached one): %s", day, "; ".join(errors))

    def current(self):
        with self._lock:
            if not self._days:
                return {"quote": None, "fallback": False}
            day = max(self._days)
            return {"quote": {**self._days[day], "date": day}, "fallback": day != self.today()}

    def loop(self):
        while True:
            self.refresh()
            time.sleep(self.CHECK_SECONDS)


class SpeedtestTimes:
    """Remembers when the router's last speed test finished: the router only reports the results.

    A test counts as finished when the state goes from running to success while we watch.
    A result that appeared while the collector was down gets finished_at = None (time unknown).
    The value is added to the quality data as speedtest.finished_at and kept on disk.
    """

    def __init__(self, path):
        self.path = path
        self._saw_running = False
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            self._key, self._finished = saved.get("key"), saved.get("finished_at")
        except (OSError, ValueError):
            self._key, self._finished = None, None

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"key": self._key, "finished_at": self._finished}), encoding="utf-8")
        except OSError as exc:
            LOG.warning("speed test time not saved: %s", exc)

    def started(self):
        """We started a test ourselves: count it even if the stream misses the running state."""
        self._saw_running = True

    def observe(self, data):
        test = (data or {}).get("speedtest")
        if not isinstance(test, dict):
            return
        state = test.get("state")
        if state == "running":
            self._saw_running = True
        elif state == "success" and test.get("download") is not None:
            key = [round(float(test.get("download") or 0), 1), round(float(test.get("upload") or 0), 1), test.get("bufferbloat")]
            if self._saw_running:
                self._key, self._finished = key, time.time()
                self._save()
                HISTORY.add_speedtest(self._finished, test)
            elif key != self._key:
                self._key, self._finished = key, None
                self._save()
            self._saw_running = False
        elif state == "failed":
            self._saw_running = False
        test["finished_at"] = self._finished


STATE = State()
GL = GLSession()
LUCI = LuciSession()
SPEEDTEST = Speedtest(GL, STATE)


class WanReconnect:
    """Re-dials the WAN, the same as the "Restart" button of an interface in LuCI.

    LuCI runs /sbin/ifup <interface> through ubus file.exec (its ACL allows exactly that);
    on an interface that is up, ifup takes it down and brings it back, so PPPoE dials again
    and usually receives a new IP. No settings change. Internet drops for a few seconds,
    so the page asks for confirmation and one reconnect per COOLDOWN is allowed.
    """

    COOLDOWN = 60

    def __init__(self, luci, state):
        self._luci = luci
        self._state = state
        self._lock = threading.Lock()
        self._last = 0.0

    def run(self):
        if not re.fullmatch(r"[\w.-]+", WAN_INTERFACE):
            raise ValueError(f"invalid WAN_INTERFACE {WAN_INTERFACE!r}")
        with self._lock:
            wait = self._last + self.COOLDOWN - time.monotonic()
            if wait > 0:
                raise Conflict(f"the WAN was reconnected a moment ago, try again in {wait:.0f} s")
            self._last = time.monotonic()
        ipv4 = (self._state.data("link") or {}).get("ipv4") or {}
        previous = str(ipv4.get("ip") or "").split("/")[0] or None
        try:
            result = self._luci.call("file", "exec", {"command": "/sbin/ifup", "params": [WAN_INTERFACE]}) or {}
        except Exception:
            with self._lock:
                self._last = 0.0  # a failed call should not block a retry
            raise
        if result.get("code") not in (None, 0):
            raise RuntimeError(f"ifup {WAN_INTERFACE} failed: {(result.get('stderr') or '').strip() or result.get('code')}")
        LOG.info("WAN reconnect: ifup %s (IP before: %s)", WAN_INTERFACE, previous or "unknown")
        return {"previous_ip": previous, "started": time.time()}
QUOTES = QuoteOfTheDay(Path(os.environ.get("QUOTE_CACHE", ROOT / "data" / "quotes.json")))
SPEEDTEST_TIMES = SpeedtestTimes(ROOT / "data" / "speedtest.json")
HISTORY = History(ROOT / "data" / "history.json")


class VpnTunnels:
    """Turns VPN client tunnels on or off, the same call as the power switch on the GL VPN Dashboard:
    vpn-client.set_tunnel {tunnel_id, enabled}. The tunnel must exist and, to be turned on, have a
    VPN server selected (as the GL UI requires). One switch per MIN_INTERVAL."""

    MIN_INTERVAL = 5

    def __init__(self):
        self._lock = threading.Lock()
        self._last = 0.0

    def set(self, tunnel_id, enabled):
        with self._lock:
            if time.monotonic() - self._last < self.MIN_INTERVAL:
                raise Conflict("a VPN tunnel was switched a moment ago, try again in a few seconds")
            self._last = time.monotonic()
        tunnels = (GL.call("vpn-client", "get_tunnel") or {}).get("tunnels") or []
        tunnel = next((t for t in tunnels if t.get("tunnel_id") == tunnel_id), None)
        if tunnel is None:
            raise ValueError(f"no VPN tunnel with id {tunnel_id}")
        via = tunnel.get("via") or {}
        configs = via.get("configs") or []
        if enabled and via.get("type") != "novpn" and not (configs and (configs[0] or {}).get("id_list")):
            raise Conflict("this tunnel has no VPN server selected yet; choose one on the router's VPN Dashboard")
        GL.call("vpn-client", "set_tunnel", {"tunnel_id": tunnel_id, "enabled": enabled}, timeout=30)
        LOG.info("VPN tunnel %s turned %s", tunnel.get("name"), "on" if enabled else "off")
        HISTORY.event("vpn", f"VPN {tunnel.get('name')} turned {'on' if enabled else 'off'} from the dashboard")
        return {"name": tunnel.get("name")}


VPN = VpnTunnels()


class VpnExit:
    """External IP seen through a connected VPN tunnel; the router does not report it.

    The collector asks an IP service from its own host (ifconfig.co, ipinfo.io as backup), so the
    answer is the tunnel's exit only when this host's traffic goes through the tunnel. It is compared
    with the WAN IP and with the IP seen while no tunnel is on (baseline); when it matches either,
    the result is flagged as not routed instead of being shown as the VPN address.
    """

    CHECK_SECONDS, REFRESH_SECONDS, BASELINE_SECONDS, SETTLE_SECONDS = 15, 600, 1800, 3

    def __init__(self, state):
        self._state = state
        self._baseline, self._baseline_at = None, -1e9
        self._key, self._checked = None, -1e9

    @staticmethod
    def lookup():
        errors = []
        for url, country_field in (("https://ifconfig.co/json", "country"), ("https://ipinfo.io/json", "country")):
            try:
                data = get_json(url, timeout=6)
                return {"ip": data["ip"], "city": data.get("city") or "", "country": data.get(country_field) or ""}
            except Exception as exc:  # rate limit, network, changed format: try the next service
                errors.append(f"{url.split('/')[2]}: {exc}")
        raise RuntimeError("IP lookup failed: " + "; ".join(errors))

    def check(self):
        status = self._state.data("vpn")
        if status is None:
            return  # the router has not reported its tunnels yet: do not take a baseline blindly
        tunnels = status.get("status_list") or []
        now = time.monotonic()
        if not any(t.get("enabled") for t in tunnels):
            if self._key is not None:
                self._key = None
                self._state.ok("vpn_exit", None)
            if now - self._baseline_at > self.BASELINE_SECONDS:
                self._baseline_at = now
                try:
                    self._baseline = self.lookup()["ip"]
                except RuntimeError as exc:
                    LOG.debug("baseline IP: %s", exc)
            return
        key = tuple(sorted(t["tunnel_id"] for t in tunnels if t.get("enabled") and t.get("status") == 1))
        if not key:
            return  # still connecting
        if key == self._key and now - self._checked < self.REFRESH_SECONDS:
            return
        if key != self._key:
            time.sleep(self.SETTLE_SECONDS)  # let the router finish switching routes
        found = self.lookup()
        wan_ip = str(((self._state.data("link") or {}).get("ipv4") or {}).get("ip") or "").split("/")[0]
        found.update(tunnels=list(key), checked=time.time(), routed=found["ip"] not in (wan_ip, self._baseline))
        self._key, self._checked = key, now
        self._state.ok("vpn_exit", found)
        LOG.info("VPN exit IP: %s (%s, %s)%s", found["ip"], found["city"], found["country"],
                 "" if found["routed"] else " - this host does not use the tunnel")

    def loop(self):
        while True:
            try:
                self.check()
            except Exception as exc:  # lookup failed: keep the last result, retry soon
                self._state.fail("vpn_exit", exc)
            time.sleep(self.CHECK_SECONDS)


VPN_EXIT = VpnExit(STATE)


class Devices:
    """Router clients (the GL UI's Clients page): current speed and traffic per device, and new devices.

    rx/tx are the device's download/upload in bytes/s, total_rx/total_tx its traffic as counted by
    the router. Every MAC seen is remembered in data/devices.json; one that was never seen before
    adds a "New device" event to the history and is flagged as new for NEW_SECONDS. On the very
    first run the devices already present are just remembered, not reported.
    """

    NEW_SECONDS = 86400

    def __init__(self, path):
        self.path = path
        try:
            self._known = dict(json.loads(path.read_text(encoding="utf-8")).get("known") or {})
        except (OSError, ValueError):
            self._known = {}
        self._seeded = bool(self._known)

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"known": self._known}), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            LOG.warning("device list not saved: %s", exc)

    def poll(self, gl):
        clients = (gl.call("clients", "get_list") or {}).get("clients") or []
        now, changed, devices = time.time(), False, []
        for c in clients:
            mac = str(c.get("mac") or "").upper()
            if not mac:
                continue
            name = str(c.get("alias") or c.get("name") or mac)
            if mac not in self._known:
                self._known[mac] = now if self._seeded else 0  # 0: already here when tracking started
                changed = True
                if self._seeded:
                    HISTORY.event("device", f"New device: {name} ({c.get('ip') or 'no IP'}, {mac})")
            online = bool(c.get("online"))
            first_seen = self._known[mac]
            devices.append({
                "mac": mac, "name": name, "ip": c.get("ip") or "", "iface": c.get("iface") or "",
                "class": c.get("class") or "", "online": online,
                "down": (c.get("rx") or 0) if online else 0, "up": (c.get("tx") or 0) if online else 0,
                "total_down": int(c.get("total_rx") or 0), "total_up": int(c.get("total_tx") or 0),
                "since": c.get("online_time") if online else None,
                "new": bool(first_seen) and now - first_seen < self.NEW_SECONDS,
            })
        if not self._seeded:
            self._seeded = True
            LOG.info("device list: remembered %d devices already in the network", len(self._known))
        if changed:
            self._save()
        devices.sort(key=lambda d: (not d["online"], -d["down"], d["name"].lower()))
        return {"devices": devices}


DEVICES = Devices(ROOT / "data" / "devices.json")
WAN_RECONNECT = WanReconnect(LUCI, STATE)


class HealthChecks:
    """Status dots on the service tiles: a TCP connect to each tile's check target every HEALTH_SECONDS.

    A failed connect is retried once after a second, so a single dropped packet does not turn a
    tile red. Results are keyed by target and keep the time of the last up/down change.
    """

    TIMEOUT = 3

    def __init__(self, state):
        self._state = state
        self._last = {}

    @classmethod
    def probe(cls, target):
        host, port = target.rsplit(":", 1)
        error = None
        for attempt in (1, 2):
            started = time.monotonic()
            try:
                with socket.create_connection((host, int(port)), timeout=cls.TIMEOUT):
                    return True, round((time.monotonic() - started) * 1000), None
            except OSError as exc:
                error = exc.strerror or str(exc) or type(exc).__name__
                if attempt == 1:
                    time.sleep(1)
        return False, None, error

    def run_once(self):
        config = SITE.get()["config"]
        if not config:
            return
        targets = sorted({svc["check"] for group in config["groups"] for svc in group["services"] if svc["check"]})
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = dict(zip(targets, pool.map(self.probe, targets)))
        now, current = time.time(), {}
        for target, (up, ms, error) in results.items():
            previous = self._last.get(target)
            since = previous["since"] if previous and previous["up"] == up else now
            if previous and previous["up"] != up:
                LOG.info("service %s is %s%s", target, "up" if up else "down", "" if up else f" ({error})")
            current[target] = {"up": up, "ms": ms, "error": error, "since": since}
        self._last = current
        self._state.ok("health", current)

    def loop(self):
        while True:
            try:
                self.run_once()
            except Exception as exc:  # never let the checker thread die
                self._state.fail("health", exc)
            time.sleep(HEALTH_SECONDS)


HEALTH = HealthChecks(STATE)


class NoRedirect(HTTPRedirectHandler):
    """Lets us read the Location header instead of downloading the full-size original."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Wallpaper:
    """Background set from the page by pasting an Unsplash photo link.

    Only unsplash.com photo pages are accepted, so the collector never fetches arbitrary URLs.
    The photo is downloaded (resized to 2560 px) into data/wallpapers/ and served from there.
    With UNSPLASH_ACCESS_KEY set, the Unsplash API supplies the exact author name and the photo
    location; without a key (or if the API fails, e.g. rate limit) the author is taken from
    Unsplash's download file name ("<author>-<id>-unsplash.jpg") and there is no location.
    It overrides page.background.image from the config until it is reset.
    """

    WIDTH = 3840              # standard 4K width, independent of the screen that set it
    MAX_BYTES = 25 * 1024 * 1024
    MIN_INTERVAL = 10
    UA = "Mozilla/5.0 (compatible; glinet-dashboard/1.0)"

    def __init__(self, folder):
        self.folder = folder
        self.state_path = folder.parent / "background.json"
        self._lock = threading.Lock()
        self._last_change = 0.0
        try:
            self._current = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not (self.folder / self._current["file"]).is_file():
                self._current = None
        except (OSError, ValueError, KeyError, TypeError):
            self._current = None

    def current(self):
        with self._lock:
            return dict(self._current) if self._current else None

    @staticmethod
    def photo_id(url):
        """Photo id from any unsplash.com photo page link, including localized ones (/de/fotos/...)."""
        parts = urlsplit(str(url).strip())
        if parts.scheme != "https" or parts.hostname not in ("unsplash.com", "www.unsplash.com"):
            raise ValueError("paste the link of an Unsplash photo page, e.g. https://unsplash.com/photos/...")
        segment = parts.path.rstrip("/").rsplit("/", 1)[-1]
        # Photo ids are 11 characters; page links append them to a slug: "snow-covered-mountain-ooxzy4JN6gw"
        if len(segment) >= 11 and re.fullmatch(r"[A-Za-z0-9_-]{11}", segment[-11:]) and (len(segment) == 11 or segment[-12] == "-"):
            return segment[-11:]
        raise ValueError("this does not look like an Unsplash photo link (https://unsplash.com/photos/...)")

    def set(self, url, location=""):
        photo = self.photo_id(url)
        with self._lock:
            if time.monotonic() - self._last_change < self.MIN_INTERVAL:
                raise Conflict("the wallpaper was changed a moment ago, try again in a few seconds")
            self._last_change = time.monotonic()
        details = None
        key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
        if key:
            try:
                details = self._from_api(photo, key)
            except ValueError:
                raise
            except Exception as exc:  # rate limit, bad key, network: the keyless way still works
                LOG.warning("Unsplash API: %s; falling back to the download link", exc)
        if details is None:
            details = self._from_download_link(photo)
        author, api_location, image_url = details
        location = location or api_location[:80]
        with urlopen(Request(image_url, headers={"User-Agent": self.UA}), timeout=30) as resp:
            if not resp.headers.get("Content-Type", "").startswith("image/"):
                raise RuntimeError("the Unsplash CDN did not return an image")
            data = resp.read(self.MAX_BYTES + 1)
        if len(data) > self.MAX_BYTES:
            raise RuntimeError("the image is larger than 25 MB")
        self.folder.mkdir(parents=True, exist_ok=True)
        name = f"{photo}.jpg"
        (self.folder / name).write_bytes(data)
        state = {"file": name, "id": photo, "set_at": time.time(), "credit": author,
                 "credit_url": f"https://unsplash.com/photos/{photo}", "location": clean_location(location)}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.state_path)
        with self._lock:
            self._current = state
        self._cleanup(keep=name)
        LOG.info("wallpaper set from the page: Unsplash %s by %s (%d KB)", photo, author or "unknown", len(data) // 1024)
        return state

    def _from_api(self, photo, key):
        """(author, location, image URL) from api.unsplash.com; ValueError if the photo does not exist."""
        headers = {"Authorization": f"Client-ID {key}", "Accept-Version": "v1", "User-Agent": self.UA}
        try:
            with urlopen(Request(f"https://api.unsplash.com/photos/{photo}", headers=headers), timeout=15) as resp:
                data = json.load(resp)
        except HTTPError as exc:
            if exc.code == 404:
                raise ValueError("this photo does not exist on Unsplash") from None
            raise RuntimeError(f"HTTP {exc.code} {exc.read(200).decode(errors='replace').strip()}") from None
        if data.get("premium") or data.get("plus"):
            raise ValueError("this is an Unsplash+ photo, which is not free to download")
        place = data.get("location") or {}
        location = place.get("name") or ", ".join(p for p in (place.get("city"), place.get("country")) if p)
        # Counts the download for the photographer, as Unsplash asks; failures do not matter
        try:
            urlopen(Request(data["links"]["download_location"], headers=headers), timeout=10).close()
        except Exception:
            pass
        return (data.get("user") or {}).get("name") or "", location or "", self._sized(data["urls"]["raw"])

    def _from_download_link(self, photo):
        """(author, "", image URL) without an API key: the download endpoint redirects to the image CDN
        and its dl= parameter names the author ("marek-piwnicki-<id>-unsplash.jpg")."""
        try:
            build_opener(NoRedirect).open(Request(f"https://unsplash.com/photos/{photo}/download?force=true",
                                                  headers={"User-Agent": self.UA}), timeout=15)
            raise RuntimeError("Unsplash did not return the photo")
        except HTTPError as exc:
            location = exc.headers.get("Location") if exc.code in (301, 302, 303, 307, 308) else None
            if not location:
                raise RuntimeError(f"Unsplash refused the download (HTTP {exc.code}); Unsplash+ photos are not free") from None
        cdn = urlsplit(location)
        if cdn.scheme != "https" or cdn.hostname != "images.unsplash.com":
            raise RuntimeError("Unsplash did not return a free photo download (Unsplash+ photos need a subscription)")
        match = re.fullmatch(rf"(.+)-{re.escape(photo)}-unsplash\.jpg", parse_qs(cdn.query).get("dl", [""])[0])
        author = " ".join(word.capitalize() for word in match[1].split("-")) if match else ""
        return author, "", self._sized(location)

    def _sized(self, url):
        """The image CDN URL resized to WIDTH px as JPEG (Unsplash's imgix parameters)."""
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "images.unsplash.com":
            raise RuntimeError("Unsplash returned an unexpected image address")
        query = {k: v[0] for k, v in parse_qs(parts.query).items() if k != "dl"}
        query.update(w=str(self.WIDTH), q="80", fm="jpg")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    def set_location(self, location):
        """Change only the location of the current page wallpaper."""
        with self._lock:
            if not self._current:
                raise Conflict("no wallpaper set from the page: paste a photo link first")
            self._current = {**self._current, "location": clean_location(location)}
            state = dict(self._current)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.state_path)
        return state

    def reset(self):
        with self._lock:
            self._current = None
        self.state_path.unlink(missing_ok=True)
        self._cleanup(keep=None)
        LOG.info("wallpaper reset to the config background")

    def _cleanup(self, keep):
        for old in self.folder.glob("*.jpg") if self.folder.is_dir() else []:
            if old.name != keep:
                old.unlink(missing_ok=True)


def clean_location(value):
    value = " ".join(str(value or "").split())
    if len(value) > 80:
        raise ValueError("the location is too long (80 characters max)")
    return value


WALLPAPER = Wallpaper(ROOT / "data" / "wallpapers")


class WebRedirect(HTTPRedirectHandler):
    """Follows redirects for link previews, but only to http(s): never file:, ftp: or data:."""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme not in ("http", "https"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PageHead(HTMLParser):
    """Collects <title>, og:site_name, <meta charset> and icon <link>s from a page's <head>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.site_name, self.icons, self._in_title, self.done = "", "", [], False, False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta" and a.get("property", a.get("name", "")).lower() == "og:site_name":
            self.site_name = a.get("content", "")
        elif tag == "link" and a.get("href"):
            rels = a.get("rel", "").lower().split()
            if "icon" in rels or "apple-touch-icon" in rels or "apple-touch-icon-precomposed" in rels:
                self.icons.append(a)
        elif tag == "body":
            self.done = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data):
        if self._in_title and not self.title_done():
            self.title += data

    def title_done(self):
        return len(self.title) > 300


class Bookmarks:
    """Quick links under the search box, edited on the page and kept in data/bookmarks.json.

    A link is {"id", "url", "name", "title", "icon", "state"}: name is what was typed on the page
    (may be empty), title the page's own name and icon its favicon cached in data/favicons/. Both are
    fetched in the background when a link is added or its URL changes ("state": "pending" until then);
    a link whose page could not be read is retried after RETRY_SECONDS. Only http(s) URLs are fetched,
    redirects included, and only the start of the page and a small image are read.
    """

    MAX_LINKS = 40
    MAX_PAGE = 512 * 1024
    MAX_ICON = 256 * 1024
    RETRY_SECONDS = 6 * 3600
    UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ICON_TYPES = {"png": "image/png", "ico": "image/x-icon", "svg": "image/svg+xml", "gif": "image/gif",
                  "jpg": "image/jpeg", "webp": "image/webp"}

    def __init__(self, path):
        self.path = path
        self.icons = path.parent / "favicons"
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._opener = build_opener(WebRedirect)
        self._insecure = build_opener(WebRedirect, HTTPSHandler(context=ssl._create_unverified_context()))
        try:
            self._links = [l for l in json.loads(path.read_text(encoding="utf-8")).get("links", []) if isinstance(l, dict)]
        except (OSError, ValueError, AttributeError):
            self._links = []
        self._wake.set()  # links still pending from before a restart

    def get(self):
        with self._lock:
            return {"links": [{k: l.get(k) for k in ("id", "url", "name", "title", "icon", "state")} for l in self._links]}

    @staticmethod
    def clean_url(value):
        url = str(value or "").strip()
        parts = urlsplit(url)
        if len(url) > 2048 or parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"not a web address: {url[:80] or '(empty)'} (http:// or https:// only)")
        return url

    def replace(self, items):
        """Saves the list sent by the page editor: [{"id"?, "url", "name", "refresh"?}] in display order."""
        if not isinstance(items, list) or len(items) > self.MAX_LINKS:
            raise ValueError(f"up to {self.MAX_LINKS} links")
        with self._lock:
            old = {l["id"]: l for l in self._links}
            links = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("each link must be {\"url\": ..., \"name\": ...}")
                url, name = self.clean_url(item.get("url")), " ".join(str(item.get("name") or "").split())
                if len(name) > 60:
                    raise ValueError("a name is longer than 60 characters")
                prev = old.get(item.get("id"))
                if prev and prev["url"] == url and item.get("refresh") is not True:
                    links.append({**prev, "name": name})
                else:
                    links.append({"id": prev["id"] if prev else secrets.token_hex(4), "url": url, "name": name,
                                  "title": "", "icon": None, "state": "pending", "checked": 0})
            self._links = links
            self._save()
        self._cleanup()
        self._wake.set()
        return self.get()

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"links": self._links}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            LOG.warning("quick links not saved: %s", exc)

    def _cleanup(self):
        with self._lock:
            used = {l.get("icon") for l in self._links}
        for file in self.icons.glob("*") if self.icons.is_dir() else []:
            if file.name not in used:
                file.unlink(missing_ok=True)

    def loop(self):
        while True:
            self._wake.wait(3600)
            self._wake.clear()
            while True:
                now = time.time()
                with self._lock:
                    todo = next((dict(l) for l in self._links if l.get("state") == "pending"
                                 or (l.get("state") == "failed" and now - l.get("checked", 0) > self.RETRY_SECONDS)), None)
                if not todo:
                    break
                title, icon, ok = self._preview(todo)
                with self._lock:
                    for l in self._links:
                        if l["id"] == todo["id"] and l["url"] == todo["url"]:
                            l.update(title=title, icon=icon or l.get("icon"), state="ok" if ok else "failed", checked=time.time())
                    self._save()
                self._cleanup()

    def _open(self, url, limit, accept):
        """(final URL, Content-Type, body up to limit bytes); retries without certificate checks for
        self-signed LAN services: only a title and an icon are read, nothing is sent."""
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("not http(s)")
        req = Request(url, headers={"User-Agent": self.UA, "Accept": accept, "Accept-Language": "en,*;q=0.5"})
        try:
            resp = self._opener.open(req, timeout=8)
        except URLError as exc:
            if not isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise
            resp = self._insecure.open(req, timeout=8)
        with resp:
            return resp.geturl(), resp.headers.get("Content-Type", ""), resp.read(limit + 1)[:limit]

    def _preview(self, link):
        """(title, cached icon file name or None, page was readable)."""
        url, head, final = link["url"], PageHead(), link["url"]
        try:
            final, ctype, body = self._open(url, self.MAX_PAGE, "text/html,application/xhtml+xml,*/*;q=0.8")
            charset = re.search(r"charset=([\w-]+)", ctype) or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
            try:
                text = body.decode(charset[1] if isinstance(charset[1], str) else charset[1].decode()) if charset else body.decode("utf-8")
            except (LookupError, UnicodeDecodeError):
                text = body.decode("utf-8", errors="replace")
            for chunk in range(0, len(text), 8192):
                head.feed(text[chunk:chunk + 8192])
                if head.done:
                    break
            ok = True
        except Exception as exc:  # unreachable, refused, not a page: keep the host as the name, try /favicon.ico
            LOG.info("quick link %s: page not readable (%s)", url, exc)
            ok = False
        title = " ".join(head.title.split()) or ""
        site = " ".join(head.site_name.split())
        if len(title) > 30:
            # Long page titles ("Repo · Build and ship software ... · GitHub") do not fit a pill: the site name,
            # else the first part before a separator ("Українська правда - новини онлайн" -> "Українська правда")
            first = re.split(r"\s+[-|·—–:]\s+|:\s+", title, maxsplit=1)[0].strip()
            title = site or (first if 3 <= len(first) < len(title) else title)
        icon = self._icon(link["id"], final, head.icons)
        return title[:120], icon, ok

    def _icon(self, link_id, page_url, icons):
        def score(a):
            sizes = [int(n) for n in re.findall(r"(\d+)x\d+", a.get("sizes", ""))]
            svg = a.get("type", "").endswith("svg+xml") or urlsplit(a["href"]).path.lower().endswith(".svg")
            size = max(sizes) if sizes else (180 if "apple-touch-icon" in a.get("rel", "") else 32)
            return (0 if svg else 1, abs(size - 64))  # vector first, then the size nearest 64 px (crisp at 2x)
        candidates = [urljoin(page_url, a["href"]) for a in sorted(icons, key=score)]
        root = urlsplit(page_url)
        candidates.append(urlunsplit((root.scheme, root.netloc, "/favicon.ico", "", "")))
        for src in dict.fromkeys(candidates):
            try:
                if src.startswith("data:image/"):
                    meta, _, payload = src.partition(",")
                    data = base64.b64decode(payload) if meta.endswith(";base64") else unquote(payload).encode()
                else:
                    _, _, data = self._open(src, self.MAX_ICON, "image/*")
                ext = self._image_type(data)
                if not ext:
                    continue
                self.icons.mkdir(parents=True, exist_ok=True)
                name = f"{link_id}-{hashlib.sha256(data).hexdigest()[:10]}.{ext}"
                (self.icons / name).write_bytes(data)
                return name
            except Exception:
                continue
        return None

    @staticmethod
    def _image_type(data):
        """File type from the image bytes: the Content-Type of favicons is often wrong."""
        if len(data) < 16 or len(data) >= Bookmarks.MAX_ICON:
            return None
        if data.startswith(b"\x89PNG"):
            return "png"
        if data[:4] == b"\x00\x00\x01\x00":
            return "ico"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        if data[:3] == b"\xff\xd8\xff":
            return "jpg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        if b"<svg" in data[:1024].lower():
            return "svg"
        return None


BOOKMARKS = Bookmarks(ROOT / "data" / "bookmarks.json")


def site_config():
    """services.yaml, with a wallpaper set from the page replacing page.background.image."""
    result = SITE.get()
    wallpaper = WALLPAPER.current()
    if not result["config"]:
        return result
    result = {**result, "config": copy.deepcopy(result["config"])}
    page = result["config"]["page"]
    configured = page.get("background")
    if wallpaper:
        background = dict(configured or {"shade": 0.45, "blur": 0})
        # Older saves stored "Author / Unsplash"; the credit is a link to the photo, so the name is enough
        credit = str(wallpaper.get("credit") or "").removesuffix(" / Unsplash").removesuffix("Unsplash").strip()
        background.update(image=f"media/{wallpaper['file']}?v={int(wallpaper['set_at'])}", tone="auto",
                          credit=credit, credit_url=wallpaper["credit_url"],
                          location=wallpaper.get("location") or "", source="page")
        page["background"] = background
    elif configured:
        page["background"] = {**configured, "source": "config"}
    return result


class SpeedtestSchedule:
    """Starts the router speed test once a day at speedtest.schedule (router local time).

    If the collector was not running at that minute, the test still starts within LATE_SECONDS;
    later that day is skipped. The date of the last run is kept on disk, so a restart never
    causes a second test on the same day.
    """

    CHECK_SECONDS = 30
    LATE_SECONDS = 3600
    RETRY_SECONDS = 300

    def __init__(self, path, speedtest, state):
        self.path = path
        self._speedtest = speedtest
        self._state = state
        self._retry_at = 0.0
        try:
            self._last = json.loads(path.read_text(encoding="utf-8")).get("last_date")
        except (OSError, ValueError):
            self._last = None

    def _router_now(self):
        return router_now()

    def _done(self, day):
        self._last = day
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"last_date": day}), encoding="utf-8")
        except OSError as exc:
            LOG.warning("speed test schedule state not saved: %s", exc)

    def check(self):
        schedule = ((SITE.get()["config"] or {}).get("speedtest") or {}).get("schedule")
        if not schedule:
            return
        now = self._router_now()
        hour, minute = map(int, schedule.split(":"))
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        day = now.strftime("%Y-%m-%d")
        if self._last == day or not due <= now < due + timedelta(seconds=self.LATE_SECONDS):
            return
        if time.monotonic() < self._retry_at:
            return
        try:
            self._speedtest.set(True)
            LOG.info("scheduled speed test started (%s router time)", schedule)
            self._done(day)
        except Conflict as exc:  # a test is running or just ran: tonight's test is covered
            LOG.info("scheduled speed test skipped: %s", exc)
            self._done(day)
        except Exception as exc:  # router unreachable: try again within the window
            LOG.warning("scheduled speed test failed, retrying in %d min: %s", self.RETRY_SECONDS // 60, exc)
            self._retry_at = time.monotonic() + self.RETRY_SECONDS

    def loop(self):
        while True:
            try:
                self.check()
            except Exception as exc:  # never let the scheduler thread die
                LOG.warning("speed test schedule: %s", exc)
            time.sleep(self.CHECK_SECONDS)


SCHEDULE = SpeedtestSchedule(ROOT / "data" / "schedule.json", SPEEDTEST, STATE)


class Handler(BaseHTTPRequestHandler):
    server_version = "glinet-dashboard"

    def _send(self, code, body, mime, cache="no-store", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _asset(self, rel):
        """Images from the config folder (background, icons/); nothing else is reachable."""
        root = CONFIG.parent.resolve()
        path = (root / rel).resolve()
        if not ASSET.match(rel) or root not in path.parents or not path.is_file():
            self.send_error(404)
            return
        mime = ASSET_TYPES[path.suffix[1:].lower()]
        # SVG can carry scripts: forbid them in case the file is opened directly
        headers = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"} if mime == "image/svg+xml" else None
        self._send(200, path.read_bytes(), mime, cache="public, max-age=300", headers=headers)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            self._send(200, STATE.snapshot().encode(), "application/json")
        elif path == "/api/config":
            self._json(200, site_config())
        elif path == "/api/bookmarks":
            self._json(200, BOOKMARKS.get())
        elif path.startswith("/media/favicons/"):
            name = path[len("/media/favicons/"):]
            ext = name.rsplit(".", 1)[-1]
            file = BOOKMARKS.icons / name
            if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{10}\.\w+", name) and ext in Bookmarks.ICON_TYPES and file.is_file():
                # SVG can carry scripts: forbid them in case the file is opened directly
                headers = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"} if ext == "svg" else None
                self._send(200, file.read_bytes(), Bookmarks.ICON_TYPES[ext], cache="public, max-age=86400", headers=headers)
            else:
                self.send_error(404)
        elif path.startswith("/media/"):
            name = path[len("/media/"):]
            file = WALLPAPER.folder / name
            if re.fullmatch(r"[\w-]+\.jpg", name) and file.is_file():
                self._send(200, file.read_bytes(), "image/jpeg", cache="public, max-age=86400")
            else:
                self.send_error(404)
        elif path == "/api/quote":
            self._json(200, QUOTES.current())
        elif path == "/api/history":
            self._json(200, HISTORY.snapshot())
        elif path.startswith("/assets/"):
            self._asset(path[len("/assets/"):])
        elif path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def _read_json(self, limit=4096):
        """JSON body of a POST; None unless it was sent as application/json.

        A JSON content type forces a CORS preflight (which this server never approves),
        so other websites cannot trigger these actions from a visitor's browser.
        """
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._json(415, {"error": "Content-Type must be application/json"})
            return None
        try:
            length = min(int(self.headers.get("Content-Length") or 0), limit)
            body = json.loads(self.rfile.read(length) or b"{}")
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/speedtest", "/api/background", "/api/wan/reconnect", "/api/dns/protection", "/api/vpn", "/api/bookmarks"):
            self.send_error(404)
            return
        body = self._read_json(limit=192 * 1024 if path == "/api/bookmarks" else 4096)
        if body is None:
            return
        if path == "/api/bookmarks":
            try:
                self._json(200, {"ok": True, **BOOKMARKS.replace(body.get("links"))})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/api/background":
            self._background(body)
            return
        if path == "/api/dns/protection":
            self._protection(body)
            return
        if path == "/api/vpn":
            tunnel_id, enabled = body.get("tunnel_id"), body.get("enabled")
            if not isinstance(tunnel_id, int) or isinstance(tunnel_id, bool) or not isinstance(enabled, bool):
                self._json(400, {"error": "body must be {\"tunnel_id\": <number>, \"enabled\": true|false}"})
                return
            try:
                self._json(200, {"ok": True, **VPN.set(tunnel_id, enabled)})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Conflict as exc:
                self._json(409, {"error": str(exc)})
            except Exception as exc:  # router unreachable or refused
                LOG.warning("VPN: %s", exc)
                self._json(502, {"error": f"router: {exc}"})
            return
        if path == "/api/wan/reconnect":
            try:
                self._json(200, {"ok": True, **WAN_RECONNECT.run()})
            except Conflict as exc:
                self._json(409, {"error": str(exc)})
            except Exception as exc:  # router unreachable or LuCI refused
                LOG.warning("WAN reconnect: %s", exc)
                self._json(502, {"error": f"router: {exc}"})
            return
        enable = body.get("enable")
        if not isinstance(enable, bool):
            self._json(400, {"error": "body must be {\"enable\": true} or {\"enable\": false}"})
            return
        try:
            self._json(200, {"ok": True, "result": SPEEDTEST.set(enable)})
        except Conflict as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:  # router unreachable or rejected the call
            LOG.warning("speed test: %s", exc)
            self._json(502, {"error": f"router: {exc}"})

    def _protection(self, body):
        """Pause AdGuard protection for N minutes (default 30, like AdGuard's own menu) or resume it."""
        enabled, minutes = body.get("enabled"), body.get("minutes", 30)
        if not isinstance(enabled, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
            self._json(400, {"error": "body must be {\"enabled\": false, \"minutes\": 1-1440} or {\"enabled\": true}"})
            return
        try:
            GL.adguard("/control/protection", {"enabled": True} if enabled else {"enabled": False, "duration": minutes * 60_000})
            LOG.info("AdGuard protection %s", "resumed" if enabled else f"paused for {minutes} min")
            STATE.ok("dns", poll_dns(GL))  # show the new state right away
            self._json(200, {"ok": True})
        except Exception as exc:  # AdGuard unreachable or refused
            LOG.warning("AdGuard protection: %s", exc)
            self._json(502, {"error": f"AdGuard: {exc}"})

    def _background(self, body):
        if body.get("reset") is True:
            WALLPAPER.reset()
            self._json(200, {"ok": True})
            return
        url, location = body.get("url"), body.get("location", "")
        if not isinstance(location, str) or (url is not None and not isinstance(url, str)) or (not url and "location" not in body):
            self._json(400, {"error": "body must be {\"url\": \"https://unsplash.com/photos/...\", \"location\": \"...\"}, "
                                      "{\"location\": \"...\"} or {\"reset\": true}"})
            return
        try:
            wallpaper = WALLPAPER.set(url, location) if url else WALLPAPER.set_location(location)
            self._json(200, {"ok": True, "wallpaper": wallpaper})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Conflict as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:  # Unsplash unreachable, refused or returned something unexpected
            LOG.warning("wallpaper: %s", exc)
            self._json(502, {"error": str(exc)})

    def log_message(self, fmt, *args):
        pass  # keep the container log for router problems


if __name__ == "__main__":
    if not os.environ.get("GL_PASS"):
        raise SystemExit("GL_PASS is not set")
    threading.Thread(target=poll_loop, args=(GL, LUCI, STATE), name="poll", daemon=True).start()
    threading.Thread(target=stream_loop, args=(GL, STATE), name="stream", daemon=True).start()
    threading.Thread(target=QUOTES.loop, name="quotes", daemon=True).start()
    threading.Thread(target=SCHEDULE.loop, name="schedule", daemon=True).start()
    threading.Thread(target=HEALTH.loop, name="health", daemon=True).start()
    threading.Thread(target=VPN_EXIT.loop, name="vpn-exit", daemon=True).start()
    threading.Thread(target=BOOKMARKS.loop, name="bookmarks", daemon=True).start()
    LOG.info("serving on :%d, polling %s every %d s", PORT, GL_HOST, POLL_SECONDS)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
