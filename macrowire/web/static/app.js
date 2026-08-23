"use strict";

// Sydney is the only timezone the page thinks in. The server resolves every
// instant with zoneinfo and hands back positions already projected; the
// client never does timezone arithmetic of its own, because doing it in two
// places is how the two drift apart.

const $ = (id) => document.getElementById(id);

// Viewer-facing strings arrive with the bootstrap payload, already resolved
// against the default locale, so a missing key cannot reach here as
// `undefined`. If one somehow does, show the key: an ugly label you can see
// beats an invisible one you cannot.
let STRINGS = {};
function t(key, fields) {
  let node = STRINGS;
  for (const part of key.split(".")) {
    if (!node || typeof node !== "object" || !(part in node)) { node = null; break; }
    node = node[part];
  }
  if (typeof node !== "string") { console.error("missing string", key); return key; }
  return fields
    ? node.replace(/\{(\w+)\}/g, (m, name) => (name in fields ? fields[name] : m))
    : node;
}

// SOURCE FACTS, not interface text. These state when a publisher publishes.
// The RBA fixes at 4pm Sydney time whether you read this in Sydney or in
// Stuttgart, so translating them would not localise the page, it would
// falsify it. They sit here, outside the catalogue, deliberately.
const FACT = {
  cfetsFix: "09:15 CST",
  cotRelease: "15:30 ET",
  ecbPublish: "~16:00 CET",
  ecbBase: "EUR",
  rbaFix: "4pm AEST",
};
// The SCHEDULE is the fact - positions as of Tuesday, released Friday
// 15:30 ET. The weekday NAME is the reader's word for it, so it comes from
// the catalogue while the clock time does not. 周二 with the schedule
// intact loses nothing; 周二 with the hour changed would be a lie.
// One filter model, four axes. OR within an axis, AND across them.
const AXES = ["fx", "jurisdiction", "ticker", "source", "type"];
const state = {
  sources: [], facets: null, tape: [], offsetHours: 10,
  // Filled from the server at boot. "en" and a null label are only the
  // shape; the real values are config, not defaults worth relying on.
  locale: "en", zoneLabel: null,
  f: { fx: new Set(), jurisdiction: new Set(), ticker: new Set(),
       source: new Set(), type: new Set() },
};
const anyActive = () => AXES.some((a) => state.f[a].size);
// Where the READER is. Not a source's zone - those are in FACT.
const viewerZone = () => state.zoneLabel || t("ribbon.viewer_zone");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}
async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                               body: JSON.stringify(body) });
  if (r.ok) return r.json();
  // A 400 from the watchlist carries the same message the CLI prints; it is
  // the reason the call failed and must reach the user, not the console.
  let detail = `${r.status}`;
  try { detail = (await r.json()).detail || detail; } catch (_) {}
  const err = new Error(detail);
  err.status = r.status;
  throw err;
}

/* ---------------- ribbon ---------------- */

// The track is inset 34px for the venue labels, so an hour's position is
// its fraction of the remaining width, not of the whole band.
function atFraction(f) {
  return `calc(34px + ${f * 100}% - ${f * 34}px)`;
}

function drawHours() {
  $("hours").innerHTML = Array.from({ length: 9 }, (_, i) => {
    const h = i * 3;
    return `<span style="left:${atFraction(h / 24)}">${String(h).padStart(2, "0")}</span>`;
  }).join("");
}

