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
// UNREAD IS AN AXIS, not a sixth panel row. It lives here so it ANDs with
// the others exactly like any of them, clears with "clear all", and shows
// in the tokens row when active - no invisible filter. It is deliberately
// NOT added to drawPanel's rows: the count is already in the masthead and
// this costs no new chrome.
const AXES = ["fx", "jurisdiction", "ticker", "source", "type", "unread"];
const state = {
  sources: [], facets: null, tape: [], offsetHours: 10,
  // Filled from the server at boot. "en" and a null label are only the
  // shape; the real values are config, not defaults worth relying on.
  locale: "en", zoneLabel: null,
  f: { fx: new Set(), jurisdiction: new Set(), ticker: new Set(),
       source: new Set(), type: new Set(), unread: new Set() },
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
  // Kept, because the strip is derived from the same payload rather than
  // from a second route: sessions carry their segments as fractions of the
  // day and marks carry their positions on that same scale.
  state.ribbon = data;
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
  drawStrip(frac);
}

/* ---------------- the strip ---------------- */

// "in 45m", "in 2h 10m". Its own formatter and not `ago()`, which reads
// backwards from a timestamp - these are forward from a fraction of the
// day, and a sentence that says "45m ago" about something that has not
// happened would be worse than no sentence.
function until(fraction) {
  const mins = Math.max(0, Math.round(fraction * 1440));
  if (mins < 60) return t("strip.in_minutes", { n: mins });
  // "7h 0m" is a clumsy way to say seven hours, and the zh reading
  // "7小时0分钟后" is worse. The exact-hour case gets its own string
  // rather than a zero glued to the end of the general one.
  const h = Math.floor(mins / 60), m = mins % 60;
  return m ? t("strip.in_hours", { h, m }) : t("strip.in_hours_flat", { h });
}

// EVERYTHING HERE COMES FROM state.ribbon AND THE CLOCK. No route, no
// query, no server-side view: the payload that draws the band already
// carries every session's segments as fractions of the day and every
// mark's position on the same scale, and tickClock already has the
// current fraction because the now-marker needs it. Two things that were
// already computed, read a second way.
function stripVenues(frac) {
  const open = [], soon = [], shut = [];
  for (const s of (state.ribbon.sessions || [])) {
    const segs = s.segments || [];
    const live = segs.find((g) => frac >= g.start && frac < g.end);
    if (live) { open.push({ s, time: live.closes_local }); continue; }
    // A session split by local midnight has two segments; the earliest one
    // that has not started yet is the next opening either way.
    const next = segs.filter((g) => g.start > frac).sort((a, b) => a.start - b.start)[0];
    if (next) soon.push({ s, time: next.opens_local, in: next.start - frac });
    else shut.push({ s, time: segs.length ? segs[segs.length - 1].closes_local : null });
  }
  return { open, soon, shut };
}

function drawStrip(frac) {
  if (!state.ribbon) return;
  // The venue hue, in the strip, on the same four conditions style.css
  // sets out for the band and for the same reason: the set is bounded at
  // five, every dot sits beside its own code, hue repeats a name that is
  // already written rather than replacing one, and NO UNREAD MARKER
  // APPEARS HERE - nothing in this row competes with amber.
  const hues = { sydney: "--syd", tokyo: "--tyo", hongkong: "--hkg",
                 london: "--lon", newyork: "--nyc" };
  const dot = (s, dim) =>
    `<i class="sdot" style="background:var(${hues[s.key]})${dim ? ";opacity:.32" : ""}"></i>`;
  const parts = [];

  const { open, soon, shut } = stripVenues(frac);
  for (const v of open) {
    parts.push(`<span class="sv">${dot(v.s)}<b>${esc(v.s.label)}</b> `
      + `${esc(t("strip.open", { time: v.time }))}</span>`);
  }
  for (const v of soon) {
    parts.push(`<span class="sv">${dot(v.s)}<b>${esc(v.s.label)}</b> `
      + `${esc(v.time)} ${esc(until(v.in))}</span>`);
  }
  if (shut.length) {
    parts.push('<i class="sdiv"></i>');
    for (const v of shut) {
      parts.push(`<span class="sv off">${dot(v.s, true)}<b>${esc(v.s.label)}</b> `
        + `${esc(v.time ? t("strip.closed", { time: v.time })
                       : t("strip.closed_plain"))}</span>`);
    }
  }

  const next = (state.ribbon.marks || [])
    .filter((m) => m.position !== null && m.position > frac)
    .sort((a, b) => a.position - b.position)[0];
  if (next) {
    parts.push('<i class="sdiv"></i>');
    parts.push(`<span class="sv"><b>${esc(t("strip.next_mark"))}</b> `
      + `${esc(next.local_time)} ${esc(next.source.replace(/_/g, " ").split(" ")[0])} `
      + `${esc(until(next.position - frac))}</span>`);
  }

  const shown = !$("ribbon").hidden;
  parts.push(`<button class="fbtn sband" id="band-toggle"
      aria-expanded="${shown}" aria-controls="ribbon"><span>${esc(
      shown ? t("strip.hide_band") : t("strip.show_band"))}</span> <kbd>r</kbd></button>`);
  $("mast-strip").innerHTML = parts.join("");
  $("band-toggle").onclick = toggleBand;
}

