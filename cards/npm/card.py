"""Nginx Proxy Manager: the certificate that expires first, proxy hosts and whether nginx serves them.

Config (services.yaml), as a card or as the widget of a service tile:
    widget:
      type: npm
      url: http://192.168.8.2:81           # the NPM admin address (its API lives under /api)
      username: dashboard@example.com      # a user of NPM; a non-admin user with view rights is enough
      password_env: NPM_PASS               # the password itself goes into this environment variable
      cert_warning_days: 14                # optional: warn when a certificate expires sooner (critical: a third of it)

Read-only: POST /api/tokens to log in, then GET /api/nginx/certificates, proxy-hosts, redirection-hosts, streams.
"""
from datetime import datetime, timezone

TYPE = "npm"
TITLE = "Nginx Proxy Manager"
INTERVAL = 60


def check(options):
    url = str(options.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must be the NPM admin address, e.g. http://192.168.8.2:81")
    if not options.get("username") or not options.get("password_env"):
        raise ValueError("username and password_env are required (NPM's API needs a login)")
    options["cert_warning_days"] = float(options.get("cert_warning_days", 14))
    return options


def _login(ctx):
    resp = ctx.request("POST", ctx.url("/api/tokens"), json_body={"identity": ctx.options["username"], "secret": ctx.secret("password")})
    token = resp.json().get("token") if resp.status == 200 else None
    if not token:
        ctx.pause("login refused: check username and password_env")
        raise RuntimeError("login refused: check username and password_env")
    ctx.memory["token"] = token


def _get(ctx, path):
    if "token" not in ctx.memory:
        _login(ctx)
    for attempt in range(2):
        resp = ctx.request("GET", ctx.url(f"/api/{path}"), headers={"Authorization": f"Bearer {ctx.memory['token']}"})
        if resp.status == 401 and attempt == 0:  # the token expired: log in again once
            _login(ctx)
            continue
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} for /api/{path}")
        return resp.json()


def _expiry(value):
    """Unix time of expires_on: "2027-01-29 10:39:34" (SQLite installs) or ISO "2027-01-29T10:39:34.000Z"; UTC."""
    try:
        when = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def _cert_level(expires, now, warning_days):
    """warning inside the configured days, critical inside a third of them or once expired; None = unknown date."""
    if expires is None:
        return None
    days = (expires - now) / 86400
    return "critical" if days < warning_days / 3 else "warning" if days < warning_days else ""


def poll(ctx):
    certs, hosts = _get(ctx, "nginx/certificates"), _get(ctx, "nginx/proxy-hosts")
    names = {c["id"]: c.get("nice_name") or ", ".join(c.get("domain_names") or []) for c in certs}
    now = datetime.now(timezone.utc).timestamp()
    cert_rows = sorted(({"name": names[c["id"]], "provider": c.get("provider", ""), "expires": _expiry(c.get("expires_on")),
                         "domains": c.get("domain_names") or []} for c in certs),
                       key=lambda c: c["expires"] or float("inf"))
    for c in cert_rows:
        c["level"] = _cert_level(c["expires"], now, ctx.options["cert_warning_days"])
    host_rows = [{"domains": h.get("domain_names") or [], "target": f"{h.get('forward_scheme', 'http')}://{h.get('forward_host')}:{h.get('forward_port')}",
                  "enabled": bool(h.get("enabled")), "online": (h.get("meta") or {}).get("nginx_online") is not False,
                  "error": (h.get("meta") or {}).get("nginx_err") or "", "cert": names.get(h.get("certificate_id"), "")}
                 for h in sorted(hosts, key=lambda h: (h.get("domain_names") or [""])[0])]
    return {
        "now": now,
        "certs": cert_rows,
        "hosts": host_rows,
        "redirects": len(_get(ctx, "nginx/redirection-hosts")),
        "streams": len(_get(ctx, "nginx/streams")),
    }


def alerts(data, options):
    out = []
    for c in data["certs"]:
        if c["level"] is None:  # a date this plugin cannot read must not pass for "all fine"
            out.append({"id": f"cert:{c['name']}", "level": "warning", "text": f"certificate {c['name']}: expiry date unknown"})
        elif c["level"]:
            days = (c["expires"] - data["now"]) / 86400
            text = f"certificate {c['name']} expired" if days < 0 else f"certificate {c['name']} expires in {days:.0f} days"
            out.append({"id": f"cert:{c['name']}", "level": c["level"], "text": text})
    for h in data["hosts"]:
        if h["enabled"] and not h["online"]:
            out.append({"id": f"host:{h['domains'][0] if h['domains'] else '?'}", "level": "warning",
                        "text": f"{', '.join(h['domains'])} is not served by nginx{': ' + h['error'] if h['error'] else ''}"})
    return out