function drawRibbon(data) {
  // The zone NAME comes from config via the server, not the catalogue: it
  // is a fact about this installation, and a translator has no business
  // turning "New York" into "Sydney". The catalogue holds the sentence
  // around it. ribbon.viewer_zone remains the fallback for a server that
  // has not sent one.
  $("ribbon-day").textContent = t("ribbon.day", { day: data.day, zone: viewerZone() });
  const colours = { sydney: "--syd", tokyo: "--tyo", hongkong: "--hkg",
                    london: "--lon", newyork: "--nyc" };

  $("sessions").innerHTML = data.sessions.map((s) => {
    const segs = s.segments.map((g) => {
      // A session split by local midnight is ONE session. Without a marker
      // the two pieces at opposite ends of the band read as a fault.
      const wrapInto = g.continues === "into" || g.continues === "both"
        ? `<span class="wrapmark" style="left:calc(${g.end * 100}% - 10px)">\u25b8</span>` : "";
      const wrapFrom = g.continues === "from" || g.continues === "both"
        ? `<span class="wrapmark" style="left:calc(${g.start * 100}% + 3px)">\u25c2</span>` : "";
      const title = t("ribbon.wrap_title", { label: s.label, open: g.opens_local,
                                             close: g.closes_local, zone: viewerZone() })
        + (g.continues ? " " + t("ribbon.wrap_continues") : "");
      return `<div class="seg" data-continues="${g.continues || ""}"
                   style="left:${g.start * 100}%;width:${(g.end - g.start) * 100}%;
                          background:var(${colours[s.key]})" title="${esc(title)}"></div>${wrapInto}${wrapFrom}`;
    }).join("");
    const hint = s.segments.length === 0
      ? `<span class="closed">${esc(t("ribbon.closed"))}</span>` : "";
    return `<div class="srow${s.weekend ? " weekend" : ""}">
              <span class="lab">${s.label}</span>
              <div class="track">${segs}${hint}</div>
            </div>`;
  }).join("");

  // Only fixed and tight sources get a position. Everything else is named
  // below the band rather than given a coordinate it does not have.
  const placed = data.marks.filter((m) => m.position !== null)
                           .sort((a, b) => a.position - b.position);
  $("marks-track").innerHTML = placed.map((m, i) => {
    const win = m.window > 0
      ? `<div class="win" style="left:0;width:${Math.max(m.window * 100, 0.4)}%"></div>` : "";
    const label = m.source.replace(/_/g, " ");
    const short = label.split(" ")[0];
    const tip = t("ribbon.mark_title", { source: label, local: m.local_time,
                                         zone: viewerZone(), origin: m.origin })
      + (m.shifts ? "\n" + t("ribbon.mark_shifts", { shifts: m.shifts }) : "")
      + (m.crosses_date ? "\n" + t("ribbon.mark_prev_day") : "");
    return `<div class="mark imp${m.importance}" data-i="${i}"
                 style="left:${m.position * 100}%" title="${esc(tip)}">
              <div class="stem"></div>${win}
              <div class="tag">${esc(m.local_time)} ${esc(short)}</div>
            </div>`;
  }).join("");
  layoutMarkLanes();

  const unplaced = data.marks.filter((m) => m.position === null);
  const byReason = {};
  unplaced.forEach((m) => { (byReason[m.reason] ||= []).push(m.source.replace(/_/g, " ")); });
  $("untimed").innerHTML = Object.entries(byReason).map(
    ([reason, names]) => `<b>${esc(t("ribbon.no_mark"))}</b> \u2014 `
      + esc(t("ribbon.untimed", { reason: t(reason), sources: names.join(", ") }))
  ).join("<br>");
}

// Marks near each other overprint their labels into mush. Measure each tag
// and push colliding ones onto lower lanes.
//
// Only the LABEL moves between lanes. The stem stays in the axis strip for
// every mark, whatever lane its label ends up on. Moving whole marks let a
// lower lane's stem reach up into an upper lane's text: NBS 11:30 sits
// 13.5px right of CFETS 11:15, which put its stem exactly inside the CFETS
// colon and rendered it as a corrupt glyph.
const AXIS_H = 11;   // stem zone; must exceed .mark .stem height
const LANE_H = 16;   // >= .mark .tag line-height

function layoutMarkLanes() {
  const track = $("marks-track");
  const marks = Array.from(track.querySelectorAll(".mark"));
  if (!marks.length) { $("marks").style.height = "0px"; return; }
  const PAD = 8;
  const lanes = [];
  for (const el of marks) {
    const tag = el.querySelector(".tag");
    if (!tag) continue;
    const left = el.offsetLeft;
    const right = left + tag.offsetWidth + PAD;
    let lane = 0;
    while (lane < lanes.length && lanes[lane] > left) lane += 1;
    if (lane === lanes.length) lanes.push(right); else lanes[lane] = right;
    tag.style.top = `${AXIS_H + lane * LANE_H}px`;
  }
  $("marks").style.height = `${AXIS_H + lanes.length * LANE_H + 4}px`;
}

function tickClock() {
  const now = new Date();
  // Server gave us Sydney's offset for this instant; apply it to UTC.
  const syd = new Date(now.getTime() + (state.offsetHours * 3600 + now.getTimezoneOffset() * 60) * 1000);
  const hh = String(syd.getHours()).padStart(2, "0");
  const mm = String(syd.getMinutes()).padStart(2, "0");
  const ss = String(syd.getSeconds()).padStart(2, "0");
  $("clock").innerHTML = `${hh}:${mm}:${ss}<span class="zone"> ${state.zone || ""}</span>`;
  const frac = (syd.getHours() * 3600 + syd.getMinutes() * 60 + syd.getSeconds()) / 86400;
  const line = $("nowline");
  line.hidden = false;
  line.style.left = atFraction(frac);
}

/* ---------------- tape ---------------- */

function dayKeySydney(iso) {
  const d = new Date(iso);
  const syd = new Date(d.getTime() + (state.offsetHours * 3600 + d.getTimezoneOffset() * 60) * 1000);
  return syd;
}

