#!/usr/bin/env python3
"""Home network collector.

Polls the GL.iNet router with read-only calls, keeps the latest results in memory
and serves them next to the start page:

  GET /            -> index.html
  GET /api/config  -> start page configuration from config/services.yaml {"config", "error"}
  GET /assets/...  -> images from the config folder (background, icons/)
  GET /api/quote   -> quote of the day (ZenQuotes, FavQs as backup), cached on disk
  GET /api/status  -> cached snapshot {"now", "interval", "sources": {name: {data, updated, error}}}
  POST /api/speedtest {"enable": true|false} -> start/stop the router speed test (network-quality.set_speedtest)

Sources (all read-only):
  router   GL RPC   /rpc  system.get_status             CPU, memory, load, uptime, WAN online
  quality  GL WS    /ws   network_quality.status        score, latency, loss, live rate, last speed test
  link     GL WS    /ws   cable.status                  WAN protocol, public IP, gateway
  wan      LuCI     :8080/ubus  network.interface dump  WAN uptime and L3 device
                               luci-rpc getNetworkDevices  WAN byte counters
  dns      AdGuard  :3000/control/stats, /control/status  (GL sid sent as the Admin-Token cookie)
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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
        }
        if background["tone"] not in ("auto", "dark", "light"):
            raise ValueError("page.background.tone must be auto, dark or light")
    theme = str(page.get("theme") or "auto")
    if theme not in ("auto", "light", "dark"):
        raise ValueError("page.theme must be auto, light or dark")
    return {
        "title": parts,
        "tab_title": str(page.get("tab_title") or "".join(part["text"] for part in parts)),
        "theme": theme,
        "background": background or None,
    }


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
            })
        groups.append({"name": str(group["name"]), "services": services})
    return {
        "page": normalize_page(raw.get("page") or {}),
        "network": {key: str(network.get(key) or "") for key in ("subnet", "label", "router")},
        "groups": groups,
        "speedtest": {"schedule": normalize_schedule((raw.get("speedtest") or {}).get("schedule"))},
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
TOP_N = int(os.environ.get("TOP_N", "10"))
PORT = int(os.environ.get("PORT", "3100"))

# WebSocket topic -> source name in the snapshot
WS_TOPICS = {"network_quality.status": "quality", "cable.status": "link"}


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

    def adguard(self, path):
        """AdGuard Home runs with --glinet: it accepts the GL session as the Admin-Token cookie."""
        def fetch(sid):
            req = Request(f"http://{GL_HOST}:{ADGUARD_PORT}{path}", headers={"Cookie": "Admin-Token=" + sid})
            try:
                with urlopen(req, timeout=8) as resp:
                    return json.load(resp)
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


class WanCounters:
    """WAN byte counters from the L3 device (pppoe-wan for PPPoE); rates are derived between polls."""

    def __init__(self):
        self._prev = None

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
        self._prev = (device, rx, tx, now) if rx is not None else None
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


def poll_dns(gl):
    stats = gl.adguard("/control/stats")
    status = gl.adguard("/control/status")

    def top(key):
        return [[name, count] for item in (stats.get(key) or [])[:TOP_N] for name, count in item.items()]

    avg_times = {name: t for item in stats.get("top_upstreams_avg_time") or [] for name, t in item.items()}
    return {
        "running": status.get("running"),
        "protection_enabled": status.get("protection_enabled"),
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


def poll_loop(gl, luci, state):
    wan = WanCounters()
    jobs = (("router", lambda: poll_router(gl)), ("wan", lambda: wan.poll(luci)), ("dns", lambda: poll_dns(gl)))
    while True:
        started = time.monotonic()
        for name, job in jobs:
            try:
                state.ok(name, job())
            except Exception as exc:  # keep polling whatever a single source does
                state.fail(name, exc)
        time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))


def stream_loop(gl, state):
    """Subscribe to the GL UI WebSocket; the router pushes network_quality.status every second."""
    delay = 2
    while True:
        try:
            sid = gl.token()
            try:
                ws = websocket.create_connection(f"ws://{GL_HOST}/ws?sid={sid}", timeout=30)
            except websocket.WebSocketBadStatusException as exc:
                if exc.status_code == 401:
                    gl.drop(sid)
                raise
            try:
                for topic in WS_TOPICS:
                    ws.send(json.dumps({"cmd": "subscribe", "name": topic}))
                LOG.info("websocket subscribed: %s", ", ".join(WS_TOPICS))
                delay = 2
                while True:
                    message = json.loads(ws.recv())
                    name = WS_TOPICS.get(message.get("name"))
                    if name == "quality":
                        SPEEDTEST_TIMES.observe(message.get("data"))
                    if name:
                        state.ok(name, message.get("data"))
            finally:
                ws.close()
        except Exception as exc:  # reconnect on any failure
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
            elif key != self._key:
                self._key, self._finished = key, None
                self._save()
            self._saw_running = False
        elif state == "failed":
            self._saw_running = False
        test["finished_at"] = self._finished


STATE = State()
GL = GLSession()
SPEEDTEST = Speedtest(GL, STATE)
QUOTES = QuoteOfTheDay(Path(os.environ.get("QUOTE_CACHE", ROOT / "data" / "quotes.json")))
SPEEDTEST_TIMES = SpeedtestTimes(ROOT / "data" / "speedtest.json")


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
        """Current router local time from its tzoffset; container local time until the router answered."""
        offset = re.fullmatch(r"([+-])(\d\d)(\d\d)", str((self._state.data("router") or {}).get("tzoffset") or ""))
        if not offset:
            return datetime.now()
        seconds = (int(offset[2]) * 3600 + int(offset[3]) * 60) * (-1 if offset[1] == "-" else 1)
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(tzinfo=None)

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
            self._json(200, SITE.get())
        elif path == "/api/quote":
            self._json(200, QUOTES.current())
        elif path.startswith("/assets/"):
            self._asset(path[len("/assets/"):])
        elif path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/speedtest":
            self.send_error(404)
            return
        # A JSON content type forces a CORS preflight (which this server never approves),
        # so other websites cannot trigger the speed test from a visitor's browser.
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 1024)
            enable = json.loads(self.rfile.read(length) or b"{}").get("enable")
        except (ValueError, AttributeError):
            enable = None
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

    def log_message(self, fmt, *args):
        pass  # keep the container log for router problems


if __name__ == "__main__":
    if not os.environ.get("GL_PASS"):
        raise SystemExit("GL_PASS is not set")
    luci = LuciSession()
    threading.Thread(target=poll_loop, args=(GL, luci, STATE), name="poll", daemon=True).start()
    threading.Thread(target=stream_loop, args=(GL, STATE), name="stream", daemon=True).start()
    threading.Thread(target=QUOTES.loop, name="quotes", daemon=True).start()
    threading.Thread(target=SCHEDULE.loop, name="schedule", daemon=True).start()
    LOG.info("serving on :%d, polling %s every %d s", PORT, GL_HOST, POLL_SECONDS)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
