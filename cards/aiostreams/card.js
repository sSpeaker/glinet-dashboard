// AIOStreams: memory, usenet and errors on the tile; system, usenet providers, addons, failed tasks and the
// error / warning log of the last 24 hours in the details window.
(() => {
  const bytes = (h, n) => (n == null ? "–" : h.bytes(n).join(" "));
  const openLog = new Set();   // log entries the reader expanded: stay open across refreshes
  const when = ts => new Date(ts * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  Cards.register("aiostreams", {
    // Tile: what it is doing; orange only when usenet or an addon is in trouble (decided by the collector)
    tile(d, h) {
      const u = d.usenet;
      return { text: u.streams ? `▶ ${u.streams} · ↓${h.byteRate(u.speed).join("")}` : `${d.requests_24h} requests today`, level: d.trouble ? "warning" : "" };
    },
    // Charts for the service window: different units, so two charts
    charts: (d, h) => [
      { title: "CPU", series: [{ key: "s1", label: "CPU", metric: d.series.cpu }], format: v => [Math.round(v), "%"] },
      { title: "Process memory", series: [{ key: "s2", label: "RAM", metric: d.series.rss }], format: v => h.bytes(v) },
    ],
    render(body, d, h) {
      body.append(h.stats([
        ["RAM", d.rss == null ? "–" : h.bytes(d.rss)],
        ["CPU", d.cpu == null ? "–" : [d.cpu, "%"]],
        ["Usenet", d.usenet.streams ? h.byteRate(d.usenet.speed) : "idle"],
        ["Errors 24h", String(d.log_errors)],
      ]));
    },
    details(body, d, h) {
      const section = (title, count) => body.append(h.heading(count == null ? title : `${title} · ${count}`));
      body.append(h.stats([
        ["Process RAM", d.rss == null ? "–" : h.bytes(d.rss)],
        ["Host RAM", d.host_mem.total ? [Math.round(d.host_mem.used / d.host_mem.total * 100), "%"] : "–"],
        ["CPU", d.cpu == null ? "–" : [d.cpu, "%"]],
        ["Uptime", h.duration(d.uptime)],
      ]));

      const u = d.usenet;
      section("Usenet");
      body.append(h.table(["", ""], [
        ["Now", u.streams ? `${u.streams} stream(s) · ${h.byteRate(u.speed).join(" ")}` : "idle"],
        ["Last 24 h", `${bytes(h, u.bytes_24h)} · ${u.articles_24h.toLocaleString("en-GB")} articles · ${u.errors_24h} errors · ${u.missing_24h} missing`],
      ]));
      if (u.providers.length) {
        body.append(h.table(["Provider", "Host", "State", "Connections", "24 h", "Errors", "Missing"], u.providers.map(p => [
          p.name + (p.backup ? " (backup)" : ""), p.host, !p.enabled ? "disabled" : p.tripped ? h.warn("tripped") : p.state === "offline" ? "idle" : p.state,
          `${p.active} / ${p.max}`, bytes(h, p.bytes), `${p.errors}${p.error_rate ? ` (${(p.error_rate * 100).toFixed(1)} %)` : ""}`,
          p.miss_rate ? `${(p.miss_rate * 100).toFixed(1)} %` : "0",
        ])));
      }

      section("Addons, 24 h", `${d.addons.length} addons · ${d.requests_24h} requests`);
      body.append(h.table(["Addon", "Requests", "Errors", "Kind", "Latency"], d.addons.map(a => [  // failing ones first
        a.name, a.requests, a.errors ? h.warn(String(a.errors)) : "0", Object.entries(a.kinds).map(([k, n]) => `${k} ${n}`).join(", ") || "–",
        a.latency == null ? "–" : `${Math.round(a.latency)} ms`,
      ])));

      if (d.failed_tasks.length) {
        section("Failed tasks", d.failed_tasks.length);
        body.append(h.table(["Task", "Last run", "Error"], d.failed_tasks.map(t => [t.name, t.at ? when(t.at) : "–", h.warn(t.error)])));
      }

      section("Log, 24 h", `${d.log_errors} errors · ${d.log_warnings} warnings`);
      const log = d.log || [];
      if (!log.length) body.append(h.note("No errors or warnings."));
      else body.append(h.table(["Time", "Level", "Module", "Message"], log.map(l => {
        if (!l.detail) return [when(l.ts), l.level === "warn" ? "warning" : h.warn(l.level), l.module, l.msg];
        // Click the message for the full record (error, stack, request ...)
        const box = h.el("details", "log-entry"), sum = h.el("summary", null, l.msg), pre = h.el("pre", "log-detail", l.detail);
        box.open = openLog.has(l.id);
        box.addEventListener("toggle", () => { if (box.open) openLog.add(l.id); else openLog.delete(l.id); });
        box.append(sum, pre);
        return [when(l.ts), l.level === "warn" ? "warning" : h.warn(l.level), l.module, box];
      })));
    },
  });
})();
