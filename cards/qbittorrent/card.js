// qBittorrent card: speeds and torrent counts on the card; the torrents (active, seeding, stopped) in the details window.
Cards.register("qbittorrent", {
  render(body, d, h) {
    const c = d.counts;
    body.append(
      h.stats([
        ["Download", h.byteRate(d.down)],
        ["Upload", h.byteRate(d.up)],
        ["Free", d.free == null ? ["–"] : h.bytes(d.free)],
      ]),
      h.line([
        `${c.downloading} downloading`,
        `${c.seeding} seeding`,
        c.stopped && `${c.stopped} stopped`,
        c.error && h.warn(`${c.error} errors`, d.errors.join("\n")),
      ]),
    );
  },
  // Tile of the qBittorrent service: download and upload speed (its colour follows the card's alerts)
  tile(d, h) {
    return `↓ ${h.byteRate(d.down).join(" ")} · ↑ ${h.byteRate(d.up).join(" ")}`;
  },
  // Details: active torrents first, then the idle seeding ones, then every stopped one
  details(body, d, h) {
    const section = (title, count) => body.append(h.heading(count == null ? title : `${title} · ${count}`));
    section("Active", d.active.length);
    if (!d.active.length) body.append(h.note("Nothing is downloading or uploading right now."));
    else body.append(h.table(["Torrent", "Progress", "↓", "↑", "ETA", "Size"], d.active.map(t => [
      h.cut(t.name, 60),
      h.meter(t.progress, `${Math.floor(t.progress * 100)} %`),
      h.byteRate(t.down).join(" "),
      h.byteRate(t.up).join(" "),
      t.progress >= 1 || t.eta >= 8640000 ? "–" : h.duration(t.eta),
      h.bytes(t.size).join(" "),
    ])));
    const idle = (title, list, stateCol) => {
      if (!list || !list.length) return;
      section(title, list.length);
      body.append(h.table(["Torrent", stateCol, "Ratio", "Size", "Category"], list.map(t => [
        h.cut(t.name, 70),
        t.state === "missingFiles" ? h.warn("missing files") : t.state === "error" ? h.warn("error")
          : t.progress < 1 ? h.meter(t.progress, `${Math.floor(t.progress * 100)} %`) : "complete",
        t.ratio.toFixed(2), h.bytes(t.size).join(" "), t.category || "–",
      ])));
    };
    idle("Seeding", d.seeding, "Progress");
    idle("Stopped", d.stopped, "State");
  },
});