function activeFilters() {
  const out = [];
  for (const axis of AXES) for (const value of state.f[axis]) out.push({ axis, value });
  return out;
}

function typeLabel(token) {
  const [source, primary, tag] = token.split(":");
  const src = source.replace(/_/g, " ");
  return tag ? `${src} ${primary}:${tag}` : `${src} ${primary}`;
}

function tokenHtml({ axis, value }) {
  const shown = axis === "fx" ? t(`filter.fx_state.${value}`)
              : axis === "type" ? typeLabel(value)
              : axis === "source" ? value.replace(/_/g, " ") : value;
  const ax = t(`filter.short.${axis}`);
  return `<span class="token"><span class="ax">${ax}</span>${esc(shown)}
            <button data-axis="${axis}" data-value="${esc(value)}"
                    aria-label="${esc(t("filter.remove", { label: shown }))}">\u00d7</button></span>`;
}

function drawTokens() {
  const active = activeFilters();
  $("tokens").innerHTML = active.map(tokenHtml).join("");
  $("fclear").hidden = active.length === 0;
  $("tokens").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { state.f[b.dataset.axis].delete(b.dataset.value); afterFilterChange(); };
  });
}

function matches(r) {
  const f = state.f;
  if (f.fx.size && !f.fx.has(r.fx_state || "unclassified")) return false;
  if (f.jurisdiction.size && !f.jurisdiction.has(r.jurisdiction)) return false;
  if (f.ticker.size && !f.ticker.has(r.ticker)) return false;
  if (f.source.size && !f.source.has(r.source)) return false;
  if (f.type.size) {
    let hit = false;
    for (const token of f.type) {
      const [source, primary, tag] = token.split(":");
      if (r.source !== source || r.type_primary !== primary) continue;
      if (!tag) { hit = true; break; }
      if ((r.type_tags || "").split(",").includes(tag)) { hit = true; break; }
    }
    if (!hit) return false;
  }
  return true;
}

function drawTape() {
  const rows = state.tape.filter(matches);
  if (!rows.length) {
    // A filtered-to-nothing tape and a genuinely empty one must never look
    // alike. Restate the filters that produced the emptiness.
    $("tape").innerHTML = anyActive()
      ? `<div class="nomatch">
           <h3>${esc(t("tape.no_match_title"))}</h3>
           <div class="tokens">${activeFilters().map(tokenHtml).join("")}</div>
           <p>${esc(t("tape.no_match_body"))}</p>
           <p><button class="fbtn" id="nm-clear">${esc(t("tape.no_match_clear"))}</button></p>
         </div>`
      : state.sources.every((s) => !s.enabled)
      // Nothing enabled and nothing on the tape are the same picture and
      // completely different problems. Waiting for a fetch that will never
      // contact anything is the worse one, so it gets said out loud.
      ? `<div class="nomatch"><h3>${esc(t("tape.no_sources_title"))}</h3>
           <p>${esc(t("tape.no_sources_body"))}</p></div>`
      : `<div class="nomatch"><h3>${esc(t("tape.empty_title"))}</h3>
           <p>${esc(t("tape.empty_body"))}</p></div>`;
    const nm = $("nm-clear");
    if (nm) nm.onclick = clearFilters;
    $("tape").querySelectorAll(".token button").forEach((b) => {
      b.onclick = () => { state.f[b.dataset.axis].delete(b.dataset.value); afterFilterChange(); };
    });
    return;
  }

  let html = "", lastDay = null;
  for (const r of rows) {
    const syd = dayKeySydney(r.published_at);
    const key = syd.toDateString();
    if (key !== lastDay) {
      lastDay = key;
      // The VIEWER's locale, not a hardcoded en-AU. A day header is the
      // reader's own calendar; a publication time is not, and those live
      // in FACT rather than here.
      html += `<div class="dayhead">${syd.toLocaleDateString(state.locale,
        { weekday: "long", day: "numeric", month: "long" })}</div>`;
    }
    const hh = String(syd.getHours()).padStart(2, "0");
    const mm = String(syd.getMinutes()).padStart(2, "0");
    const count = r.count > 1 ? ` <span class="count">\u00d7${r.count}</span>` : "";
    const cat = r.announcement_type ? ` \u00b7 ${esc(r.announcement_type)}` : "";
    const jur = r.jurisdiction ? `<span class="jur">${esc(r.jurisdiction)}</span>` : "";
    const tkr = r.ticker ? `<span class="tkr">${esc(r.ticker)}</span>` : "";
    const sum = r.summary ? `<div class="sum">${esc(r.summary)}</div>` : "";
    const href = r.url ? `href="${esc(r.url)}" target="_blank" rel="noopener"` : "";
    html += `<article class="item imp${r.importance}${r.unread ? " unread" : ""}"
                      data-key="${esc(r.key)}" data-ids="${esc(r.ids.join(","))}">
        <div class="t">${hh}:${mm}</div>
        <div class="gut"></div>
        <div>
          <a class="hl" ${href}>${esc(r.title)}</a>
          <div class="meta">${jur}${tkr}<span class="src">${esc(r.source.replace(/_/g, " "))}</span>${cat}${count}</div>
          ${sum}
        </div>
      </article>`;
  }
  $("tape").innerHTML = html;
  observeItems();
}

