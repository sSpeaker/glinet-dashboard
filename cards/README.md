# Card plugins

A plugin teaches the dashboard one more service. It is a folder with two files:

```
cards/<type>/
  card.py   # runs in the dashboard's server: fetches the data on a schedule, checks options, raises alerts
  card.js   # runs in the page: draws the card, the value on a service tile and the details window
  card.css  # optional styles
```

The same plugin serves two places, chosen in `config/services.yaml`:

```yaml
cards:                      # a card in the top row / the Servers row
  - internet                # the router cards; remove a line to switch one off
  - quality
  - type: node              # a plugin card: its folder name ...
    title: Proxmox
    url: http://192.168.8.10:9100   # ... plus whatever options the plugin asks for
    link: https://proxmox.example.com   # optional: what the card title opens (default: url)

groups:
  - name: Media
    services:
      - name: qBittorrent
        url: https://qbit.example.com
        widget:             # a live value on the service's tile; click it for the details window
          type: qbittorrent
          url: http://192.168.8.20:8080
```

- **Built-in plugins** live in `cards/` in this repository and the image: `node`, `qbittorrent`, `npm`,
  `homeassistant`, `aiostreams`. Their options are documented at the top of each `card.py`.
- **Your own plugins** go into `config/cards/<type>/` (on CasaOS: `/DATA/AppData/glinet-dashboard/config/cards/`).
  They are loaded at start-up, so restart the container after adding one. A plugin there with the name of a
  built-in one replaces it.
- The first cards of `cards:` form the top row (three, or four when none of them is wide); the rest go to the
  **Servers** row. `id` (default: the type; letters, digits, `-`, `_`) tells two cards of one type apart,
  `title` overrides the plugin's title, `wide: true` makes a card as wide as the DNS card.
- **Secrets** never go into the YAML: an option `password_env: QBIT_PASS` names an environment variable, and
  the plugin reads it with `ctx.secret("password")`. The page never receives a plugin's options.

A plugin is trusted code: `card.py` runs inside the dashboard with the same rights. Only install plugins you have read.

## card.py

```python
TYPE = "example"       # must equal the folder name
TITLE = "Example"      # card title (the config's title: overrides it)
INTERVAL = 15          # seconds between polls (2-3600)
WIDE = False           # optional: a card as wide as the DNS card

def check(options):
    """Optional. Validate and fill in the options from services.yaml and return them. Raise ValueError with a
    readable message: it shows on the page like any other config error (any other exception does too)."""
    if not str(options.get("url", "")).startswith(("http://", "https://")):
        raise ValueError("url must be the service address")
    options["warn_at"] = float(options.get("warn_at", 0.9))
    return options

def poll(ctx):
    """Required. Return a small JSON-friendly dict: it travels to every open page with /api/status every few
    seconds and is handed to card.js as `data`. Raise on failure: the message shows on the card, which turns
    stale, and becomes an alert if it lasts."""
    status = ctx.get_json(ctx.url("/api/status"), headers={"Authorization": f"Bearer {ctx.secret('token')}"})
    ctx.record("cpu", status["cpu"] * 100)          # a metric: kept 30 days, charted by the page
    ctx.memory["items"] = status["items"]            # big things stay here for details()
    return {"cpu": status["cpu"], "count": len(status["items"]), "series": {"cpu": ctx.series("cpu")}}

def details(ctx, data):
    """Optional. Extra data for the details window only (long lists, logs), merged into `data` there. Called on
    demand while a window is open, never at the same time as poll()."""
    return {"items": ctx.memory.get("items", [])[:300]}

def alerts(data, options):
    """Optional. Warnings and critical alerts for the strip next to "Live"; the card title is prefixed. They
    also colour the card's tile, so thresholds live here, not in card.js."""
    if data["cpu"] > options["warn_at"]:
        return [{"id": "cpu", "level": "warning", "text": f"CPU at {data['cpu']:.0%}"}]
    return []
```

