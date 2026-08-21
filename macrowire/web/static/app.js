"use strict";

// Sydney is the only timezone the page thinks in. The server resolves every
// instant with zoneinfo and hands back positions already projected; the
// client never does timezone arithmetic of its own, because doing it in two
// places is how the two drift apart.

const $ = (id) => document.getElementById(id);
const state = { sources: [], active: new Set(), activeJ: new Set(),
                tape: [], offsetHours: 10, seen: new Set() };

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
  return r.ok ? r.json() : null;
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

function drawTape() {
  // Two independent facets: OR within an axis, AND across them. Picking CN
  // and then a US source is an empty result, which is what was asked for.
  const rows = state.tape.filter((r) =>
    (state.activeJ.size === 0 || state.activeJ.has(r.jurisdiction)) &&
    (state.active.size === 0 || state.active.has(r.source)));
  if (!rows.length) { $("tape").innerHTML = `<div class="note">nothing in this window</div>`; return; }

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
    const count = r.count > 1
      ? ` <span class="count">×${r.count}</span>` : "";
    const cat = r.announcement_type ? ` \u00b7 ${esc(r.announcement_type)}` : "";
    const jur = r.jurisdiction ? `<span class="jur">${esc(r.jurisdiction)}</span>` : "";
    const sum = r.summary ? `<div class="sum">${esc(r.summary)}</div>` : "";
    const href = r.url ? `href="${esc(r.url)}" target="_blank" rel="noopener"` : "";
    html += `<article class="item imp${r.importance}${r.unread ? " unread" : ""}"
                      data-key="${esc(r.key)}" data-ids="${esc(r.ids.join(","))}">
        <div class="t">${hh}:${mm}</div>
        <div class="gut"></div>
        <div>
          <a class="hl" ${href}>${esc(r.title)}</a>
          <div class="meta">${jur}<span class="src">${esc(r.source.replace(/_/g, " "))}</span>${cat}${count}</div>
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

/* ---------------- chips ---------------- */

function drawChips(unread) {
  // Jurisdiction is the coarser axis and sits above. Selecting CN shows
  // cfets and both NBS feeds together without naming any of them.
  const order = ["AU", "CN", "HK", "JP", "US", "EU", "UK"];
  const present = order.filter((j) => state.sources.some((s) => s.jurisdiction === j));
  $("chips-j").innerHTML = present.map((j) => {
    const n = (unread.per_jurisdiction || {})[j] || 0;
    const names = state.sources.filter((s) => s.jurisdiction === j)
                               .map((s) => s.name.replace(/_/g, " ")).join(", ");
    return `<button class="chip" data-jur="${esc(j)}" title="${esc(names)}"
                    aria-pressed="${state.activeJ.has(j)}">
              ${esc(j)}${n ? `<span class="n">${n}</span>` : ""}
            </button>`;
  }).join("");
  $("chips-j").querySelectorAll(".chip").forEach((b) => {
    b.onclick = () => {
      const j = b.dataset.jur;
      state.activeJ.has(j) ? state.activeJ.delete(j) : state.activeJ.add(j);
      b.setAttribute("aria-pressed", state.activeJ.has(j));
      drawTape();
    };
  });

  $("chips").innerHTML = state.sources.map((s) => {
    const n = unread.per_source[s.name] || 0;
    return `<button class="chip" data-src="${esc(s.name)}"
                    aria-pressed="${state.active.has(s.name)}">
              ${esc(s.name.replace(/_/g, " "))}${n ? `<span class="n">${n}</span>` : ""}
            </button>`;
  }).join("");
  $("chips").querySelectorAll(".chip").forEach((b) => {
    b.onclick = () => {
      const name = b.dataset.src;
      state.active.has(name) ? state.active.delete(name) : state.active.add(name);
      b.setAttribute("aria-pressed", state.active.has(name));
      drawTape();
    };
  });
}

async function refreshUnread() {
  const u = await get("/api/unread?days=30");
  $("unread-total").innerHTML = u.total ? `<b>${u.total}</b> unread` : "all read";
  drawChips(u);
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

  $("rba").innerHTML = d.rba.map((r) => `
      <div class="k">${esc(r.series)}</div><div class="v">${r.value}</div>`
  ).join("");
  $("rba-asof").textContent = d.rba.length ? `4pm AEST · ${d.rba[0].period}` : "no data";

  const order = ["AU", "CN", "HK", "JP", "US", "EU", "UK"];
  const grouped = {};
  d.health.forEach((h) => { (grouped[h.jurisdiction] ||= []).push(h); });
  const row = (h) => {
    const s = h.seconds_since_success;
    const age = s === null ? "no success logged"
      : s < 90 ? "just now"
      : s < 3600 ? `${Math.floor(s / 60)}m ago`
      : s < 86400 ? `${Math.floor(s / 3600)}h ago`
      : `${Math.floor(s / 86400)}d ago`;
    // failure_kinds arrive already formatted; a null error_kind reads as
    // "unclassified" rather than leaking the string "None".
    const bad = h.stale || h.consecutive_failures > 0 || s === null;
    const parts = [age];
    if (h.consecutive_failures) parts.push(`${h.failure_kinds.join(", ")}`);
    if (h.stale) parts.push("stale");
    return `<div><span class="nm">${esc(h.name.replace(/_/g, " "))}</span>
                 <span class="${bad ? "warn" : "ok"}">${esc(parts.join(" \u00b7 "))}</span></div>`;
  };
  $("health").innerHTML = order.filter((j) => grouped[j]).map(
    (j) => `<span class="jgroup">${esc(j)}</span>` + grouped[j].map(row).join("")
  ).join("");

  const risk = d.health.filter((h) => h.replaceable === "NO");
  const rows = risk.reduce((a, h) => a + h.at_risk, 0);
  $("risk").innerHTML =
    `<b>${rows}</b> row(s) exist only here — ${esc(risk.map((h) => h.name.replace(/_/g, " ")).join(", "))}.<br>` +
    `Everything else is re-fetchable. Commit <b>export/irreplaceable.jsonl</b>.`;
}

/* ---------------- boot ---------------- */

async function boot() {
  const b = await get("/api/bootstrap");
  state.sources = b.sources;
  state.offsetHours = b.now.offset;
  state.zone = b.now.zone;
  if (b.first_run_marked_read) {
    const n = $("firstrun");
    n.hidden = false;
    n.textContent = `first run — ${b.first_run_marked_read} existing items marked read. `
      + `Only what arrives from now on will show as unread.`;
  }
  drawHours(); tickClock(); setInterval(tickClock, 1000);
  drawRibbon(await get("/api/ribbon"));
  state.tape = (await get("/api/tape?days=30")).items;
  drawTape();
  await refreshUnread();
  drawRail(await get("/api/rail"));

  setInterval(async () => {
    state.tape = (await get("/api/tape?days=30")).items;
    drawTape(); refreshUnread();
    drawRail(await get("/api/rail"));
  }, 120000);
  window.addEventListener("resize", () => { drawHours(); layoutMarkLanes(); });
}

boot().catch((e) => { $("tape").innerHTML = `<div class="note">failed: ${esc(e.message)}</div>`; });