// Mark read once an item has been on screen briefly. Collapsed groups mark
// every member: 207 identical notices are one thing you have now seen.
let observer = null;
function observeItems() {
  if (observer) observer.disconnect();
  const pending = new Map();
  observer = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const el = e.target, key = el.dataset.key;
      if (e.isIntersecting && el.classList.contains("unread")) {
        if (!pending.has(key)) {
          pending.set(key, setTimeout(() => {
            pending.delete(key);
            el.classList.remove("unread");
            post("/api/read", { ids: el.dataset.ids.split(",") }).then(refreshUnread);
          }, 1800));
        }
      } else if (pending.has(key)) {
        clearTimeout(pending.get(key)); pending.delete(key);
      }
    }
  }, { threshold: 0.6 });
  document.querySelectorAll(".item.unread").forEach((el) => observer.observe(el));
}

/* ---------------- filter panel ---------------- */

// TWO badges, never one, because the two numbers answer different questions
// and used to be told apart by nothing at all. The bucket count is how many
// are in the window; the attention count is how many of those are unread.
// Both are collapsed-group counts, so they are comparable.
//
//   bucket    neutral, ALWAYS rendered, including 0. Zero is a real bucket
//             size and an absent badge used to read as "no data" - HK had
//             70 items and printed a bare "HK" beside "CN 7".
//   attention amber, muted at 0. "Caught up" and "nothing here" must not
//             look alike, so 0 is shown quietly rather than dropped.
//   unknown   an em dash. Never a 0: a number nobody computed is not zero.
function countBadges(counts) {
  const c = counts || {};
  const bucket = c.count === null || c.count === undefined
    ? `<span class="n-bucket unknown" title="${esc(t("filter.count_unknown"))}">\u2014</span>`
    : `<span class="n-bucket">${c.count}</span>`;
  const unread = c.unread === null || c.unread === undefined
    ? ""
    : `<span class="n-unread${c.unread ? "" : " zero"}">${c.unread}</span>`;
  return bucket + unread;
}

function chip(axis, value, label, counts) {
  const on = state.f[axis].has(value);
  const c = counts || {};
  // The screen reader gets the scope spelled out; the sighted reader gets
  // it from the legend and the two treatments.
  const described = c.count === null || c.count === undefined
    ? t("filter.aria_unknown", { label })
    : t("filter.aria_counts", { label, total: c.count, unread: c.unread ?? 0 });
  return `<button class="chip" data-axis="${axis}" data-value="${esc(value)}"
                  aria-pressed="${on}" aria-label="${esc(described)}"
                  >${esc(label)}${countBadges(c)}</button>`;
}