// localStorage, NOT preferences.py. A preference is something the server
// renders differently - locale, timezone, window - and every one of those
// is consumed server-side, which is why they are in the database. This is
// a view toggle on a single-user local page that the server never sees and
// never needs to: a migration and a settings row would buy nothing.
const BAND_KEY = "macrowire.band";

function bandShouldShow() {
  try { return localStorage.getItem(BAND_KEY) === "open"; } catch (e) { return false; }
}

function toggleBand() {
  const band = $("ribbon");
  band.hidden = !band.hidden;
  try {
    localStorage.setItem(BAND_KEY, band.hidden ? "closed" : "open");
  } catch (e) { /* private window: the choice just does not outlive the tab */ }
  drawStrip(currentFraction());
}

function currentFraction() {
  const now = new Date();
  const syd = new Date(now.getTime()
    + (state.offsetHours * 3600 + now.getTimezoneOffset() * 60) * 1000);
  return (syd.getHours() * 3600 + syd.getMinutes() * 60 + syd.getSeconds()) / 86400;
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
  const shown = axis === "unread" ? t("filter.unread_only")
              : axis === "fx" ? t(`filter.fx_state.${value}`)
              : axis === "type" ? typeLabel(value)
              : axis === "source" ? value.replace(/_/g, " ") : value;
  const ax = t(`filter.short.${axis}`);
  return `<span class="token"><span class="ax">${ax}</span>${esc(shown)}
            <button data-axis="${axis}" data-value="${esc(value)}"
                    aria-label="${esc(t("filter.remove", { label: shown }))}">\u00d7</button></span>`;
}

function drawTokens() {
  const active = activeFilters();
  // The row this lives in is UNCONDITIONAL now. It used to hide itself when
  // nothing was filtered, to keep the masthead from taking tape; it shares
  // a row with the jurisdiction chips, which are always there, so there is
  // nothing left to hide. `.tokens:empty::before` carries
  // filter.none_active for the empty case.
  $("tokens").innerHTML = active.map(tokenHtml).join("");
  $("tokens").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { state.f[b.dataset.axis].delete(b.dataset.value); afterFilterChange(); };
  });
}

// THE MOST-USED AXIS, ALWAYS ON SCREEN. Jurisdiction cost `f`, find it in
// the panel, click, Esc - every time. Seven chips is one line.
//
// Renders from the same facets the panel reads and from the same Set in
// state.f. There is no bar-specific state: `chip()` asks state.f whether
// each one is pressed, exactly as the panel's copy does, and wireChips
// gives both the identical handler.
function drawJurBar() {
  const f = state.facets;
  if (!f) return;
  $("jur-chips").innerHTML = (f.jurisdiction || [])
    .map((x) => chip("jurisdiction", x.value, x.value, x)).join("");
  wireChips($("jur-chips"));
}

