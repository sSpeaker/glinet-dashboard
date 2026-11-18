"""Server health from Prometheus node_exporter: CPU, memory, CPU temperature, uptime, SMART per disk,
filesystems, network, failed systemd services, memory (ECC) errors, Proxmox VMs/LXCs when
pve-guests-textfile runs on the host, Docker containers when docker-textfile does (see README.md here).

Config (services.yaml):
    cards:
      - type: node
        title: Proxmox
        url: http://192.168.8.10:9100        # node_exporter (it serves /metrics)
        filesystems: ["/", "/DATA"]           # optional: mount points to show (default: every real disk)
        cpu_temp: {warning: 80, critical: 90} # optional alert thresholds; "off" turns one off
        disk_temp: {warning: 50, critical: 60}
        memory: {warning: 90, critical: off}  # % used
        filesystem: {warning: 85, critical: 95}
        smart_max_age_minutes: 120            # SMART data older than this is a warning (smartmon stopped?)
"""

import math
import re

TYPE = "node"
TITLE = "Server"
INTERVAL = 10
WIDE = False

THRESHOLDS = {"cpu_temp": (80, 90), "disk_temp": (50, 60), "memory": (90, None), "filesystem": (85, 95)}
VIRTUAL_NIC = re.compile(r"^(lo|veth|tap|fw|vmbr|docker|br-|virbr|bond|wg|tun|tailscale|zt|dummy)")
LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)')
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def check(options):
    url = str(options.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must be the node_exporter address, e.g. http://192.168.8.10:9100")
    fs = options.get("filesystems")
    if fs is not None and (not isinstance(fs, list) or not all(isinstance(m, str) for m in fs)):
        raise ValueError("filesystems must be a list of mount points, e.g. [\"/\"]")
    for key, (warning, critical) in THRESHOLDS.items():
        value = options.get(key, {})
        if value is False or value is None:
            value = {"warning": None, "critical": None}
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be {{warning: N, critical: N}} or off")
        options[key] = {level: (None if value.get(level, default) in (False, None) else float(value.get(level, default)))
                        for level, default in (("warning", warning), ("critical", critical))}
    options["smart_max_age_minutes"] = float(options.get("smart_max_age_minutes", 120))
    options["filesystems"] = fs
    return options


def parse(text):
    """Prometheus text format → {name: [(labels, value), ...]}."""
    metrics = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        m = LINE.match(line)
        if not m:
            continue
        labels = {k: v.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\") for k, v in LABEL.findall(m[2] or "")}
        try:
            value = float(m[3])
        except ValueError:
            continue
        if math.isfinite(value):  # NaN / Inf samples (a sensor that is gone) are not data
            metrics.setdefault(m[1], []).append((labels, value))
    return metrics


def one(metrics, name, default=None, **match):
    for labels, value in metrics.get(name, []):
        if all(labels.get(k) == v for k, v in match.items()):
            return value
    return default


def by(metrics, name, key):
    """{label value: metric value} for one label."""
    return {labels.get(key): value for labels, value in metrics.get(name, [])}


def cpu_temperature(m):
    names = {labels["chip"]: labels.get("chip_name", "") for labels, _ in m.get("node_hwmon_chip_names", [])}
    sensor_labels = {(l["chip"], l["sensor"]): l.get("label", "") for l, _ in m.get("node_hwmon_sensor_label", [])}
    best = None
    for labels, value in m.get("node_hwmon_temp_celsius", []):
        chip, sensor = labels.get("chip", ""), labels.get("sensor", "")
        name, label = names.get(chip, ""), sensor_labels.get((chip, sensor), "")
        if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal") and value > 0:
            # the package / Tctl sensor is the CPU temperature; otherwise the hottest core
            if label.startswith(("Package", "Tctl", "Tdie")):
                return value
            best = max(best or value, value)
    return best


def disks(m, now):
    info = {l["disk"]: l for l, _ in m.get("smartmon_device_info", [])}
    raw = lambda name: by(m, f"smartmon_{name}_raw_value", "disk")
    temps, hours = raw("temperature_celsius"), raw("power_on_hours")
    airflow = raw("airflow_temperature_cel")
    bad = {k: raw(k) for k in ("reallocated_sector_ct", "current_pending_sector", "offline_uncorrectable", "reported_uncorrect")}
    wear = by(m, "smartmon_wear_leveling_count_value", "disk")
    healthy = by(m, "smartmon_device_smart_healthy", "disk")
    rotation = by(m, "node_disk_ata_rotation_rate_rpm", "device")
    result = []
    for disk, l in sorted(info.items()):
        dev = disk.rsplit("/", 1)[-1]
        name = l.get("model_family") or l.get("device_model") or l.get("product") or dev
        result.append({
            "disk": dev, "name": name, "model": l.get("device_model", ""), "serial": l.get("serial_number", ""),
            "ssd": rotation.get(dev) == 0 if dev in rotation else None,
            "temp": temps.get(disk) or airflow.get(disk), "hours": hours.get(disk),
            "healthy": None if disk not in healthy else bool(healthy[disk]),
            "bad": {k: int(v.get(disk) or 0) for k, v in bad.items() if disk in v},
            "life": wear.get(disk),       # normalised wear leveling: 100 = new
        })
    mtime = next((v for l, v in m.get("node_textfile_mtime_seconds", []) if l.get("file", "").endswith("smartmon.prom")), None)
    return result, (now - mtime if mtime else None)


REAL_FS = {"ext4", "ext3", "ext2", "xfs", "btrfs", "zfs", "f2fs", "bcachefs", "ntfs", "ntfs3", "exfat", "fuseblk", "nfs", "nfs4", "cifs"}


def filesystems(m, wanted):
    """The listed mount points, or every real disk once (by device), biggest last: no /boot, tmpfs, overlays."""
    rows = {}
    for labels, size in m.get("node_filesystem_size_bytes", []):
        mount, dev, fstype = labels.get("mountpoint", ""), labels.get("device", ""), labels.get("fstype", "")
        if wanted is not None:
            if mount in wanted:
                rows[mount] = (mount, size)
        elif fstype in REAL_FS and size > 1e9 and not mount.startswith(("/boot", "/var/lib/docker", "/snap")):
            if dev not in rows or len(mount) < len(rows[dev][0]):  # one row per device: its shortest mount point
                rows[dev] = (mount, size)
    free = by(m, "node_filesystem_avail_bytes", "mountpoint")
    order = (lambda r: wanted.index(r[0])) if wanted is not None else (lambda r: (r[0] != "/", r[0]))
    return [{"mount": mount, "size": size, "used": size - free.get(mount, 0)} for mount, size in sorted(rows.values(), key=order)]


def containers(m, now):
    if "docker_container_info" not in m:
        return None
    vals = {name: by(m, f"docker_container_{name}", "name") for name in ("up", "exit_code", "restarts", "started_seconds", "cpu_ratio", "memory_bytes", "memory_limit_bytes")}
    rows = []
    for l, _ in m["docker_container_info"]:
        n = l.get("name", "")
        started = vals["started_seconds"].get(n) or 0
        rows.append({"name": n, "image": l.get("image", ""), "state": l.get("state", ""), "health": l.get("health", "none"),
                     "policy": l.get("restart_policy", "no"), "up": bool(vals["up"].get(n)), "exit": int(vals["exit_code"].get(n) or 0),
                     "restarts": int(vals["restarts"].get(n) or 0), "cpu": vals["cpu_ratio"].get(n, 0), "mem": vals["memory_bytes"].get(n, 0),
                     "uptime": now - started if started and vals["up"].get(n) else 0})
    return sorted(rows, key=lambda c: c["name"].lower())


def guests(m):
    if "pve_guest_info" not in m:
        return None
    vals = {name: by(m, f"pve_guest_{name}", "vmid") for name in ("up", "onboot", "cpu_ratio", "cpus", "memory_bytes", "memory_max_bytes", "uptime_seconds")}
    rows = []
    for l, _ in m["pve_guest_info"]:
        v = l.get("vmid")
        rows.append({"vmid": int(v), "name": l.get("name", v), "type": l.get("type", ""), "status": l.get("status", ""),
                     "up": bool(vals["up"].get(v)), "onboot": bool(vals["onboot"].get(v)), "cpu": vals["cpu_ratio"].get(v, 0),
                     "cpus": int(vals["cpus"].get(v) or 0), "mem": vals["memory_bytes"].get(v, 0),
                     "mem_max": vals["memory_max_bytes"].get(v, 0), "uptime": vals["uptime_seconds"].get(v, 0)})
    return sorted(rows, key=lambda g: g["vmid"])


def poll(ctx):
    m = parse(ctx.request("GET", ctx.url("/metrics"), timeout=8).text())
    if "node_boot_time_seconds" not in m:
        raise RuntimeError("no node_exporter metrics at this address")
    now, mem = one(m, "node_time_seconds"), ctx.memory
    # CPU use and network rates come from counter deltas between two polls
    cpu_total = sum(v for _, v in m.get("node_cpu_seconds_total", []))
    cpu_idle = sum(v for l, v in m.get("node_cpu_seconds_total", []) if l.get("mode") in ("idle", "iowait"))
    cpu = None
    if "cpu" in mem and cpu_total > mem["cpu"][0]:
        cpu = 1 - (cpu_idle - mem["cpu"][1]) / (cpu_total - mem["cpu"][0])
    mem["cpu"] = (cpu_total, cpu_idle)
    rx, tx = by(m, "node_network_receive_bytes_total", "device"), by(m, "node_network_transmit_bytes_total", "device")
    up, speed = by(m, "node_network_up", "device"), by(m, "node_network_speed_bytes", "device")
    nics, last = [], mem.get("net")
    for dev in sorted(rx):
        if VIRTUAL_NIC.match(dev) or not up.get(dev):
            continue
        rate = None
        if last and dev in last[1] and now > last[0]:
            dt = now - last[0]
            rate = ((rx[dev] - last[1][dev][0]) / dt, (tx[dev] - last[1][dev][1]) / dt)
        nics.append({"dev": dev, "speed": speed.get(dev), "rx": rate and max(0, rate[0]), "tx": rate and max(0, rate[1])})
    mem["net"] = (now, {d: (rx[d], tx.get(d, 0)) for d in rx})
    total, avail = one(m, "node_memory_MemTotal_bytes", 0), one(m, "node_memory_MemAvailable_bytes", 0)
    # Metrics into the dashboard's store (30 days, survives restarts); the page's charts load them by name
    rates = [n for n in nics if n["rx"] is not None]
    ctx.record("cpu", None if cpu is None else round(cpu * 100, 1))
    ctx.record("mem", round((total - avail) / total * 100, 1) if total else None)
    if rates:
        ctx.record("rx", sum(n["rx"] for n in rates))
        ctx.record("tx", sum(n["tx"] for n in rates))
    disk_list, smart_age = disks(m, now)
    cpu_temp = cpu_temperature(m)
    ctx.record("temp", cpu_temp)
    for disk in disk_list:
        ctx.record(f"disk.{disk['disk']}.temp", disk["temp"])
        disk["series"] = ctx.series(f"disk.{disk['disk']}.temp")
    docker = containers(m, now)
    guest_list = guests(m)
    # CPU / RAM per guest and container, recorded while it runs
    for key, item, mem_pct in ([(f"g.{g['vmid']}", g, g["mem"] / g["mem_max"] * 100 if g["mem_max"] else 0) for g in guest_list or []]
                               + [(f"ct.{c['name']}", c, c["mem"] / total * 100 if total else 0) for c in docker or []]):
        if item["up"]:
            ctx.record(f"{key}.cpu", round(item["cpu"] * 100, 2))
            ctx.record(f"{key}.mem", round(mem_pct, 2))
        item["series"] = {"cpu": ctx.series(f"{key}.cpu"), "mem": ctx.series(f"{key}.mem")}
    if docker is not None:  # restarts since the previous poll: a container restarting in a loop
        last = mem.get("restarts", {})
        for c in docker:
            c["restarting"] = c["state"] == "restarting" or c["restarts"] > last.get(c["name"], c["restarts"])
        mem["restarts"] = {c["name"]: c["restarts"] for c in docker}
    return {
        "cpu": cpu, "cores": len({l.get("cpu") for l, _ in m.get("node_cpu_seconds_total", [])}),
        "load": one(m, "node_load1"), "temp": cpu_temp,
        "mem": {"total": total, "used": total - avail}, "uptime": now - one(m, "node_boot_time_seconds", now),
        "disks": disk_list, "smart_age": smart_age,
        "fs": [{**f, "level": _level(f["used"] / f["size"] * 100 if f["size"] else 0, ctx.options["filesystem"]) or "good"}
               for f in filesystems(m, ctx.options["filesystems"])],
        "net": nics,
        "failed": sorted(l.get("name", "?") for l, v in m.get("node_systemd_unit_state", []) if l.get("state") == "failed" and v == 1),
        "ecc": {"correctable": sum(v for _, v in m.get("node_edac_correctable_errors_total", [])),
                "uncorrectable": sum(v for _, v in m.get("node_edac_uncorrectable_errors_total", []))},
        "guests": guest_list,
        "containers": docker,
        # The card's own chart: 30 minutes of CPU and RAM; everything else is loaded by series name
        "history": {"step": 30, **ctx.histories(["cpu", "mem"]),
                    "series": {k: ctx.series(k) for k in ("cpu", "mem", "rx", "tx", "temp")}},
    }


def details(ctx, data):
    """For the details window only: 30 minutes of CPU per guest and container, for the sparklines."""
    names = [f"g.{g['vmid']}.cpu" for g in data.get("guests") or []] + [f"ct.{c['name']}.cpu" for c in data.get("containers") or []]
    return {"trails": {name: [v or 0 for v in values] for name, values in ctx.histories(names).items()}}


def _level(value, levels):
    if value is None:
        return None
    if levels["critical"] is not None and value >= levels["critical"]:
        return "critical"
    if levels["warning"] is not None and value >= levels["warning"]:
        return "warning"
    return None


def alerts(d, o):
    out = []
    level = _level(d.get("temp"), o["cpu_temp"])
    if level:
        out.append({"id": "cpu_temp", "level": level, "text": f"CPU at {d['temp']:.0f} °C"})
    if d["mem"]["total"]:
        pct = d["mem"]["used"] / d["mem"]["total"] * 100
        level = _level(pct, o["memory"])
        if level:
            out.append({"id": "memory", "level": level, "text": f"memory {pct:.0f} % used"})
    for f in d["fs"]:
        pct = f["used"] / f["size"] * 100 if f["size"] else 0
        level = _level(pct, o["filesystem"])
        if level:
            out.append({"id": f"fs:{f['mount']}", "level": level, "text": f"{f['mount']} is {pct:.0f} % full"})
    for disk in d["disks"]:
        bad = {k: v for k, v in disk["bad"].items() if v}
        if disk["healthy"] is False or bad:
            what = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in bad.items()) or "SMART reports a failing disk"
            out.append({"id": f"smart:{disk['disk']}", "level": "critical", "text": f"{disk['name']}: {what}"})
        level = _level(disk.get("temp"), o["disk_temp"])
        if level:
            out.append({"id": f"disk_temp:{disk['disk']}", "level": level, "text": f"{disk['name']} at {disk['temp']:.0f} °C"})
    if d["disks"] and d.get("smart_age") and d["smart_age"] > o["smart_max_age_minutes"] * 60:
        out.append({"id": "smart_age", "level": "warning", "text": f"SMART data is {d['smart_age'] / 3600:.0f} h old"})
    if d["failed"]:
        out.append({"id": "failed", "level": "warning", "text": f"failed service{'s' if len(d['failed']) > 1 else ''}: {', '.join(d['failed'][:3])}"})
    if d["ecc"]["uncorrectable"]:
        out.append({"id": "ecc", "level": "critical", "text": f"{d['ecc']['uncorrectable']:.0f} uncorrectable memory errors"})
    elif d["ecc"]["correctable"]:
        out.append({"id": "ecc", "level": "warning", "text": f"{d['ecc']['correctable']:.0f} corrected memory errors"})
    for c in d.get("containers") or []:
        # Docker keeps the last health status of a stopped container: only a running one can be unhealthy.
        # always / unless-stopped containers are restarted by Docker after a crash, so one that stays stopped was
        # stopped on purpose (CasaOS's stop button, docker stop); only on-failure gives up after its retries.
        if c["up"] and c["health"] == "unhealthy":
            out.append({"id": f"ct:{c['name']}", "level": "warning", "text": f"container {c['name']} is unhealthy"})
        elif c.get("restarting"):
            out.append({"id": f"ct:{c['name']}", "level": "warning", "text": f"container {c['name']} keeps restarting"})
        elif not c["up"] and c["exit"] != 0 and c["policy"] == "on-failure":
            out.append({"id": f"ct:{c['name']}", "level": "warning", "text": f"container {c['name']} stopped with exit code {c['exit']}"})
    for g in d["guests"] or []:
        if g["onboot"] and not g["up"]:
            out.append({"id": f"guest:{g['vmid']}", "level": "warning", "text": f"{g['name']} ({g['vmid']}) is {g['status'] or 'not running'}"})
    return out