function drawPanel(unread) {
  const f = state.facets;
  if (!f) return;
  const rows = [];

  // Three states, and unclassified is OFFERED rather than hidden - if you
  // filter to FX and something is missing, it must be findable.
  rows.push(["fx", t("filter.axis.fx"), (f.fx || []).length
    ? f.fx.map((x) => chip("fx", x.value, t(`filter.fx_state.${x.value}`), x)).join("")
      + `<span class="fsub">${esc(t("filter.fx_caveat"))}</span>`
    : `<span class="fempty">${esc(t("filter.empty_window"))}</span>`, (f.fx || []).length]);

  rows.push(["jurisdiction", t("filter.axis.jurisdiction"), f.jurisdiction.length
    ? f.jurisdiction.map((x) => chip("jurisdiction", x.value, x.value, x)).join("")
    : `<span class="fempty">${esc(t("filter.empty_window"))}</span>`, f.jurisdiction.length]);

  // Watchlist: filter chips for tickers with items, plus the held-but-quiet
  // ones so they can be removed, plus an add form. Editing lives here rather
  // than in a terminal.
  const held = state.watchlist || [];
  const withItems = new Set(f.ticker.map((x) => x.value));
  const wlChips = held.map((e) => {
    const hasItems = withItems.has(e.ticker);
    const entry = f.ticker.find((x) => x.value === e.ticker);
    const inner = hasItems
      ? chip("ticker", e.ticker, e.ticker, entry)
      : `<button class="chip" disabled title="${esc(t("watchlist.held_quiet"))}"
                 style="opacity:.62;cursor:default">${esc(e.ticker)}</button>`;
    return `<span class="wl-chip">${inner}<button class="wl-del"
              data-ticker="${esc(e.ticker)}" data-market="${esc(e.market)}"
              aria-label="${esc(t("watchlist.remove", { ticker: e.ticker }))}"
              title="${esc(t("watchlist.remove_title", { ticker: e.ticker, market: e.market }))}"
              >\u2212</button></span>`;
  }).join("");
  const wlForm = `
    <form class="wl-add" id="wl-add">
      <input id="wl-ticker" name="ticker" maxlength="12"
             placeholder="${esc(t("watchlist.ticker_placeholder"))}"
             aria-label="${esc(t("watchlist.ticker_label"))}" autocomplete="off" spellcheck="false">
      <select id="wl-market" aria-label="${esc(t("watchlist.market_label"))}">
        <option value="US">US</option><option value="CN">CN</option>
        <option value="AU">AU</option>
        <option value="HK">HK</option><option value="JP">JP</option>
        <option value="UK">UK</option><option value="EU">EU</option>
      </select>
      <button class="fbtn" type="submit">${esc(t("watchlist.add"))}</button>
      <span class="wl-msg" id="wl-msg" role="status" aria-live="polite"></span>
    </form>`;
  rows.push(["ticker", t("filter.axis.ticker"),
    (wlChips || `<span class="fempty">${esc(t("filter.empty_watchlist"))}</span>`) + wlForm,
    held.length]);

  rows.push(["source", t("filter.axis.source"), f.source.map((x) => chip("source", x.value,
      x.value.replace(/_/g, " "), x)).join(""), f.source.length]);

  // Type is scoped to its owning source. Sources with a single type are not
  // offered at all - their type IS their source, and a chip for it would
  // just duplicate the row above.
  rows.push(["type", t("filter.axis.type"), f.type.length
    ? f.type.map((g) => {
        const head = `<span class="fsub">${esc(g.source.replace(/_/g, " "))}</span>`;
        const prim = g.primary.map((x) =>
          chip("type", `${g.source}:${x.value}`, x.value, x)).join("");
        const tags = g.tags.length
          ? `<span class="fsub">${esc(t("filter.type_items"))}</span>` + g.tags.map((x) =>
              chip("type", `${g.source}:${x.value}`,
                   `${x.value.split(":")[1]} ${x.label}`, x)).join("")
          : "";
        return head + prim + tags;
      }).join("")
    : `<span class="fempty">${esc(t("filter.empty_type"))}</span>`, f.type.length]);

  // One <details> per axis, so the closed panel is five summary lines
  // rather than five wrapped chip fields. Native disclosure: keyboard
  // handling, screen-reader semantics and the open/closed state all come
  // free, and there is no state to keep in sync with anything.
  //
  // An axis holding an active filter opens itself. A collapsed section
  // hiding a filter that is narrowing the tape would be the drawer's own
  // problem in miniature: something acting on what you see, out of sight.
  $("fgrid").innerHTML = rows.map(([axis, label, body, n]) => {
    const on = state.f[axis] ? state.f[axis].size : 0;
    return `<details class="fax"${on ? " open" : ""}>
      <summary><span class="faxis">${esc(label)}</span>` +
      `<span class="fcount">${n}</span>` +
      (on ? `<span class="fon">${on} \u2713</span>` : "") +
      `</summary><div class="fgroup">${body}</div></details>`;
  }).join("");

  wireWatchlistControls();

  $("fgrid").querySelectorAll(".chip[data-axis]").forEach((b) => {
    b.onclick = () => {
      const set = state.f[b.dataset.axis];
      set.has(b.dataset.value) ? set.delete(b.dataset.value) : set.add(b.dataset.value);
      b.setAttribute("aria-pressed", set.has(b.dataset.value));
      afterFilterChange({ keepPanel: true });
    };
  });
}

function wireWatchlistControls() {
  $("fgrid").querySelectorAll(".wl-del").forEach((b) => {
    b.onclick = async () => {
      const { ticker, market } = b.dataset;
      b.disabled = true;
      try {
        const r = await post("/api/watchlist/remove", { ticker, market });
        state.watchlist = r.entries;
        // A removed ticker must stop filtering too, or the tape silently
        // narrows to a ticker no longer on the list.
        state.f.ticker.delete(ticker);
        await reloadAfterWatchlistChange(t("watchlist.removed", { ticker }));
      } catch (e) {
        setWlMessage(e.message, true);
        b.disabled = false;
      }
    };
  });

  const form = $("wl-add");
  if (!form) return;
  form.onsubmit = async (event) => {
    event.preventDefault();
    const ticker = $("wl-ticker").value.trim().toUpperCase();
    const market = $("wl-market").value;
    if (!ticker) { setWlMessage(t("watchlist.need_ticker"), true); return; }
    setWlMessage(t("watchlist.checking"), false);
    try {
      const r = await post("/api/watchlist/add", { ticker, market });
      state.watchlist = r.entries;
      $("wl-ticker").value = "";
      // The company name is what the SEC published; it is a source fact
      // and passes through untranslated.
      const name = r.added && r.added.name;
      await reloadAfterWatchlistChange(name
        ? t("watchlist.added_named", { market, ticker, name })
        : t("watchlist.added", { market, ticker }));
    } catch (e) {
      // The identical message the CLI prints, in front of the user.
      setWlMessage(e.message, true);
    }
  };
}

