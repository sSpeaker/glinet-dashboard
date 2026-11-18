"""Home Assistant: chosen entities (sensors, weather, climate, ...) and pending updates.

Config (services.yaml), as a card or as the widget of a service tile:
    widget:
      type: homeassistant
      url: http://192.168.8.30:8123
      token_env: HA_TOKEN                  # a long-lived access token (profile → Security), in this variable
      entities:                            # what the details window lists, in this order
        - sensor.living_room_temperature
        - {entity: sensor.th_158d0002273e14_humidity, name: Living room humidity}   # with a name of your own
        - weather.forecast_home
      tile: [sensor.living_room_temperature, sensor.living_room_humidity]   # optional: on the tile (default: the first two)

Read-only: GET /api/config and /api/states.
"""

TYPE = "homeassistant"
TITLE = "Home Assistant"
INTERVAL = 30


def check(options):
    url = str(options.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must be the Home Assistant address, e.g. http://192.168.8.30:8123")
    if not options.get("token_env"):
        raise ValueError("token_env is required: the name of the variable that holds a long-lived access token")
    entities = []
    for e in options.get("entities") or []:  # an entity id, or {entity: <id>, name: <shown name>}
        e = {"entity": e} if isinstance(e, str) else e
        if not isinstance(e, dict) or "." not in str(e.get("entity", "")):
            raise ValueError("entities: each item is an entity id or {entity: sensor.x, name: My name}")
        entities.append({"entity": str(e["entity"]), "name": str(e.get("name") or "")})
    names = {e["entity"]: e["name"] for e in entities}
    tile = options.get("tile", [e["entity"] for e in entities[:2]])
    if not isinstance(tile, list) or not all(isinstance(e, str) for e in tile):
        raise ValueError("tile must be a list of entity ids")
    options["entities"], options["tile"], options["names"] = entities, tile, names
    return options


def _value(state):
    """Short display value: rounded numbers with their unit, weather / climate with their main attribute."""
    attrs, raw = state.get("attributes") or {}, state.get("state", "")
    domain = state["entity_id"].split(".")[0]
    if domain == "weather":
        t = attrs.get("temperature")
        return f"{raw.replace('partlycloudy', 'partly cloudy').replace('clear-night', 'clear')}" + (f", {t} {attrs.get('temperature_unit', '')}".rstrip() if t is not None else "")
    if domain == "climate":
        cur, target = attrs.get("current_temperature"), attrs.get("temperature")
        return " · ".join(str(x) for x in (raw, cur is not None and f"{cur} °", target is not None and f"→ {target} °") if x)
    try:
        number = float(raw)
        text = f"{number:.0f}" if abs(number) >= 100 or number == int(number) else f"{number:.1f}"
    except ValueError:
        text = raw
    unit = attrs.get("unit_of_measurement") or ""
    return f"{text} {unit}".strip() if text not in ("unavailable", "unknown") else text


def poll(ctx):
    headers = {"Authorization": f"Bearer {ctx.secret('token')}"}
    resp = ctx.request("GET", ctx.url("/api/states"), headers=headers, timeout=10)
    if resp.status == 401:
        raise RuntimeError("the access token was refused")
    if resp.status != 200:
        raise RuntimeError(f"HTTP {resp.status}")
    states = {s["entity_id"]: s for s in resp.json()}
    if "version" not in ctx.memory:
        info = ctx.request("GET", ctx.url("/api/config"), headers=headers).json() or {}
        ctx.memory["version"] = info.get("version", "")
    names = ctx.options["names"]
    row = lambda eid: ({"id": eid, "name": names.get(eid) or (states[eid].get("attributes") or {}).get("friendly_name") or eid,
                        "value": _value(states[eid]), "changed": states[eid].get("last_changed", "")}
                       if eid in states else {"id": eid, "name": eid, "value": "not found", "changed": ""})
    updates = [((s.get("attributes") or {}).get("friendly_name") or eid, (s.get("attributes") or {}).get("latest_version", ""))
               for eid, s in states.items() if eid.startswith("update.") and s.get("state") == "on"]
    return {
        "version": ctx.memory["version"],
        "entities": [row(e["entity"]) for e in ctx.options["entities"]],
        "tile": [row(e)["value"] for e in ctx.options["tile"]],
        "updates": [{"name": n, "version": v} for n, v in sorted(updates)],
        "unavailable": sum(1 for s in states.values() if s.get("state") == "unavailable"),
        "total": len(states),
    }
