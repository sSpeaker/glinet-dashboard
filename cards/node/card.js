// Server health (node_exporter). Card: big numbers, the CPU / RAM chart, network, free space per filesystem.
// Details window: history charts, Proxmox guests, Docker containers, disks (SMART), filesystems, network.
(() => {
  const pct = (used, total) => (total ? Math.round(used / total * 100) : null);
  const hours = n => (n == null ? null : `${Math.round(n).toLocaleString("en-GB")} h`);

  // Containers: running and healthy = fine; unhealthy while running, restarting, or given up by an on-failure
  // policy = attention (the same rules as alerts() in card.py). Stopped on purpose = plain "exited".
  const ctBad = c => (c.up && c.health === "unhealthy") || c.restarting || (!c.up && c.exit !== 0 && c.policy === "on-failure");

  Cards.register("node", {
    render(body, d, h) {
      const memPct = pct(d.mem.used, d.mem.total);
      const stats = h.stats([
        ["CPU", d.cpu == null ? "–" : [Math.round(d.cpu * 100), "%"]],
        ["RAM", memPct == null ? "–" : [memPct, "%"]],
        d.temp == null ? ["Load", d.load == null ? "–" : d.load.toFixed(2)] : ["CPU temp", [Math.round(d.temp), "°C"]],  // a VM has no sensors
        ["Uptime", h.duration(d.uptime)],
      ]);
      stats.classList.add("four");  // always one row of four, also in a narrow card
      stats.children[1].dataset.hint = `${h.bytes(d.mem.used).join(" ")} of ${h.bytes(d.mem.total).join(" ")}`;
      stats.children[0].dataset.hint = `${d.cores} cores · load ${d.load == null ? "–" : d.load.toFixed(2)}`;
      // The CPU and RAM labels carry the series keys, like Download / Upload on the Internet card: no separate legend
      stats.children[0].querySelector(".label").prepend(h.el("i", "key s1"));
      stats.children[1].querySelector(".label").prepend(h.el("i", "key s2"));
      body.append(stats);
      // CPU and RAM over the last 30 minutes: same unit, one 0-100 % axis
      // (from the first recorded point: a fresh install has no flat line before its data)
      const from = d.history ? d.history.mem.findIndex(v => v != null) : -1;
      if (from >= 0 && d.history.mem.length - from > 1) {
        body.append(h.chart("load", [
          { key: "s1", label: "CPU", values: d.history.cpu.slice(from) },
          { key: "s2", label: "RAM", values: d.history.mem.slice(from) },
        ], { step: d.history.step, max: 100, format: v => [Math.round(v), "%"] }));
      }
      // Network right under the chart, like Received / Sent under the Internet card's chart
      const nets = d.net.filter(n => n.rx != null);
      if (nets.length) {
        const line = h.el("p", "srv-line node-net");
        nets.forEach((n, i) => {
          if (i) line.append(" · ");
          line.append(`${nets.length > 1 ? n.dev + " " : ""}↓ `, h.el("strong", null, h.byteRate(n.rx).join(" ")), "  ↑ ", h.el("strong", null, h.byteRate(n.tx).join(" ")));
        });
        line.dataset.hint = nets.map(n => `${n.dev}${n.speed ? ` · link ${Math.round(n.speed * 8 / 1e6)} Mbit/s` : ""}`).join("\n");
        body.append(line);
      }

      // Disks, guests, containers and failed services are in the ⤢ window; their problems show as alerts next to "Live".
      // Filesystems: free space first (that is what runs out), the share used after it
      const fsList = h.el("div", "node-disks");
      for (const f of d.fs) {
        const row = h.el("div", "node-disk"), used = pct(f.used, f.size);
        row.append(h.el("i", `sdot ${f.level}`), h.el("span", "node-name", f.mount),  // level: the thresholds of the config
                   h.el("span", "node-sub", `${h.bytes(f.size - f.used).join(" ")} free · ${used} %`));
        row.dataset.hint = `${h.bytes(f.used).join(" ")} used of ${h.bytes(f.size).join(" ")}`;
        fsList.append(row);
      }
      if (d.fs.length) body.append(fsList);
    },

    details(body, d, h) {
      const section = (title, node) => { body.append(h.heading(title), node); };
      const trail = name => (d.trails || {})[name];
      // History: CPU and RAM (%), temperatures (°C), network (bytes/s): one chart per unit, never two scales
      const s = d.history.series, disks = d.disks.filter(x => x.temp != null).slice(0, 2);  // two series colours
      body.append(h.history([
        { title: "CPU and memory", max: 100, format: v => [Math.round(v), "%"],
          series: [{ key: "s1", label: "CPU", metric: s.cpu }, { key: "s2", label: "RAM", metric: s.mem }] },
        d.temp != null && { title: "CPU temperature", format: v => [Math.round(v), "°C"], series: [{ key: "s1", label: "CPU", metric: s.temp }] },
        disks.length && { title: "Disk temperature", format: v => [Math.round(v), "°C"],
                          series: disks.map((x, i) => ({ key: `s${i + 1}`, label: x.name, metric: x.series })) },
        d.net.length && { title: "Network", format: v => h.byteRate(v),
                          series: [{ key: "s1", label: "Download", metric: s.rx }, { key: "s2", label: "Upload", metric: s.tx }] },
      ].filter(Boolean)));
      if (d.guests) {
        section("Guests", h.table(["ID", "Name", "Type", "Status", "CPU", "CPU, 30 min", "RAM", "Uptime", "At boot"], d.guests.map(g => [
          g.vmid, g.name, g.type === "lxc" ? "LXC" : "VM", g.status,
          g.up ? `${Math.round(g.cpu * 100)} % of ${g.cpus}` : "–",
          g.up && trail(`g.${g.vmid}.cpu`) ? h.spark(trail(`g.${g.vmid}.cpu`), { unit: "%", key: "s1" }) : "–",
          g.up ? h.meter(g.mem_max ? g.mem / g.mem_max : 0, `${h.bytes(g.mem).join(" ")} / ${h.bytes(g.mem_max).join(" ")}`) : `– / ${h.bytes(g.mem_max).join(" ")}`,
          g.up ? h.duration(g.uptime) : "–", g.onboot ? "yes" : "no",
        ])));
      }
      if (d.containers) {
        section("Containers", h.table(["Name", "Image", "State", "Health", "CPU", "CPU, 30 min", "RAM", "Up for", "Restarts"], d.containers.map(c => [
          c.name, h.cut(c.image, 40), ctBad(c) ? h.warn(c.state === "exited" ? `exited (${c.exit})` : c.state) : c.state,
          c.health === "none" || !c.up ? "–" : c.health === "unhealthy" ? h.warn(c.health) : c.health,
          c.up ? `${(c.cpu * 100).toFixed(1)} %` : "–",
          c.up && trail(`ct.${c.name}.cpu`) ? h.spark(trail(`ct.${c.name}.cpu`), { unit: "%", key: "s1" }) : "–",
          c.up ? h.bytes(c.mem).join(" ") : "–", c.up ? h.duration(c.uptime) : "–", c.restarts,
        ])));
      }
      if (d.disks.length) {
        section("Disks (SMART)", h.table(["Disk", "Model", "Temp", "Power-on", "SMART", "Reallocated", "Pending", "Uncorrectable", "Life"], d.disks.map(x => [
          `/dev/${x.disk}`, h.cut([x.name, x.model].filter((v, i, a) => v && a.indexOf(v) === i).join(" · "), 50),
          x.temp == null ? "–" : `${Math.round(x.temp)} °C`, hours(x.hours) || "–",
          x.healthy == null ? "–" : x.healthy ? "ok" : h.warn("failing"),
          x.bad.reallocated_sector_ct ?? "–", x.bad.current_pending_sector ?? "–", x.bad.offline_uncorrectable ?? "–",
          x.life == null || x.ssd === false ? "–" : `${Math.round(x.life)} %`,
        ])));
        if (d.smart_age != null) body.append(h.note(`SMART data from ${h.duration(d.smart_age)} ago.`));
      }
      if (d.fs.length) {
        section("Filesystems", h.table(["Mount", "Used", "Free", "Size"], d.fs.map(f => [
          f.mount, h.meter(f.size ? f.used / f.size : 0, `${pct(f.used, f.size)} % · ${h.bytes(f.used).join(" ")}`),
          h.bytes(f.size - f.used).join(" "), h.bytes(f.size).join(" "),
        ])));
      }
      if (d.net.length) {
        section("Network", h.table(["Interface", "Link", "Download", "Upload"], d.net.map(n => [
          n.dev, n.speed ? `${Math.round(n.speed * 8 / 1e6)} Mbit/s` : "–",
          n.rx == null ? "–" : h.byteRate(n.rx).join(" "), n.tx == null ? "–" : h.byteRate(n.tx).join(" "),
        ])));
      }
      section("System", h.table(["", ""], [
        ["Failed services", d.failed.length ? h.warn(d.failed.join(", ")) : "none"],
        ["Memory errors (ECC)", `${d.ecc.correctable} corrected · ${d.ecc.uncorrectable} uncorrectable`],
        ["Load", `${d.load == null ? "–" : d.load.toFixed(2)} on ${d.cores} cores`],
      ]));
    },
  });
})();