function setWlMessage(text, isError) {
  const el = $("wl-msg");
  if (!el) return;
  el.textContent = text;
  el.className = `wl-msg ${isError ? "err" : "ok"}`;
}

async function reloadAfterWatchlistChange(message) {
  state.facets = await get("/api/facets?days=30");
  drawTokens();
  drawTape();
  drawPanel(state.unread || {});
  setWlMessage(t("watchlist.after_change", { message }), false);
  const input = $("wl-ticker");
  if (input) input.focus();
}

function afterFilterChange({ keepPanel = false } = {}) {
  drawTokens();
  drawTape();
  if (!keepPanel) drawPanel(state.unread || {});
  else drawPanelPressedState();
}

function drawPanelPressedState() {
  $("fgrid").querySelectorAll(".chip[data-axis]").forEach((b) => {
    b.setAttribute("aria-pressed", state.f[b.dataset.axis].has(b.dataset.value));
  });
}

function clearFilters() {
  AXES.forEach((a) => state.f[a].clear());
  afterFilterChange();
}

let lastFocus = null;
function openPanel() {
  lastFocus = document.activeElement;
  $("fpanel").hidden = false;
  $("fopen").setAttribute("aria-expanded", "true");
  drawPanel(state.unread || {});
  const first = $("fgrid").querySelector(".chip");
  (first || $("fclose")).focus();
}
function closePanel() {
  $("fpanel").hidden = true;
  $("fopen").setAttribute("aria-expanded", "false");
  (lastFocus && lastFocus.focus) ? lastFocus.focus() : $("fopen").focus();
}
const panelOpen = () => !$("fpanel").hidden;