function matches(r) {
  const f = state.f;
  if (f.unread.size && !r.unread) return false;
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

  // Coverage boundaries, interleaved in chronological position. A boundary
  // is a fact about WHEN the record starts, so it belongs in the timeline
  // rather than in a banner - and it renders ONCE per source, at its own
  // date, not once per day of an uncovered range.
  const cov = state.coverage || { boundaries: [], complete: 0, total: 0 };
  const pending = [...cov.boundaries].sort((a, b) => (a.earliest < b.earliest ? 1 : -1));

  function boundaryHtml(b) {
    const src = b.source.replace(/_/g, " ");
    const cmd = `python -m macrowire backfill --source ${b.source}`;
    const title = t(`coverage.${b.state}_title`, { source: src });
    const body = t(`coverage.${b.state}_body`, {
      date: b.earliest, first: b.first_fetch || "\u2014", command: cmd });
    return `<div class="cbound cbound-${esc(b.state)}">
      <div class="cb-t">${esc(title)}</div>
      <div class="cb-b">${esc(body)}</div></div>`;
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
      const label = syd.toLocaleDateString(state.locale,
        { weekday: "long", day: "numeric", month: "long" });
      // Chrome weight, not accent. Amber means unread and only unread, and
      // a control that CLEARS unread must not be painted in the colour of
      // the thing it clears.
      html += `<div class="dayhead"><span>${label}</span>` +
        `<button class="markday" data-markday="${esc(key)}"` +
        ` title="${esc(t("tape.mark_day_title", { day: label }))}"` +
        `>${esc(t("tape.mark_day"))}</button></div>`;
    }
    // Anything whose coverage begins after this row's date has now been
    // passed; emit it here, once.
    const rowDay = syd.toISOString().slice(0, 10);
    while (pending.length && pending[0].earliest > rowDay) {
      html += boundaryHtml(pending.shift());
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
  // Anything still pending begins before the oldest row we hold.
  while (pending.length) html += boundaryHtml(pending.shift());
  html += endOfWindow(cov);
  $("tape").innerHTML = html;
  wireReadControls();
}

// READ IS EXPLICIT. It used to be a 1.8s dwell timer on an
// IntersectionObserver: anything 60% on screen for 1.8 seconds marked
// itself read. That works for exactly one behaviour - reading top to
// bottom, once - and quietly destroys the others. Scanning for one item
// cleared everything scrolled past on the way. Reading backwards through
// weeks cleared the weeks. Leaving the tab open on a screenful cleared the
// screenful. In every case the count went down without anybody reading
// anything, and unread stopped meaning unread.
//
// So nothing marks itself now. Three deliberate acts do it, and each one
// says what it will touch before it touches it.
//
// COLLAPSED GROUPS ARE STILL ONE THING. A row's data-ids carries every
// member id - 207 identical notices are one unread and mark as one - and
// every path below posts the whole list, which is what the dwell timer did
// too. That part was never the problem.
async function markRead(ids) {
  const flat = [...new Set(ids)].filter(Boolean);
  if (!flat.length) return;
  await post("/api/read", { ids: flat });
  // THE MODEL, NOT JUST THE DOM. The old code removed a class and left
  // state.tape saying `unread: true`. That was survivable while unread was
  // only a colour; it is not now that unread is a filter, because the next
  // redraw would bring the row back.
  const marked = new Set(flat);
  for (const row of state.tape || []) {
    if ((row.ids || []).some((id) => marked.has(id))) row.unread = false;
  }
  drawTape();
  await refreshUnread();
}

// Every id currently ON SCREEN, which means AFTER the active filters. The
// masthead control marks what you are looking at, so filtered to HK it
// touches HK and nothing else.
function visibleIds(predicate) {
  const out = [];
  for (const row of (state.tape || [])) {
    if (!matches(row) || !row.unread) continue;
    if (predicate && !predicate(row)) continue;
    out.push(...(row.ids || []));
  }
  return out;
}

function wireReadControls() {
  // A headline click marks that row. The link still opens: no
  // preventDefault, so the article behaves like a link and reading it is
  // what marks it.
  $("tape").querySelectorAll("article.item .hl").forEach((a) => {
    a.onclick = () => {
      const el = a.closest("article.item");
      if (el && el.classList.contains("unread")) {
        markRead((el.dataset.ids || "").split(","));
      }
    };
  });
  $("tape").querySelectorAll("[data-markday]").forEach((b) => {
    b.onclick = () => markRead(visibleIds((r) => dayKey(r) === b.dataset.markday));
  });
}

// One definition of "which day is this row in", used by the renderer and
// by the day control, so a header and its button can never disagree.
function dayKey(row) {
  return dayKeySydney(row.published_at).toDateString();
}

// MEASURE, never warn unconditionally: when nothing is missing this says
// so plainly rather than staying silent, which would be indistinguishable
// from the feature being broken.
function endOfWindow(cov) {
  const rows = cov.boundaries || [];
  const span = t("coverage.end_heading", {
    from_date: cov.window_start || "\u2014",
    to_date: new Date().toISOString().slice(0, 10) });
  if (!rows.length) {
    return `<div class="cend"><div class="cb-t">${esc(span)}</div>
      <div class="cb-b">${esc(t("coverage.end_complete", { n: cov.total }))}</div></div>`;
  }
  const lines = rows.map((b) => `<div class="cend-row">${esc(t("coverage.end_row", {
      source: b.source.replace(/_/g, " "), date: b.earliest,
      note: t(`coverage.note_${b.state}`) }))}</div>`).join("");
  const rest = cov.complete > 0
    ? `<div class="cb-b">${esc(t("coverage.end_rest_complete", { n: cov.complete }))}</div>`
    : "";
  return `<div class="cend"><div class="cb-t">${esc(span)}</div>
    <div class="cb-b">${esc(t("coverage.end_missing"))}</div>
    ${lines}${rest}</div>`;
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

// Above this many ticker chips, the axis gets a type-to-narrow box. A
// JUDGEMENT CALL, not a measurement: nobody has been watched scanning a
// row of these. It is roughly where a wrapped field stops being something
// you take in at a glance, and it is a constant so the next person can
// argue with one number instead of hunting a literal.
const NARROW_TICKER_AXIS_AT = 24;

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

  // KEPT, even though the same chips are in the masthead bar. An axis that
  // vanished from the panel would read as an axis that had been taken away,
  // and the panel is where a reader looks to see what can be filtered at
  // all. The note says where else it lives; the chips are the same Set.
  rows.push(["jurisdiction", t("filter.axis.jurisdiction"), f.jurisdiction.length
    ? f.jurisdiction.map((x) => chip("jurisdiction", x.value, x.value, x)).join("")
      + `<span class="fsub">${esc(t("filter.also_above"))}</span>`
    : `<span class="fempty">${esc(t("filter.empty_window"))}</span>`, f.jurisdiction.length]);

  // POPULATED-ONLY, like every other axis. A chip appears when pressing it
  // would return rows, so its ABSENCE carries information: no UK chip means
  // the Bank of England published nothing this month.
  //
  // This axis used to break that rule. It rendered every held ticker,
  // including ones that had published nothing, each with a delete button,
  // plus an add form - an editing need solved inside a filtering control,
  // and the reason the axis grew with holdings rather than with activity.
  // Fifty names meant fifty chips in a panel sized for filtering. Editing
  // is in the settings dialog now, which has no height ceiling to tune.
  //
  // The count is stated because a missing ticker must not read as a missing
  // HOLDING. "12 of 47" says the other 35 are still held and simply quiet.
  const held = state.watchlist || [];
  const shown = f.ticker;
  const tickerChips = shown.map((x) => chip("ticker", x.value, x.value, x)).join("");
  const narrow = shown.length > NARROW_TICKER_AXIS_AT
    ? `<input class="fnarrow" id="wl-narrow" type="search" autocomplete="off"
              spellcheck="false"
              placeholder="${esc(t("filter.narrow_placeholder"))}"
              aria-label="${esc(t("filter.narrow_label"))}">`
    : "";
  rows.push(["ticker", t("filter.axis.ticker"),
    held.length
      ? `<span class="fsub">${esc(t("filter.ticker_of_held",
            { shown: shown.length, held: held.length }))}</span>` + narrow
        + (tickerChips
           || `<span class="fempty">${esc(t("filter.empty_ticker"))}</span>`)
      : `<span class="fempty">${esc(t("filter.empty_watchlist"))}</span>`,
    shown.length]);

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

  // FIVE OPEN SECTIONS, NOT FIVE DISCLOSURES. These were <details> to save
  // vertical space; the panel scrolls instead. Everything the panel can do
  // is on screen the moment it opens - no expanding, no drilling, no click
  // to reveal - which is how the settings dialog already worked and the
  // reason it never grew this class of bug.
  //
  // The <details> also could not survive its own stylesheet. `.fgroup` is
  // `display: flex`, an AUTHOR rule, and author origin outranks the UA
  // origin that hides a closed disclosure's content - regardless of
  // specificity. So a closed axis laid its chips out anyway, outside the
  // panel's painted box and outside its scroll height. Exactly the cascade
  // trap the settings dialog hit with `.settings { display: flex }`.
  $("fgrid").innerHTML = rows.map(([axis, label, body, n]) => {
    const on = state.f[axis] ? state.f[axis].size : 0;
    return `<section class="fax">
      <div class="fhead"><span class="faxis">${esc(label)}</span>` +
      `<span class="fcount">${n}</span>` +
      (on ? `<span class="fon">${on} \u2713</span>` : "") +
      `</div><div class="fgroup">${body}</div></section>`;
  }).join("");

  wireNarrow();

  wireChips($("fgrid"));
}

// ONE DEFINITION, BOTH RENDERS. This was inside drawPanel and scoped to
// the panel's own chips. The masthead bar draws jurisdiction a second
// time, and a second copy of this handler is how the two would eventually
// come to mean slightly different things.
function wireChips(root) {
  root.querySelectorAll(".chip[data-axis]").forEach((b) => {
    b.onclick = () => {
      const set = state.f[b.dataset.axis];
      set.has(b.dataset.value) ? set.delete(b.dataset.value) : set.add(b.dataset.value);
      afterFilterChange({ keepPanel: true });
    };
  });
}

// DISPLAY ONLY. This hides chips; it never touches state.f.ticker, so
// what the tape shows is exactly what the pressed chips say whether the
// box is empty, typed into, or cleared. A narrowing control that quietly
// filtered would be a second, invisible filter sitting on top of the
// visible one.
function wireNarrow() {
  const input = $("wl-narrow");
  if (!input) return;
  input.oninput = () => {
    const q = input.value.trim().toUpperCase();
    $("fgrid").querySelectorAll('.chip[data-axis="ticker"]').forEach((b) => {
      // A PRESSED chip is never hidden. It is narrowing the tape, and a
      // filter you cannot see acting on rows you can is the drawer's own
      // problem in miniature - the thing this panel was rebuilt to stop.
      //
      // Asks the SET, not the rendered attribute. aria-pressed is derived
      // from state.f; reading it back would make the DOM the record and
      // the Set a cache of it, which is the second store this design does
      // not have.
      const pressed = state.f.ticker.has(b.dataset.value);
      b.hidden = Boolean(q) && !pressed
                 && !b.dataset.value.toUpperCase().includes(q);
    });
  };
}

function wireWatchlistControls() {
  $("settings-watchlist").querySelectorAll(".wl-del").forEach((b) => {
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
  state.facets = await get("/api/facets");
  drawTokens();
  drawJurBar();
  drawTape();
  // The panel too, even though the editing is in the dialog now: removing a
  // holding must take its chip away, and adding one that has already
  // published must bring a chip in.
  drawPanel(state.unread || {});
  drawWatchlistSettings();
  setWlMessage(t("watchlist.after_change", { message }), false);
  const input = $("wl-ticker");
  // Same reason: the dialog is open over a tape the reader is deep inside.
  if (input) input.focus({ preventScroll: true });
}

function afterFilterChange({ keepPanel = false } = {}) {
  drawTokens();
  drawJurBar();
  drawTape();
  if (!keepPanel) drawPanel(state.unread || {});
  else syncChipPressed();
}

// DOCUMENT, NOT #fgrid. The jurisdiction chips are rendered twice - in the
// masthead bar and in the panel - and this is the whole mechanism that
// keeps them agreeing: both renders read the same Set in state.f, and
// there is no sync code between them because there is nothing to sync.
// Scope this back to #fgrid and the two copies drift the moment one is
// clicked while the other is on screen.
function syncChipPressed() {
  document.querySelectorAll(".chip[data-axis]").forEach((b) => {
    b.setAttribute("aria-pressed", state.f[b.dataset.axis].has(b.dataset.value));
  });
}

function clearFilters() {
  AXES.forEach((a) => state.f[a].clear());
  afterFilterChange();
}

let lastFocus = null;
// preventScroll ON EVERY focus() THAT IS NOT THE USER NAVIGATING.
//
// This was the worst bug in the tool and it was not what it looked like.
// Opening the panel appeared to shove the tape down; measured, the panel's
// height moves the tape by ZERO - Firefox's scroll anchoring absorbs it
// exactly. What actually happened is that focus() scrolls its target into
// view, so focusing the first chip while three weeks deep threw the page
// 2,984 pixels back to the top.
//
// The Tab handler below deliberately does NOT use this: there, moving the
// viewport to the newly focused control is the correct behaviour, because
// the user is navigating.
// Anchor the floating panel to the bottom of the masthead, whatever height
// the masthead currently is - it grows by one line when filters are active.
function positionPanel() {
  const bar = $("masthead").getBoundingClientRect();
  $("fpanel").style.top = `${Math.max(0, Math.round(bar.bottom))}px`;
}

function openPanel() {
  lastFocus = document.activeElement;
  $("fpanel").hidden = false;
  positionPanel();
  $("fopen").setAttribute("aria-expanded", "true");
  drawPanel(state.unread || {});
  const first = $("fgrid").querySelector(".chip");
  (first || $("fclose")).focus({ preventScroll: true });
}
function closePanel() {
  $("fpanel").hidden = true;
  $("fopen").setAttribute("aria-expanded", "false");
  const back = (lastFocus && lastFocus.focus) ? lastFocus : $("fopen");
  back.focus({ preventScroll: true });
}
const panelOpen = () => !$("fpanel").hidden;

function wireKeyboard() {
  $("fopen").onclick = () => (panelOpen() ? closePanel() : openPanel());
  // <dialog> handles Esc, focus trapping and the backdrop itself.
  $("settings-open").onclick = openSettings;
  // Already drawn by drawRail; this only puts it on screen.
  $("health-open").onclick = () => $("health-dialog").showModal();
  $("fclose").onclick = closePanel;
  $("fclear").onclick = clearFilters;

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (typing) return;
    if (e.key === "Escape" && panelOpen()) { e.preventDefault(); closePanel(); return; }
    if (e.key === "f" && !e.metaKey && !e.ctrlKey) { e.preventDefault(); panelOpen() ? closePanel() : openPanel(); return; }
    if (e.key === "c" && !e.metaKey && !e.ctrlKey) { e.preventDefault(); clearFilters(); return; }
    // Not while ANY modal is up. showModal() traps focus but the keydown
    // still bubbles to document, so a reader on a button in a dialog would
    // otherwise toggle a band behind the backdrop. Written against
    // `dialog[open]` rather than naming the settings dialog, because
    // health is a second one now and a third would have been missed.
    if (e.key === "r" && !e.metaKey && !e.ctrlKey
        && !document.querySelector("dialog[open]")) {
      e.preventDefault(); toggleBand(); return;
    }
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
  const u = await get("/api/unread");
  state.unread = u;
  // Both numbers, together, each labelled. They were on two surfaces with
  // no scope stated - "7 unread" in the masthead and "61 80 51" in the
  // panel - and read as a contradiction. They are not: one counts unread,
  // the other counts the window, and now they say so side by side.
  // The catalogue is our own file, so a marked-up field is safe here.
  // THE COUNT IS THE CONTROL. It carries data-axis/data-value, so
  // wireChips gives it the identical handler every filter chip has and
  // syncChipPressed keeps its pressed state honest - one code path, not a
  // second copy for the masthead. `chip` is on it for the wiring and the
  // pressed treatment; `unread-toggle` puts the masthead's own type back.
  $("unread-total").innerHTML =
    `<button class="chip unread-toggle" data-axis="unread" data-value="unread"
             aria-pressed="${state.f.unread.has("unread")}"
             title="${esc(t("filter.unread_only_title"))}">`
    + t("app.unread_and_window", {
        unread: u.total ? `<b>${u.total}</b>` : "0",
        total: u.window_total ?? "\u2014",
      })
    + `</button>`
    + `<button class="markall" id="markall"
               title="${esc(t("app.mark_view_title"))}"
       >${esc(t("app.mark_view"))}</button>`;
  wireChips($("unread-total"));
  $("markall").onclick = () => markRead(visibleIds());
  if (panelOpen()) drawPanel(u);
}

/* ---------------- rail ---------------- */

// TWO FACTS, AND THEY MUST NOT BE CONFLATED. "fix 2026-08-21" and "as of
// 2026-08-18" rendered identically, six days apart, with staleness nowhere
// on the page - a reader had to do date arithmetic to know whether a
// number was current.
//
//   AGE is how old the value is. ALWAYS shown, in --ink-2. Not a verdict.
//   LATE is older than that series' OWN cadence, and is a verdict, so it
//   is only ever asserted where the source declared one. CoT is weekly:
//   six days is on time. RBA is daily: four days is not.
//
// A series with no cadence_days shows its age and is never called late. A
// guessed cadence would put --fault on a number nobody said was late,
// which is the same failure as a staleness threshold that cries wolf.
function daysSince(period) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(period || ""));
  if (!m) return null;
  // DATE minus DATE. The payload's periods are dates, so comparing one to
  // a clock reading would make the answer depend on the hour of the day.
  const then = Date.UTC(+m[1], +m[2] - 1, +m[3]);
  const now = new Date();
  const local = new Date(now.getTime()
    + (state.offsetHours * 3600 + now.getTimezoneOffset() * 60) * 1000);
  const today = Date.UTC(local.getFullYear(), local.getMonth(), local.getDate());
  return Math.max(0, Math.round((today - then) / 86400000));
}

function asOf(text, period, cadence) {
  const days = daysSince(period);
  if (days === null) return esc(text);
  const late = cadence !== null && cadence !== undefined && days > cadence;
  const age = days === 0 ? t("rail.age_today") : t("time.days", { n: days });
  return esc(text) + ` <span class="age${late ? " late" : ""}">${esc(age)}`
    + (late ? ` · ${esc(t("rail.late"))}` : "") + `</span>`;
}

function drawRail(d) {
  const sign = (v) => (v > 0 ? "+" : "");
  const cad = d.cadence || {};
  $("fx").innerHTML = d.fx.map((f) => `
      <div class="k">${esc(f.series)}</div>
      <div class="v">${f.value}</div>
      <div class="d">${f.change === null ? "\u2014"
        : sign(f.change) + f.change.toFixed(4) + " (" + sign(f.change_pct) + f.change_pct.toFixed(2) + "%)"}</div>`
  ).join("");
  $("fx-asof").innerHTML = d.fx.length
    ? asOf(t("rail.cny_asof", { period: d.fx[0].period, time: FACT.cfetsFix,
                                prior: d.fx[0].prior_period }),
           d.fx[0].period, cad.fx)
    : esc(t("rail.no_data"));

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
    $("sb-asof").innerHTML = asOf(t("rail.sb_asof", {
      period: sb.period, unit: sb.rows[0].unit,
      prior: sb.prior_period || "\u2014" }), sb.period, cad.southbound);
  } else {
    $("sb").innerHTML = "";
    $("sb-asof").innerHTML = esc(t("rail.no_data"));
  }

  const thou = (v) => (v === undefined || v === null) ? "\u2014"
    : (v > 0 ? "+" : "") + Math.round(v).toLocaleString(state.locale);
  $("cot").innerHTML = (d.cot || []).map((p) => `
      <div class="k">${esc(p.currency)}</div>
      <div class="v">${thou(p.net)}</div>
      <div class="d">${p.change_net === undefined ? "\u2014"
        : thou(p.change_net) + " " + t("rail.week")}</div>`
  ).join("");
  $("cot-asof").innerHTML = (d.cot && d.cot.length)
    ? asOf(t("rail.cot_asof", { period: d.cot[0].period,
                                day: t("rail.weekday.tue"),
                                release: `${t("rail.weekday.fri")} ${FACT.cotRelease}` }),
           d.cot[0].period, cad.cot)
    : esc(t("rail.no_data"));

  $("ecb").innerHTML = (d.ecb || []).map((f) => `
      <div class="k">${esc(f.series)}</div>
      <div class="v">${f.value}</div>
      <div class="d">${f.change === null || f.change === undefined ? "\u2014"
        : sign(f.change) + f.change.toFixed(4) + " (" + sign(f.change_pct) + f.change_pct.toFixed(2) + "%)"}</div>`
  ).join("");
  $("ecb-asof").innerHTML = (d.ecb && d.ecb.length)
    ? asOf(t("rail.ecb_asof", { period: d.ecb[0].period, time: FACT.ecbPublish,
                                base: FACT.ecbBase }),
           d.ecb[0].period, cad.ecb)
    : esc(t("rail.no_data"));

  $("rba").innerHTML = d.rba.map((r) => `
      <div class="k">${esc(r.series)}</div><div class="v">${r.value}</div>`
  ).join("");
  $("rba-asof").innerHTML = d.rba.length
    ? asOf(t("rail.rba_asof", { time: FACT.rbaFix, period: d.rba[0].period }),
           d.rba[0].period, cad.rba)
    : esc(t("rail.no_data"));

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
  // ONE PREDICATE, read by the rows and by the masthead indicator. It was
  // the same expression written twice, which is how an indicator comes to
  // read "all current" over a list of rows marked warn.
  const affected = (h) => h.state_severity === "bad" || h.state_severity === "warn";
  const row = (h) => {
    // Contact, not store. A gated source polling and finding nothing new is
    // alive; calling that "no success logged" was a false alarm on healthy
    // data, and false alarms teach you to ignore the real ones.
    const contact = ago(h.seconds_since_contact);
    // The state label now carries the meaning; this is just the timing.
    const parts = [contact === null ? t("time.never") : contact];
    // failure_kinds arrive already formatted; a null error_kind reads as
    // "unclassified" rather than leaking the string "None".
    const bad = affected(h);
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

  // ENABLED SOURCES ONLY, on both sides of the fraction. A disabled source
  // gets a row in the dialog - switched off is worth seeing - but it is
  // neither current nor failing, and counting it as current would describe
  // it as being kept up when nothing is even contacting it.
  const polled = d.health.filter((h) => h.enabled !== false);
  const n = polled.filter(affected).length;
  $("health-summary").textContent = n
    ? t("health.affected", { n, total: polled.length })
    : t("health.all_current", { n: polled.length });
  $("health-open").classList.toggle("bad", n > 0);

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

/* ---------------- settings ---------------- */

let settingsData = null;

function provenance(row, key, label) {
  // Which level answered, and a way back down to the floor. A preference
  // that cannot be removed is a one-way door, and sources.yaml has to stay
  // the thing underneath.
  if (row.source !== "preference") {
    return `<span class="prov">${esc(t("settings.from_config"))}</span>`;
  }
  const back = row.config_value === "" ? t("settings.unset") : row.config_value;
  return `<span class="prov">${esc(t("settings.from_preference"))}</span>` +
    ` <button class="sreset" data-reset="${esc(key)}"
        title="${esc(t("settings.reset_title", { label, value: back }))}"
        >${esc(t("settings.reset"))}</button>`;
}

function drawSettings(data) {
  settingsData = data;
  const p = data.preferences;
  const rows = [];

  const pick = (key, label, options, current) => {
    const opts = options.map(([v, text]) =>
      `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(text)}</option>`
    ).join("");
    rows.push([label,
      `<select data-pref="${esc(key)}">${opts}</select>` +
      provenance(p[key], key, label)]);
  };

  // Named in their OWN language. A switcher labelled in a language you
  // cannot read is useless to the person who needs it.
  pick("locale", t("settings.locale"),
       data.locales.map((l) => [l.code, l.name]), p.locale.value);

  // Not a 400-entry dropdown: detected, the five the band draws, UTC, and
  // a text field backed by a <datalist> the browser filters as you type.
  const tz = data.timezones;
  const quick = [["system", t("settings.timezone_detected", { zone: tz.detected })]]
    .concat(tz.quick.filter((z) => z !== tz.detected).map((z) => [z, z]));
  const known = quick.some(([v]) => v === p.timezone.value);
  rows.push([t("settings.timezone"),
    `<select data-pref="timezone">${
      quick.map(([v, text]) =>
        `<option value="${esc(v)}"${v === p.timezone.value ? " selected" : ""}
        >${esc(text)}</option>`).join("")
    }<option value="__other"${known ? "" : " selected"}
      >${esc(t("settings.timezone_other"))}</option></select>` +
    `<input type="text" list="tz-all" id="tz-other" spellcheck="false"
       value="${known ? "" : esc(p.timezone.value)}"
       ${known ? "hidden" : ""} aria-label="${esc(t("settings.timezone"))}">` +
    provenance(p.timezone, "timezone", t("settings.timezone")) +
    `<span class="snote">${esc(t("settings.timezone_note"))}</span>`]);

  pick("session_order", t("settings.session_order"),
       [["viewer", t("settings.session_viewer")],
        ["fixed", t("settings.session_fixed")]], p.session_order.value);
  pick("jurisdiction_order", t("settings.jurisdiction_order"),
       [["viewer", t("settings.jur_viewer")],
        ["alphabetical", t("settings.jur_alpha")]], p.jurisdiction_order.value);

  const auto = state.jurisdiction
    ? t("settings.jurisdiction_auto", { code: state.jurisdiction })
    : t("settings.jurisdiction_none");
  pick("jurisdiction", t("settings.jurisdiction"),
       [["", auto]].concat(data.jurisdictions.map((j) => [j, j])),
       p.jurisdiction.value);

  pick("window_days", t("settings.window_days"),
       data.window_choices.map((n) => [String(n), t("settings.window_value", { n })]),
       p.window_days.value);

  $("settings-viewer").innerHTML = rows.map(
    ([label, body]) => `<div class="lab">${esc(label)}</div><div class="val">${body}</div>`
  ).join("");

  $("settings-install-note").textContent =
    t("settings.install_note", { path: data.config_path });
  $("settings-install").innerHTML = data.install.map((r) => {
    const shown = r.unset ? t("settings.falls_back", { value: r.value }) : r.value;
    return `<div class="lab">${esc(r.key)}</div><div class="val${r.unset ? " unset" : ""}"
      >${esc(shown)}${r.note ? `<span class="rnote">${esc(r.note)}</span>` : ""}</div>`;
  }).join("");
  $("tz-all").innerHTML = tz.all.map((z) => `<option value="${esc(z)}">`).join("");

  drawWatchlistSettings();
  wireSettings();
}

// THE ONLY PLACE A HOLDING IS ADDED OR REMOVED. It was in the filter
// panel, where a list that grows without bound had to live inside a box
// with a height ceiling. A dialog has no ceiling to tune, and editing is
// not filtering.
//
// Every held ticker, whether or not it published: the filter panel is
// deliberately silent about the quiet ones, so this is where a reader
// confirms a holding is still on the list.
// WHICH MARKETS HAVE AN ANNOUNCEMENT SOURCE, asked of the enabled sources
// rather than written down here. `announces_for` is the market a source
// carries company announcements for, and it is null for everything that
// publishes on its own schedule - so AU reads as uncovered even though two
// AU sources are enabled, because a central bank does not publish company
// announcements.
//
// Derived on every draw. If a market ever gains a source, these strings
// stop applying to it without anyone editing them; if one is switched off
// in sources.yaml, they start.
function announcementMarkets() {
  return new Set((state.sources || [])
    .filter((s) => s.enabled && s.announces_for)
    .map((s) => s.announces_for));
}

const MARKETS = ["US", "CN", "AU", "HK", "JP", "UK", "EU"];

function drawWatchlistSettings() {
  const held = state.watchlist || [];
  const covered = announcementMarkets();
  const withItems = new Set(((state.facets && state.facets.ticker) || [])
    .map((x) => x.value));
  const rows = held.map((e) => {
    const live = withItems.has(e.ticker);
    // THREE STATES, not two. "nothing in this window" implies something
    // could have published; for a market with no source, nothing can, and
    // saying so is the same distinction the tape draws between nothing
    // yet and nothing ever. Evidence beats derivation: a ticker that has
    // somehow published reads as published whatever the source list says.
    const uncovered = !live && !covered.has(e.market);
    const label = live ? t("settings.watchlist_published")
                : uncovered ? t("settings.watchlist_no_source")
                : t("settings.watchlist_quiet");
    // Not --fault and not amber: a market nobody collects is a fact about
    // the record, not a fault, so the row keeps the quiet --chrome it
    // already had.
    const detail = uncovered
      ? ` title="${esc(t("settings.watchlist_no_source_detail"))}"` : "";
    return `<div class="swl-row">
      <span class="swl-t">${esc(e.ticker)}</span>
      <span class="swl-m">${esc(e.market)}</span>
      <span class="swl-s${live ? " live" : ""}"${detail}>${esc(label)}</span>
      <button class="wl-del" data-ticker="${esc(e.ticker)}"
              data-market="${esc(e.market)}"
              aria-label="${esc(t("watchlist.remove", { ticker: e.ticker }))}"
              title="${esc(t("watchlist.remove_title",
                             { ticker: e.ticker, market: e.market }))}"
              >−</button></div>`;
  }).join("");
  // Unchanged from the panel: same ids, same endpoint, same validation,
  // same .wl-msg status line. Only where it renders has moved.
  $("settings-watchlist").innerHTML = (rows
    || `<span class="fempty">${esc(t("filter.empty_watchlist"))}</span>`) + `
    <form class="wl-add" id="wl-add">
      <input id="wl-ticker" name="ticker" maxlength="12"
             placeholder="${esc(t("watchlist.ticker_placeholder"))}"
             aria-label="${esc(t("watchlist.ticker_label"))}"
             autocomplete="off" spellcheck="false">
      <select id="wl-market" aria-label="${esc(t("watchlist.market_label"))}">
        ${MARKETS.map((m) => `<option value="${m}">${m}${
          covered.has(m) ? "" : " — " + esc(t("settings.market_no_source"))
        }</option>`).join("")}
      </select>
      <button class="fbtn" type="submit">${esc(t("watchlist.add"))}</button>
      <span class="wl-msg" id="wl-msg" role="status" aria-live="polite"></span>
    </form>`;
  wireWatchlistControls();
}

function wireSettings() {
  const other = $("tz-other");
  $("settings-viewer").querySelectorAll("select[data-pref]").forEach((el) => {
    el.onchange = () => {
      if (el.dataset.pref === "timezone" && el.value === "__other") {
        other.hidden = false; other.focus();
        return;
      }
      if (el.dataset.pref === "timezone") { other.hidden = true; }
      savePreference(el.dataset.pref, el.value);
    };
  });
  if (other) {
    other.onchange = () => other.value && savePreference("timezone", other.value);
  }
  $("settings-viewer").querySelectorAll("[data-reset]").forEach((b) => {
    b.onclick = () => savePreference(b.dataset.reset, null);
  });
}

async function savePreference(key, value) {
  try {
    const result = await post("/api/settings", { key, value });
    settingsData.preferences = result.preferences;
    drawSettings(settingsData);
    // Language and timezone change what the SERVER renders, so the page is
    // redrawn from scratch rather than patched in place - a half-updated
    // interface is worse than a blink.
    await reloadEverything();
  } catch (e) {
    alert(e.message);
  }
}

async function reloadEverything() {
  const b = await get("/api/bootstrap");
  STRINGS = b.strings || {};
  state.locale = b.locale || "en";
  state.zoneLabel = b.now.label;
  state.offsetHours = b.now.offset;
  state.facets = b.facets;
  document.documentElement.lang = state.locale;
  applyStaticStrings();
  drawRibbon(await get("/api/ribbon"));
  state.tape = (await get("/api/tape")).items;
  drawTape(); drawTokens(); drawJurBar();
  await refreshUnread();
  drawRail(await get("/api/rail"));
}

async function openSettings() {
  drawSettings(await get("/api/settings"));
  $("settings").showModal();
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
  state.jurisdiction = b.now.jurisdiction;
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
  wireKeyboard(); drawTokens(); drawJurBar();
  drawHours();
  // Before the first tick, so the band's stored state is settled before
  // anything measures or paints it.
  $("ribbon").hidden = !bandShouldShow();
  tickClock(); setInterval(tickClock, 1000);
  drawRibbon(await get("/api/ribbon"));
  drawStrip(currentFraction());
  const tape = await get("/api/tape");
  state.tape = tape.items;
  state.coverage = tape.coverage;
  drawTape();
  await refreshUnread();
  drawRail(await get("/api/rail"));

  setInterval(async () => {
    const fresh = await get("/api/tape");
    state.tape = fresh.items;
    state.coverage = fresh.coverage;
    state.facets = await get("/api/facets");
    drawTape(); refreshUnread();
    drawRail(await get("/api/rail"));
  }, 120000);
  window.addEventListener("resize", () => {
    drawHours(); layoutMarkLanes();
    if (panelOpen()) positionPanel();
  });
  // The masthead is sticky, so its bottom edge moves as the page scrolls
  // until it pins. An open panel follows it.
  window.addEventListener("scroll", () => {
    if (panelOpen()) positionPanel();
  }, { passive: true });
}

boot().catch((e) => {
  // t() is safe before the catalogue loads: it falls back to the key, which
  // is still more use than a blank page.
  $("tape").innerHTML = `<div class="note">${esc(t("app.failed", { message: e.message }))}</div>`;
});
