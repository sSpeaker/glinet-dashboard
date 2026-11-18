"""qBittorrent card: transfer speed, torrents by state, free disk space, the active torrents.

Config (services.yaml):
    cards:
      - type: qbittorrent
        url: http://192.168.8.20:8080        # the WebUI address
        username: admin                      # optional: only when the WebUI asks for a login
        password_env: QBIT_PASS              # the password itself goes into this environment variable
        min_free_gb: 20                      # optional: warn when the download disk has less free space

Without username/password_env the card expects the WebUI to skip the login for the dashboard's address
(qBittorrent: Options → WebUI → "Bypass authentication for clients in whitelisted IP subnets").
Uses /api/v2/sync/maindata, the same incremental feed the WebUI itself polls.
"""

TYPE = "qbittorrent"
TITLE = "qBittorrent"
INTERVAL = 5

DOWNLOADING = {"downloading", "metaDL", "forcedMetaDL", "forcedDL", "stalledDL", "queuedDL", "checkingDL", "allocating"}
SEEDING = {"uploading", "forcedUP", "stalledUP", "queuedUP", "checkingUP"}
STOPPED = {"stoppedDL", "stoppedUP", "pausedDL", "pausedUP"}
ERRORS = {"error", "missingFiles"}


def check(options):
    url = str(options.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must be the WebUI address, e.g. http://192.168.8.20:8080")
    if bool(options.get("username")) != bool(options.get("password_env")):
        raise ValueError("give both username and password_env, or neither (login bypassed for the LAN)")
    options["min_free_gb"] = float(options.get("min_free_gb", 20))
    return options


def _login(ctx):
    if not ctx.options.get("username"):
        raise RuntimeError("the WebUI asks for a login: add username and password_env to the card")
    resp = ctx.request("POST", ctx.url("/api/v2/auth/login"), headers={"Referer": ctx.url("/")},
                       data={"username": ctx.options["username"], "password": ctx.secret("password")})
    if resp.status != 200 or resp.text().strip() != "Ok.":
        # qBittorrent bans an address after a few failed logins: stop trying for a while
        ctx.pause("login refused: check username and password_env")
        raise RuntimeError("login refused: check username and password_env")


def _sync(ctx):
    """One maindata call; keeps the merged torrent list in ctx.memory between polls."""
    mem = ctx.memory
    resp = ctx.request("GET", ctx.url("/api/v2/sync/maindata"), params={"rid": mem.get("rid", 0)})
    if resp.status == 403:  # not logged in (or the session expired): log in and start over
        _login(ctx)
        mem.clear()
        resp = ctx.request("GET", ctx.url("/api/v2/sync/maindata"), params={"rid": 0})
    if resp.status != 200:
        raise RuntimeError(f"HTTP {resp.status}")
    d = resp.json()
    if d.get("full_update") or "torrents" not in mem:
        mem["torrents"], mem["server"] = {}, {}
    for h, t in (d.get("torrents") or {}).items():
        mem["torrents"].setdefault(h, {}).update(t)
    for h in d.get("torrents_removed") or []:
        mem["torrents"].pop(h, None)
    mem["server"].update(d.get("server_state") or {})
    mem["rid"] = d.get("rid", 0)
    return mem["torrents"], mem["server"]


def poll(ctx):
    torrents, server = _sync(ctx)
    counts = {"downloading": 0, "seeding": 0, "stopped": 0, "error": 0, "other": 0}
    for t in torrents.values():
        state = t.get("state", "")
        key = ("downloading" if state in DOWNLOADING else "seeding" if state in SEEDING
               else "stopped" if state in STOPPED else "error" if state in ERRORS else "other")
        counts[key] += 1
    errors = [t.get("name", "?") for t in torrents.values() if t.get("state") in ERRORS]
    return {
        "down": server.get("dl_info_speed", 0), "up": server.get("up_info_speed", 0),
        "free": server.get("free_space_on_disk"), "connection": server.get("connection_status", ""),
        "total": len(torrents), "counts": counts, "errors": errors[:20],
    }


def _row(t):
    return {"name": t.get("name", ""), "progress": t.get("progress", 0), "down": t.get("dlspeed", 0), "up": t.get("upspeed", 0),
            "eta": t.get("eta", 0), "size": t.get("size", 0), "state": t.get("state", ""), "ratio": round(t.get("ratio", 0), 2),
            "category": t.get("category", "")}


def details(ctx, data):
    """For the details window only: the torrents themselves (active first, then idle seeding, then stopped)."""
    torrents = list((ctx.memory.get("torrents") or {}).values())
    moving = lambda t: t.get("dlspeed") or t.get("upspeed") or t.get("state") in DOWNLOADING
    active = sorted((t for t in torrents if moving(t)), key=lambda t: -(t.get("dlspeed", 0) + t.get("upspeed", 0)))
    rest = lambda states: [_row(t) for t in sorted((t for t in torrents if t.get("state") in states and not moving(t)),
                                                   key=lambda t: t.get("name", "").lower())][:300]
    return {"active": [_row(t) for t in active[:50]], "seeding": rest(SEEDING), "stopped": rest(STOPPED | ERRORS)}


def alerts(data, options):
    result = []
    if data.get("free") is not None and data["free"] < options["min_free_gb"] * 1e9:
        result.append({"id": "disk", "level": "warning", "text": f"only {data['free'] / 1e9:.0f} GB free on the download disk"})
    if data["counts"]["error"]:
        n = data["counts"]["error"]
        result.append({"id": "errors", "level": "warning", "text": f"{n} torrent{'s' if n > 1 else ''} with errors (missing files?)"})
    if data.get("connection") == "disconnected":
        result.append({"id": "offline", "level": "warning", "text": "no connection to the network"})
    return result