function wireKeyboard() {
  $("fopen").onclick = () => (panelOpen() ? closePanel() : openPanel());
  $("fclose").onclick = closePanel;
  $("fclear").onclick = clearFilters;

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (typing) return;
    if (e.key === "Escape" && panelOpen()) { e.preventDefault(); closePanel(); return; }
    if (e.key === "f" && !e.metaKey && !e.ctrlKey) { e.preventDefault(); panelOpen() ? closePanel() : openPanel(); return; }
    if (e.key === "c" && !e.metaKey && !e.ctrlKey) { e.preventDefault(); clearFilters(); return; }
    // Focus stays inside the panel while it is open.
    if (e.key === "Tab" && panelOpen()) {
      const focusable = $("fpanel").querySelectorAll(
        "button:not([disabled]), input, select");
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
}

async function refreshUnread() {
  const u = await get("/api/unread?days=30");
  state.unread = u;
  // Both numbers, together, each labelled. They were on two surfaces with
  // no scope stated - "7 unread" in the masthead and "61 80 51" in the
  // panel - and read as a contradiction. They are not: one counts unread,
  // the other counts the window, and now they say so side by side.
  // The catalogue is our own file, so a marked-up field is safe here.
  $("unread-total").innerHTML = t("app.unread_and_window", {
    unread: u.total ? `<b>${u.total}</b>` : "0",
    total: u.window_total ?? "\u2014",
  });
  if (panelOpen()) drawPanel(u);
}

/* ---------------- rail ---------------- */

function drawRail(d) {
  const sign = (v) => (v > 0 ? "+" : "");
  $("fx").innerHTML = d.fx.map((f) => `
      <div class="k">${esc(f.series)}</div>
      <div class="v">${f.value}</div>
      <div class="d">${f.change === null ? "\u2014"
        : sign(f.change) + f.change.toFixed(4) + " (" + sign(f.change_pct) + f.change_pct.toFixed(2) + "%)"}</div>`
  ).join("");
  $("fx-asof").textContent = d.fx.length
    ? t("rail.cny_asof", { period: d.fx[0].period, time: FACT.cfetsFix,
                           prior: d.fx[0].prior_period })
    : t("rail.no_data");

  // Southbound. Net leads because the direction is the signal; the level
  // is context. Sign is spelled out in words as well as shown, because a
  // minus sign in a dense rail is easy to miss and the two readings are
  // opposite.
  const sb = d.southbound;
  if (sb && sb.rows.length) {
    $("sb").innerHTML = sb.rows.map((r) => {
      const dir = r.key !== "net" ? ""
        : ` <span class="dir">${esc(t(r.value >= 0 ? "rail.sb_into" : "rail.sb_out"))}</span>`;
      return `
      <div class="k">${esc(t(`rail.sb.${r.key}`))}</div>
      <div class="v${r.key === "net" ? " lead" : ""}">${sign(r.value)}${r.value.toFixed(2)}${dir}</div>
      <div class="d">${r.change === null || r.change === undefined ? "\u2014"
        : sign(r.change) + r.change.toFixed(2)}</div>`;
    }).join("");
    $("sb-asof").textContent = t("rail.sb_asof", {
      period: sb.period, unit: sb.rows[0].unit, prior: sb.prior_period || "\u2014" });
  } else {
    $("sb").innerHTML = "";
    $("sb-asof").textContent = t("rail.no_data");
  }

  const thou = (v) => (v === undefined || v === null) ? "\u2014"
    : (v > 0 ? "+" : "") + Math.round(v).toLocaleString(state.locale);
  $("cot").innerHTML = (d.cot || []).map((p) => `
      <div class="k">${esc(p.currency)}</div>
      <div class="v">${thou(p.net)}</div>
      <div class="d">${p.change_net === undefined ? "\u2014"
        : thou(p.change_net) + " " + t("rail.week")}</div>`
  ).join("");
  $("cot-asof").textContent = (d.cot && d.cot.length)
    ? t("rail.cot_asof", { period: d.cot[0].period,
                           day: t("rail.weekday.tue"),
                           release: `${t("rail.weekday.fri")} ${FACT.cotRelease}` })
    : t("rail.no_data");

  $("ecb").innerHTML = (d.ecb || []).map((f) => `
      <div class="k">${esc(f.series)}</div>
      <div class="v">${f.value}</div>
      <div class="d">${f.change === null || f.change === undefined ? "\u2014"
        : sign(f.change) + f.change.toFixed(4) + " (" + sign(f.change_pct) + f.change_pct.toFixed(2) + "%)"}</div>`
  ).join("");
  $("ecb-asof").textContent = (d.ecb && d.ecb.length)
    ? t("rail.ecb_asof", { period: d.ecb[0].period, time: FACT.ecbPublish,
                           base: FACT.ecbBase })
    : t("rail.no_data");

  $("rba").innerHTML = d.rba.map((r) => `
      <div class="k">${esc(r.series)}</div><div class="v">${r.value}</div>`
  ).join("");
  $("rba-asof").textContent = d.rba.length
    ? t("rail.rba_asof", { time: FACT.rbaFix, period: d.rba[0].period })
    : t("rail.no_data");

  // From the server, not a second list here. This was hardcoded
  // ["AU","CN","HK","JP","US","EU","UK"] while the filter chips were
  // sorted alphabetically in SQL - the same axis in two orders, neither
  // aware of the other.
  const order = d.jurisdiction_order || [];
  const grouped = {};
  d.health.forEach((h) => { (grouped[h.jurisdiction] ||= []).push(h); });
  const ago = (s) => s === null ? null
    : s < 90 ? t("time.just_now")
    : s < 3600 ? t("time.minutes", { n: Math.floor(s / 60) })
    : s < 86400 ? t("time.hours", { n: Math.floor(s / 3600) })
    : t("time.days", { n: Math.floor(s / 86400) });
  const sinceText = (iso) => iso === null || iso === undefined
    ? t("time.unknown")
    : ago((Date.now() - new Date(iso).getTime()) / 1000);
  const row = (h) => {
    // Contact, not store. A gated source polling and finding nothing new is
    // alive; calling that "no success logged" was a false alarm on healthy
    // data, and false alarms teach you to ignore the real ones.
    const contact = ago(h.seconds_since_contact);
    // The state label now carries the meaning; this is just the timing.
    const parts = [contact === null ? t("time.never") : contact];
    // failure_kinds arrive already formatted; a null error_kind reads as
    // "unclassified" rather than leaking the string "None".
    const bad = h.state_severity === "bad" || h.state_severity === "warn";
    if (h.consecutive_failures) parts.push(`${h.failure_kinds.join(", ")}`);
    // The state label alone reads as a verdict on the source. For an
    // unreachable one it is not, so the qualification travels with it.
    if (h.state === "unreachable") parts.push(t("health.unreachable.short"));
    if (h.stale) parts.push(t("health.stale_flag"));
    const tip = [h.state_meaning, h.state_action].filter(Boolean).join("\n\n");
    return `<div><span class="nm">${esc(h.name.replace(/_/g, " "))}</span>
                 <span class="${bad ? "warn" : "ok"} st"
                       title="${esc(t("health.tip", { label: h.state_label, detail: tip }))}"
                 >${esc(h.state_label)} \u00b7 ${esc(parts.join(" \u00b7 "))}</span></div>`;
  };
  const note = d.health_note
    ? `<div class="hnote">${esc(d.health_note)}</div>` : "";
  $("health").innerHTML = note + order.filter((j) => grouped[j]).map(
    (j) => `<span class="jgroup">${esc(j)}</span>` + grouped[j].map(row).join("")
  ).join("");

  const risk = d.health.filter((h) => h.replaceable === "NO");
  const rows = risk.reduce((a, h) => a + h.at_risk, 0);
  $("risk").innerHTML =
    t("risk.only_here", { n: `<b>${rows}</b>`,
                          sources: esc(risk.map((h) => h.name.replace(/_/g, " ")).join(", ")) })
    + "<br>" + esc(t("risk.rest_refetchable"));

  // MEASURE, never warn unconditionally. If the rows are written off this
  // disk and up to date, say so - a panel that nags at a solved problem is
  // one you stop reading, and then it cannot warn you about a real one.
  const x = d.export || {};
  const el = $("export-state");
  if (x.error) {
    el.className = "risk unsolved";
    el.innerHTML = t("risk.export_misconfigured", { error: esc(x.error) });
  } else if (!x.exists) {
    el.className = "risk unsolved";
    el.innerHTML = t("risk.export_never", { command: "<b>macrowire export</b>",
                                            setting: "<b>export.path</b>" });
  } else if (x.external && x.current) {
    el.className = "risk solved";
    el.innerHTML = t("risk.export_protected", { path: `<b>${esc(x.directory)}</b>`,
                                                n: `<b>${x.rows}</b>`,
                                                when: esc(sinceText(x.written_at)) });
  } else if (x.external) {
    el.className = "risk unsolved";
    el.innerHTML = t("risk.export_stale", { path: `<b>${esc(x.directory)}</b>`,
                                            fetch: "<b>macrowire fetch</b>",
                                            export: "<b>macrowire export</b>" });
  } else {
    el.className = "risk unsolved";
    el.innerHTML = t("risk.export_local", { n: `<b>${x.rows}</b>`,
                                            path: `<b>${esc(x.directory)}</b>`,
                                            stale: x.current ? "" : t("risk.export_local_stale"),
                                            setting: "<b>export.path</b>" });
  }
}

/* ---------------- boot ---------------- */

// Static chrome in index.html carries a key, not a sentence, so the markup
// has one language and the catalogue has the rest. The elements ship empty
// and are filled here, before anything else draws.
function applyStaticStrings() {
  document.title = t("app.title");
  // The empty-filter hint is drawn by CSS on `.tokens:empty`, which cannot
  // reach the catalogue. Hand it the string as a custom property instead of
  // leaving one sentence stranded in the stylesheet.
  document.documentElement.style.setProperty(
    "--empty-filters", JSON.stringify(t("filter.none_active")));
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-label]").forEach((el) => {
    el.setAttribute("aria-label", t(el.dataset.i18nLabel));
  });
}

