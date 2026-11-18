"""AIOStreams: memory and CPU, the usenet engine (providers, live speed, 24 h totals), addons with errors,
scheduled tasks that failed, and the error / warning log.

Config (services.yaml), as a card or as the widget of a service tile:
    widget:
      type: aiostreams
      url: http://192.168.8.20:3123
      username: admin                  # a dashboard user of AIOStreams
      password_env: AIO_PASS           # the password itself goes into this environment variable

Logs in like the web UI (POST /api/v1/auth/login, a session cookie), then only reads /api/v1/dashboard/...
"""
import json
import time

TYPE = "aiostreams"
TITLE = "AIOStreams"
INTERVAL = 30
LOG_HOURS = 24


def check(options):
    url = str(options.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must be the AIOStreams address, e.g. http://192.168.8.20:3123")
    if not options.get("username") or not options.get("password_env"):
        raise ValueError("username and password_env are required (the dashboard API needs a login)")
    return options


def _login(ctx):
    resp = ctx.request("POST", ctx.url("/api/v1/auth/login"),
                       json_body={"username": ctx.options["username"], "password": ctx.secret("password")})
    body = resp.json() if resp.status < 500 else {}
    if not (body or {}).get("success"):
        reason = f"login refused: {((body or {}).get('error') or {}).get('message') or resp.status}"
        ctx.pause(reason)  # every failed attempt lands in AIOStreams' own log: do not hammer it
        raise RuntimeError(reason)


def _get(ctx, path):
    for attempt in range(2):
        resp = ctx.request("GET", ctx.url(f"/api/v1/{path}"), timeout=10)
        # Not logged in (or the session expired): the API redirects to the login page, which arrives as HTML
        json_reply = "json" in (resp.headers.get("Content-Type") or "")
        if (resp.status in (401, 403) or not json_reply) and attempt == 0:
            _login(ctx)
            continue
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} for /api/v1/{path}")
        return resp.json().get("data") or {}


def _detail(entry):
    """Every field of the log record except the ones already shown (level, time, module, msg), as indented JSON."""
    try:
        line = json.loads(entry.get("line") or "{}")
    except ValueError:
        return entry.get("line", "")[:4000]
    rest = {k: v for k, v in line.items() if k not in ("level", "time", "module", "msg")}
    return json.dumps(rest, indent=2, ensure_ascii=False)[:4000] if rest else ""


def _message(entry):
    try:
        line = json.loads(entry.get("line") or "{}")
    except ValueError:
        return entry.get("line", "")
    msg = str(line.get("msg") or "")
    err = line.get("err") or line.get("error")
    if isinstance(err, dict):
        err = err.get("message")
    return f"{msg}: {err}" if err and str(err) not in msg else msg


def poll(ctx):
    system = _get(ctx, "dashboard/system")
    usenet = _get(ctx, "dashboard/usenet/stats?window=24h")
    addons = _get(ctx, "dashboard/analytics/addons?range=24h")
    tasks = _get(ctx, "dashboard/tasks").get("tasks") or []
    logs = _get(ctx, "dashboard/logs?order=desc&limit=1000").get("logs") or []
    since = (time.time() - LOG_HOURS * 3600) * 1000
    recent = [l for l in logs if l.get("ts", 0) >= since and l.get("level") in ("error", "fatal", "warn")]
    ctx.memory["log"] = [{"id": l.get("seq"), "ts": l["ts"] / 1000, "level": l.get("level"), "module": l.get("module", ""),
                          "msg": _message(l)[:300], "detail": _detail(l)} for l in recent[:60]]
    mem, cpu, proc = system.get("memory") or {}, system.get("cpu") or {}, system.get("process") or {}
    ctx.record("cpu", cpu.get("total"))
    ctx.record("rss", mem.get("rss"))
    live, totals = usenet.get("live") or {}, usenet.get("totals") or {}
    pool = {p["id"]: p for p in (usenet.get("pool") or {}).get("providers") or []}
    return {
        "cpu": cpu.get("total"), "cores": cpu.get("cores"), "load": (cpu.get("loadavg") or [None])[0],
        "rss": mem.get("rss"), "heap": mem.get("heapUsed"), "host_mem": {"used": mem.get("used"), "total": mem.get("total")},
        "disk": system.get("disk"), "uptime": proc.get("uptimeSec"),
        "usenet": {
            "streams": live.get("activeStreams", 0), "speed": live.get("currentBytesPerSec", 0),
            "errors_minute": live.get("errorsLastMinute", 0),
            "bytes_24h": totals.get("bytes", 0), "articles_24h": totals.get("articles", 0), "errors_24h": totals.get("errors", 0),
            "missing_24h": totals.get("missing", 0),
            "providers": [{"name": p.get("name"), "host": p.get("host"), "enabled": p.get("enabled"), "backup": p.get("isBackup"),
                           "state": (pool.get(p["id"]) or p.get("live") or {}).get("state", ""),
                           "tripped": bool((pool.get(p["id"]) or p.get("live") or {}).get("tripped")),
                           "active": (p.get("live") or {}).get("active", 0), "max": (p.get("live") or {}).get("max", 0),
                           "bytes": p.get("bytes", 0), "errors": p.get("errors", 0), "error_rate": p.get("errorRate", 0),
                           "miss_rate": p.get("missRate", 0)}
                          for p in usenet.get("providers") or [] if not p.get("removed")],
        },
        "addons": sorted(({"name": a.get("presetId"), "requests": a.get("requests", 0), "errors": a.get("errors", 0),
                           "kinds": a.get("errorKinds") or {}, "latency": a.get("avgLatencyMs")}
                          for a in addons.get("addons") or []), key=lambda a: (-a["errors"], -a["requests"])),
        "requests_24h": addons.get("total", 0),
        "failed_tasks": [{"name": t.get("label") or t.get("id"), "error": t.get("lastError") or "", "at": (t.get("lastRunAt") or 0) / 1000}
                         for t in tasks if t.get("enabled") and t.get("lastStatus") == "error"],
        "log_errors": sum(1 for l in recent if l.get("level") in ("error", "fatal")),
        "log_warnings": sum(1 for l in recent if l.get("level") == "warn"),
        "series": {"cpu": ctx.series("cpu"), "rss": ctx.series("rss")},
        # Orange tile: usenet or an addon in trouble (a failed background task is only an alert)
        "trouble": any(p.get("enabled") and (pool.get(p["id"]) or p.get("live") or {}).get("tripped") for p in usenet.get("providers") or [])
                   or any(_failing(a.get("requests", 0), a.get("errors", 0)) for a in addons.get("addons") or []),
    }


def _failing(requests, errors):
    """An addon counts as failing when a quarter of today's requests (at least 5 of them) went wrong."""
    return requests >= 5 and errors / requests >= 0.25


def details(ctx, data):
    """For the details window only: the error / warning log of the last 24 hours, with each record's fields."""
    return {"log": ctx.memory.get("log", [])}


def alerts(d, options):
    out = []
    for p in d["usenet"]["providers"]:
        if p["enabled"] and p["tripped"]:
            out.append({"id": f"provider:{p['name']}", "level": "warning", "text": f"usenet provider {p['name']} is tripped (too many errors)"})
    for t in d["failed_tasks"]:
        out.append({"id": f"task:{t['name']}", "level": "warning", "text": f"task \"{t['name']}\" failed: {t['error'][:80]}"})
    for a in d["addons"]:
        if _failing(a["requests"], a["errors"]):
            out.append({"id": f"addon:{a['name']}", "level": "warning", "text": f"addon {a['name']}: {a['errors']} of {a['requests']} requests failed today"})
    return out