`ctx` gives the plugin:

| | |
|---|---|
| `ctx.options` | the card's options from services.yaml (after `check`) |
| `ctx.url(path)` | `options["url"]` without a trailing slash, plus `path` |
| `ctx.secret(name)` | the environment variable named by the option `<name>_env`; `None` without that option |
| `ctx.request(method, url, params=, data=, json_body=, headers=, timeout=8)` | HTTP(S) with cookies kept between polls; returns `.status`, `.headers`, `.body`, `.json()`, `.text()` for any status, raises only on network errors |
| `ctx.get_json(url, ...)` | GET, raises on HTTP 400 and above, returns the parsed JSON |
| `ctx.memory` | a dict kept between polls (sessions, counters, incremental sync state, data for `details`) |
| `ctx.record(name, value)` | store a metric of this card: 30-second points for a day, 5-minute averages for 30 days, in `data/metrics.db` |
| `ctx.series(name)` | the metric's full name, to hand to the page for its charts |
| `ctx.history(name)`, `ctx.histories([names])` | the last 30 minutes of one / several metrics (one value per 30 s, `None` where missing); `histories` is one database query |
| `ctx.pause(reason)` | stop polling this card for a while (5 minutes, doubling up to 6 hours) and show `reason`: call it when a login is refused, so the dashboard does not get banned for retrying |

Options the core understands for every card: `url`, `tls_verify: false` (accept self-signed certificates, for
LAN services). Only `http(s)` URLs can be requested; redirects to other schemes are refused. Data that cannot
go into JSON (sets, dates, NaN) is refused and shown as the card's error.

## card.js

```js
Cards.register("example", {
  // A card: fill `body` (an empty element inside the card) from `data`. Called on every refresh (5 s).
  render(body, data, h, card) {
    body.append(h.stats([["CPU", h.percent(data.cpu)], ["Items", String(data.count)]]));
  },
  // A service tile's value: text, or {text, level}. Without `level` the tile takes the colour of the
  // card's alerts; level: "" keeps it plain whatever the alerts say.
  tile: (data, h) => `CPU ${h.percent(data.cpu)}`,
  // Charts for the service's info window, by metric name (the period switch loads them).
  charts: (data, h) => [
    { title: "CPU", series: [{ key: "s1", label: "CPU", metric: data.series.cpu }], max: 100, format: v => [Math.round(v), "%"] },
  ],
  // The details window (⤢ on a card, a click on a tile's value): `data` here also has what details() added.
  details(body, data, h, card) {
    body.append(h.history([{ title: "CPU", series: [{ key: "s1", label: "CPU", metric: data.series.cpu }], max: 100 }]));
    body.append(h.heading(`Items · ${data.items.length}`));
    body.append(h.table(["Name", "Size"], data.items.map(i => [i.name, h.bytes(i.size).join(" ")])));
  },
});
```

All four are optional except that a card needs `render`. The core draws the card frame: title (a link),
status dot (green: fresh data, orange: stale, red: no data), the *stale* mark and the ⤢ button. An exception
in a plugin function shows on that card or window only.

