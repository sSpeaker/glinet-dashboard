// Home Assistant: the chosen values on the tile; all chosen entities, updates and counts in the details.
Cards.register("homeassistant", {
  tile: d => d.tile.filter(v => v && v !== "not found").join(" · "),
  render(body, d, h) {
    body.append(h.stats(d.entities.slice(0, 4).map(e => [e.name.replace(/ (temperature|humidity)$/i, ""), e.value])));
  },
  details(body, d, h) {
    if (d.entities.length) {
      body.append(h.table(["Entity", "Value", "Changed"], d.entities.map(e => [
        e.name, e.value === "unavailable" || e.value === "not found" ? h.warn(e.value) : e.value,
        e.changed ? `${h.duration((Date.now() - Date.parse(e.changed)) / 1000)} ago` : "–",
      ])));
    } else body.append(h.note("No entities chosen: list them under entities: in the widget's config."));
    body.append(h.heading(`Updates · ${d.updates.length}`));
    body.append(d.updates.length ? h.table(["Component", "New version"], d.updates.map(u => [u.name, u.version || "–"])) : h.note("Everything is up to date."));
    body.append(h.note(`Home Assistant ${d.version} · ${d.total} entities, ${d.unavailable} unavailable.`));
  },
});