async function boot() {
  const b = await get("/api/bootstrap");
  // Strings first: everything drawn below reads from them.
  STRINGS = b.strings || {};
  document.documentElement.lang = b.locale || "en";
  applyStaticStrings();
  state.sources = b.sources;
  state.offsetHours = b.now.offset;
  state.facets = b.facets;
  state.watchlist = b.watchlist || [];
  state.zone = b.now.zone;
  state.zoneLabel = b.now.label;
  state.locale = b.locale || "en";
  if (b.first_run) {
    // bootstrap only REPORTS this; the sweep is a POST so a GET never mutates.
    const swept = await post("/api/first-run", {});
    if (swept && swept.marked) {
      const n = $("firstrun");
      n.hidden = false;
      n.textContent = t("app.first_run", { n: swept.marked });
    }
  }
  wireKeyboard(); drawTokens();
  drawHours(); tickClock(); setInterval(tickClock, 1000);
  drawRibbon(await get("/api/ribbon"));
  state.tape = (await get("/api/tape?days=30")).items;
  drawTape();
  await refreshUnread();
  drawRail(await get("/api/rail"));

  setInterval(async () => {
    state.tape = (await get("/api/tape?days=30")).items;
    state.facets = await get("/api/facets?days=30");
    drawTape(); refreshUnread();
    drawRail(await get("/api/rail"));
  }, 120000);
  window.addEventListener("resize", () => { drawHours(); layoutMarkLanes(); });
}

boot().catch((e) => {
  // t() is safe before the catalogue loads: it falls back to the key, which
  // is still more use than a blank page.
  $("tape").innerHTML = `<div class="note">${esc(t("app.failed", { message: e.message }))}</div>`;
});