Helpers in `h` (the page's own formats and building blocks, so cards look alike):

| | |
|---|---|
| `h.stats([[label, value], ...])` | a row of big numbers like the router cards; `value` is text, an element or a `[number, unit]` pair |
| `h.line([part, ...])` | one muted line, parts joined with " · ", falsy parts skipped; parts are text or elements |
| `h.warn(text, hint)` | orange text, with an optional hover hint |
| `h.note(text)`, `h.heading(text)` | a muted paragraph; a section title in a details window |
| `h.table(headers, rows)` | a table; cells are text or elements. With all-empty headers it is a compact "label – value" list |
| `h.meter(fraction, label)` | a small progress bar with a label |
| `h.spark(values, {unit, key})` | a tiny trend line for a table cell; its hint names the latest value and the peak |
| `h.cut(text, max)` | text shortened to `max` characters, the full text in its tooltip |
| `h.chart(name, series, {step, max, format, height})` | a line chart with crosshair and tooltip from values the card has: `series = [{key: "s1" \| "s2", label, values}]`, oldest first, one value per `step` seconds |
| `h.legend([[key, label], ...])` | the legend for a two-series chart |
| `h.history([{title, series: [{key, label, metric}], format, max}])` | details window only: the "History" block with the period switch (30 min … 30 days); charts are loaded from the metrics store by name. One chart per unit: never two scales in one |
| `h.bytes(n)`, `h.byteRate(n)`, `h.bitRate(n)` | `[number, unit]`: `1234567` → `["1.23", "MB"]`; rates take bytes per second |
| `h.duration(seconds)`, `h.percent(fraction)` | `"1h 15m"`, `"42 %"` |
| `h.el(tag, className, text)` | create an element |

A chart has two series colours (`s1` blue, `s2` orange): more than two series belong in separate charts or
a table. Insert text with `textContent` or the helpers, never `innerHTML` with data from a service.

## Built-in plugins

| Type | Shows | Needs |
|---|---|---|
| [`node`](node/) | a server from Prometheus **node_exporter**: CPU, memory, temperature, uptime, network, free space; details: history charts, SMART per disk, failed systemd services, Proxmox guests and Docker containers | node_exporter; for SMART, guests and containers see below |
| [`qbittorrent`](qbittorrent/) | transfer speed, torrents by state, free space; details: active, seeding and stopped torrents | WebUI address; `username` + `password_env` unless the login is bypassed for the LAN |
| [`npm`](npm/) | Nginx Proxy Manager: proxy hosts, days until the first certificate expires; details: certificates and hosts | `username` + `password_env` (a view-only user is enough) |
| [`homeassistant`](homeassistant/) | chosen entities (sensors, weather, climate); details: all of them and pending updates | `token_env`: a long-lived access token; `entities:` |
| [`aiostreams`](aiostreams/) | requests, usenet streams; details: CPU / memory, usenet providers, addons, failed tasks, error log | `username` + `password_env` of a dashboard user |

A service tile linked to a container or a guest of a `node` card also gets that container's CPU / memory
charts in its info window. The link is found by name (the service name equals the container name, the image
name or the guest's host name, compared on letters and digits); set `container: <name>` or `guest: <vmid>` on
the service to choose it yourself.

### node: SMART, Proxmox guests, Docker containers

`node` reads whatever node_exporter serves. Three extras come through its *textfile collector*, a folder of
`.prom` files it adds to `/metrics`:

- **SMART** (`smartmon_*` metrics): the `smartmon.sh` / `smartmon.py` script from
  [node-exporter-textfile-collector-scripts](https://github.com/prometheus-community/node-exporter-textfile-collector-scripts)
  (on Debian / Proxmox: package `prometheus-node-exporter-collectors`).
- **Proxmox VMs and containers**: [`node/pve-guests-textfile`](node/pve-guests-textfile) on the Proxmox host. It reads
  `pvesh get /cluster/resources` (local, read-only, no API token) once a minute:

  ```sh
  install -m 755 pve-guests-textfile /usr/local/bin/
  install -m 644 pve-guests-textfile.service pve-guests-textfile.timer /etc/systemd/system/
  systemctl daemon-reload && systemctl enable --now pve-guests-textfile.timer
  ```

- **Docker containers**: [`node/docker-textfile`](node/docker-textfile) on the Docker host (`docker ps`, `inspect`,
  `stats`: read-only), with `docker-textfile.service` / `.timer` installed the same way. It writes into
  `/var/lib/node_exporter/textfile`; [`node/node-exporter-casaos.yml`](node/node-exporter-casaos.yml) runs
  node_exporter as a CasaOS app that reads that folder. Create the folder (or run the script once) before
  installing the app.

Both scripts take `TEXTFILE_DIR` to write somewhere else.
