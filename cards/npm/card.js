// Nginx Proxy Manager: the tile counts proxy hosts and the days until the first certificate expires;
// the details list the certificates and every proxy host with its target.
(() => {
  const days = (d, c) => (c.expires == null ? null : Math.floor((c.expires - d.now) / 86400));
  Cards.register("npm", {
    tile(d) {
      const first = d.certs[0], left = first ? days(d, first) : null, down = d.hosts.filter(h => h.enabled && !h.online).length;
      // its colour follows the card's alerts (certificate expiry, hosts nginx does not serve)
      return [`${d.hosts.length} hosts`, down && `${down} offline`, left != null && (left < 0 ? "cert expired" : `cert ${left} d`)].filter(Boolean).join(" · ");
    },
    render(body, d, h) {
      const first = d.certs[0], left = first ? days(d, first) : null;
      body.append(h.stats([
        ["Proxy hosts", String(d.hosts.length)],
        ["Offline", String(d.hosts.filter(x => x.enabled && !x.online).length)],
        ["Certificate", left == null ? "–" : [left, "days"]],
      ]));
    },
    details(body, d, h) {
      body.append(h.heading(`Certificates · ${d.certs.length}`), h.table(["Certificate", "Provider", "Expires", "In"], d.certs.map(c => {
        const n = days(d, c);
        return [c.name, c.provider, c.expires ? new Date(c.expires * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "–",
                n == null ? h.warn("unknown") : c.level ? h.warn(n < 0 ? "expired" : `${n} days`) : `${n} days`];
      })));
      body.append(h.heading(`Proxy hosts · ${d.hosts.length}`), h.table(["Domain", "Forwards to", "Certificate", "State"], d.hosts.map(x => [
        x.domains.join(", "), x.target, x.cert || "–",
        !x.enabled ? "disabled" : x.online ? "online" : h.warn(x.error ? `offline: ${x.error}` : "offline"),
      ])));
      if (d.redirects || d.streams) body.append(h.note(`Also ${d.redirects} redirection host(s) and ${d.streams} stream(s).`));
    },
  });
})();
