"use strict";

// Sydney is the only timezone the page thinks in. The server resolves every
// instant with zoneinfo and hands back positions already projected; the
// client never does timezone arithmetic of its own, because doing it in two
// places is how the two drift apart.

const $ = (id) => document.getElementById(id);
// One filter model, four axes. OR within an axis, AND across them.
const AXES = ["fx", "jurisdiction", "ticker", "source", "type"];
const state = {
  sources: [], facets: null, tape: [], offsetHours: 10,
  f: { fx: new Set(), jurisdiction: new Set(), ticker: new Set(),
       source: new Set(), type: new Set() },
};
const anyActive = () => AXES.some((a) => state.f[a].size);

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
  $("ribbon-day").textContent = data.day + " \u00b7 Sydney";
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
      const title = `${s.label} ${g.opens_local}\u2013${g.closes_local} Sydney`
        + (g.continues ? " (continues across local midnight)" : "");
      return `<div class="seg" data-continues="${g.continues || ""}"
                   style="left:${g.start * 100}%;width:${(g.end - g.start) * 100}%;
                          background:var(${colours[s.key]})" title="${esc(title)}"></div>${wrapInto}${wrapFrom}`;
    }).join("");
    const hint = s.segments.length === 0
      ? `<span class="closed">closed</span>` : "";
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
    const tip = `${label} \u2014 ${m.local_time} Sydney, published ${m.origin}`
      + (m.shifts ? `\nacross the year: ${m.shifts}` : "")
      + (m.crosses_date ? "\npublished the previous day at origin" : "");
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
    ([reason, names]) => `<b>no mark</b> \u2014 ${esc(reason)}: ${esc(names.join(", "))}`
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
  const FX_LABEL = { fx: "FX-relevant", not_fx: "not FX", unclassified: "unclassified" };
  const shown = axis === "fx" ? (FX_LABEL[value] || value)
              : axis === "type" ? typeLabel(value)
              : axis === "source" ? value.replace(/_/g, " ") : value;
  const ax = { fx: "FX", jurisdiction: "JUR", ticker: "TKR",
               source: "SRC", type: "TYPE" }[axis];
  return `<span class="token"><span class="ax">${ax}</span>${esc(shown)}
            <button data-axis="${axis}" data-value="${esc(value)}"
                    aria-label="Remove filter ${esc(shown)}">\u00d7</button></span>`;
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
           <h3>No items match these filters</h3>
           <div class="tokens">${activeFilters().map(tokenHtml).join("")}</div>
           <p>Filters combine as OR within a row and AND across rows,
              so narrowing two axes at once can leave nothing.</p>
           <p><button class="fbtn" id="nm-clear">clear all filters</button></p>
         </div>`
      : `<div class="nomatch"><h3>Nothing in this window</h3>
           <p>No filters are active \u2014 the last 30 days are genuinely empty.</p></div>`;
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
      html += `<div class="dayhead">${syd.toLocaleDateString("en-AU",
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

function chip(axis, value, label, count) {
  const on = state.f[axis].has(value);
  return `<button class="chip" data-axis="${axis}" data-value="${esc(value)}"
                  aria-pressed="${on}">${esc(label)}${
            count ? `<span class="n">${count}</span>` : ""}</button>`;
}

function drawPanel(unread) {
  const f = state.facets;
  if (!f) return;
  const rows = [];

  // Three states, and unclassified is OFFERED rather than hidden - if you
  // filter to FX and something is missing, it must be findable.
  const FXL = { fx: "FX-relevant", not_fx: "not FX", unclassified: "unclassified" };
  rows.push(["FX", (f.fx || []).length
    ? f.fx.map((x) => chip("fx", x.value, FXL[x.value] || x.value, x.count)).join("")
      + `<span class="fsub">unclassified means no rule matched, never "not FX"</span>`
    : `<span class="fempty">nothing in this window</span>`]);

  rows.push(["Jurisdiction", f.jurisdiction.length
    ? f.jurisdiction.map((x) => chip("jurisdiction", x.value, x.value,
        (unread.per_jurisdiction || {})[x.value] || 0)).join("")
    : `<span class="fempty">nothing in this window</span>`]);

  // Watchlist: filter chips for tickers with items, plus the held-but-quiet
  // ones so they can be removed, plus an add form. Editing lives here rather
  // than in a terminal.
  const held = state.watchlist || [];
  const withItems = new Set(f.ticker.map((x) => x.value));
  const wlChips = held.map((e) => {
    const hasItems = withItems.has(e.ticker);
    const count = (f.ticker.find((x) => x.value === e.ticker) || {}).count || 0;
    const inner = hasItems
      ? chip("ticker", e.ticker, e.ticker, count)
      : `<button class="chip" disabled title="held, but nothing in this window"
                 style="opacity:.62;cursor:default">${esc(e.ticker)}</button>`;
    return `<span class="wl-chip">${inner}<button class="wl-del"
              data-ticker="${esc(e.ticker)}" data-market="${esc(e.market)}"
              aria-label="Remove ${esc(e.ticker)} from watchlist"
              title="Remove ${esc(e.ticker)} (${esc(e.market)}) from the watchlist"
              >\u2212</button></span>`;
  }).join("");
  const wlForm = `
    <form class="wl-add" id="wl-add">
      <input id="wl-ticker" name="ticker" maxlength="12" placeholder="ticker"
             aria-label="Ticker to add" autocomplete="off" spellcheck="false">
      <select id="wl-market" aria-label="Market">
        <option value="US">US</option><option value="AU">AU</option>
        <option value="HK">HK</option><option value="JP">JP</option>
        <option value="UK">UK</option><option value="EU">EU</option>
      </select>
      <button class="fbtn" type="submit">add</button>
      <span class="wl-msg" id="wl-msg" role="status" aria-live="polite"></span>
    </form>`;
  rows.push(["Watchlist", (wlChips || `<span class="fempty">watchlist is empty</span>`) + wlForm]);

  rows.push(["Source", f.source.map((x) => chip("source", x.value,
      x.value.replace(/_/g, " "), (unread.per_source || {})[x.value] || 0)).join("")]);

  // Type is scoped to its owning source. Sources with a single type are not
  // offered at all - their type IS their source, and a chip for it would
  // just duplicate the row above.
  rows.push(["Type", f.type.length
    ? f.type.map((g) => {
        const head = `<span class="fsub">${esc(g.source.replace(/_/g, " "))}</span>`;
        const prim = g.primary.map((x) =>
          chip("type", `${g.source}:${x.value}`, x.value, x.count)).join("");
        const tags = g.tags.length
          ? `<span class="fsub">8-K items</span>` + g.tags.map((x) =>
              chip("type", `${g.source}:${x.value}`,
                   `${x.value.split(":")[1]} ${x.label}`, x.count)).join("")
          : "";
        return head + prim + tags;
      }).join("")
    : `<span class="fempty">no source in this window has more than one type</span>`]);

  $("fgrid").innerHTML = rows.map(
    ([label, body]) => `<div class="faxis">${label}</div><div class="fgroup">${body}</div>`
  ).join("");

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
        await reloadAfterWatchlistChange(`removed ${ticker}`);
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
    if (!ticker) { setWlMessage("enter a ticker", true); return; }
    setWlMessage("checking\u2026", false);
    try {
      const r = await post("/api/watchlist/add", { ticker, market });
      state.watchlist = r.entries;
      $("wl-ticker").value = "";
      const name = r.added && r.added.name ? ` \u2014 ${r.added.name}` : "";
      await reloadAfterWatchlistChange(`added ${market} ${ticker}${name}`);
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
  setWlMessage(message + ". Filings appear after the next fetch.", false);
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
  $("unread-total").innerHTML = u.total ? `<b>${u.total}</b> unread` : "all read";
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
    ? `fix ${d.fx[0].period} 09:15 CST · change vs ${d.fx[0].prior_period}` : "no data";

  const thou = (v) => (v === undefined || v === null) ? "\u2014"
    : (v > 0 ? "+" : "") + Math.round(v).toLocaleString("en-AU");
  $("cot").innerHTML = (d.cot || []).map((p) => `
      <div class="k">${esc(p.currency)}</div>
      <div class="v">${thou(p.net)}</div>
      <div class="d">${p.change_net === undefined ? "\u2014"
        : thou(p.change_net) + " wk"}</div>`
  ).join("");
  $("cot-asof").textContent = (d.cot && d.cot.length)
    ? `CFTC non-commercial net, contracts \u00b7 as of ${d.cot[0].period} (Tue), `
      + `released Fri 15:30 ET` : "no data";

  $("ecb").innerHTML = (d.ecb || []).map((f) => `
      <div class="k">${esc(f.series)}</div>
      <div class="v">${f.value}</div>
      <div class="d">${f.change === null || f.change === undefined ? "\u2014"
        : sign(f.change) + f.change.toFixed(4) + " (" + sign(f.change_pct) + f.change_pct.toFixed(2) + "%)"}</div>`
  ).join("");
  $("ecb-asof").textContent = (d.ecb && d.ecb.length)
    ? `reference rate ${d.ecb[0].period} \u00b7 ~16:00 CET \u00b7 base EUR` : "no data";

  $("rba").innerHTML = d.rba.map((r) => `
      <div class="k">${esc(r.series)}</div><div class="v">${r.value}</div>`
  ).join("");
  $("rba-asof").textContent = d.rba.length ? `4pm AEST · ${d.rba[0].period}` : "no data";

  const order = ["AU", "CN", "HK", "JP", "US", "EU", "UK"];
  const grouped = {};
  d.health.forEach((h) => { (grouped[h.jurisdiction] ||= []).push(h); });
  const sinceText = (iso) => {
    if (!iso) return "at an unknown time";
    const secs = (Date.now() - new Date(iso).getTime()) / 1000;
    return secs < 90 ? "just now"
      : secs < 3600 ? `${Math.floor(secs / 60)}m ago`
      : secs < 86400 ? `${Math.floor(secs / 3600)}h ago`
      : `${Math.floor(secs / 86400)}d ago`;
  };
  const ago = (s) => s === null ? null
    : s < 90 ? "just now"
    : s < 3600 ? `${Math.floor(s / 60)}m ago`
    : s < 86400 ? `${Math.floor(s / 3600)}h ago`
    : `${Math.floor(s / 86400)}d ago`;
  const row = (h) => {
    // Contact, not store. A gated source polling and finding nothing new is
    // alive; calling that "no success logged" was a false alarm on healthy
    // data, and false alarms teach you to ignore the real ones.
    const contact = ago(h.seconds_since_contact);
    // The state label now carries the meaning; this is just the timing.
    const parts = [contact === null ? "never" : contact];
    // failure_kinds arrive already formatted; a null error_kind reads as
    // "unclassified" rather than leaking the string "None".
    const bad = h.state_severity === "bad" || h.state_severity === "warn";
    if (h.consecutive_failures) parts.push(`${h.failure_kinds.join(", ")}`);
    if (h.stale) parts.push("stale");
    const tip = [h.state_meaning, h.state_action].filter(Boolean).join("\n\n");
    return `<div><span class="nm">${esc(h.name.replace(/_/g, " "))}</span>
                 <span class="${bad ? "warn" : "ok"} st" title="${esc(h.state_label)} \u2014 ${esc(tip)}"
                 >${esc(h.state_label)} \u00b7 ${esc(parts.join(" \u00b7 "))}</span></div>`;
  };
  $("health").innerHTML = order.filter((j) => grouped[j]).map(
    (j) => `<span class="jgroup">${esc(j)}</span>` + grouped[j].map(row).join("")
  ).join("");

  const risk = d.health.filter((h) => h.replaceable === "NO");
  const rows = risk.reduce((a, h) => a + h.at_risk, 0);
  $("risk").innerHTML =
    `<b>${rows}</b> row(s) exist only here — ${esc(risk.map((h) => h.name.replace(/_/g, " ")).join(", "))}.<br>` +
    `Everything else is re-fetchable: losing it costs polling time, not data.`;

  // MEASURE, never warn unconditionally. If the rows are written off this
  // disk and up to date, say so - a panel that nags at a solved problem is
  // one you stop reading, and then it cannot warn you about a real one.
  const x = d.export || {};
  const el = $("export-state");
  if (x.error) {
    el.className = "risk unsolved";
    el.innerHTML = `<b>export misconfigured</b> — ${esc(x.error)}`;
  } else if (!x.exists) {
    el.className = "risk unsolved";
    el.innerHTML = `<b>not exported yet</b> — run <b>macrowire export</b>, or `
      + `set <b>export.path</b> in sources.yaml to a synced folder and it happens automatically.`;
  } else if (x.external && x.current) {
    el.className = "risk solved";
    el.innerHTML = `Exporting to <b>${esc(x.directory)}</b> — `
      + `<b>${x.rows}</b> row(s) protected ${esc(sinceText(x.written_at))}. `
      + `Off this disk, so a drive failure costs nothing.`;
  } else if (x.external) {
    el.className = "risk unsolved";
    el.innerHTML = `Exporting to <b>${esc(x.directory)}</b>, but the file is `
      + `<b>out of date</b> — run <b>macrowire fetch</b> or <b>macrowire export</b>.`;
  } else {
    el.className = "risk unsolved";
    el.innerHTML = `<b>${x.rows}</b> row(s) exported to <b>${esc(x.directory)}</b>`
      + `${x.current ? "" : " (out of date)"} — that is the same disk as the `
      + `database, so it protects against a mistake but not a drive failure. `
      + `Set <b>export.path</b> in sources.yaml to a synced folder.`;
  }
}

/* ---------------- boot ---------------- */

async function boot() {
  const b = await get("/api/bootstrap");
  state.sources = b.sources;
  state.offsetHours = b.now.offset;
  state.facets = b.facets;
  state.watchlist = b.watchlist || [];
  state.zone = b.now.zone;
  if (b.first_run) {
    // bootstrap only REPORTS this; the sweep is a POST so a GET never mutates.
    const swept = await post("/api/first-run", {});
    if (swept && swept.marked) {
      const n = $("firstrun");
      n.hidden = false;
      n.textContent = `first run \u2014 ${swept.marked} existing items marked read. `
        + `Only what arrives from now on will show as unread.`;
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

boot().catch((e) => { $("tape").innerHTML = `<div class="note">failed: ${esc(e.message)}</div>`; });
