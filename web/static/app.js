"use strict";
// Calendar agent web page. Times are shown as the agent's local wall-clock time (taken from the ISO strings the
// server sends, already in the agent's timezone), so a phone in another timezone still shows your day correctly.

const CSRF = document.querySelector('meta[name="csrf-token"]').content;
const HOUR = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--hour")) || 56;
const SNAP = 15;
let state = null;
let day = null;

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;  // CSSOM, allowed by the page's Content-Security-Policy
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) node.append(kid.nodeType ? kid : String(kid));
  return node;
};

function toast(message, error = false) {
  const t = $("#toast");
  t.textContent = message;
  t.className = "show" + (error ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.className = ""), error ? 6000 : 3000);
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  return { ok: res.ok, status: res.status, data };
}

async function act(path, body = {}) {
  const r = await api(path, body);
  let msg = r.data.message || (r.ok ? "Done." : "That didn't work.");
  if (r.data.alternatives && r.data.alternatives.length) {
    msg += " Free: " + r.data.alternatives.map(([s]) => hm(s)).join(", ");
  }
  toast(msg, !r.ok);
  await load();
  return r.ok;
}

// "2026-09-28T19:00:00+05:30" -> minutes since midnight, "19:00"
const minutesOf = (iso) => parseInt(iso.slice(11, 13), 10) * 60 + parseInt(iso.slice(14, 16), 10);
const hm = (iso) => iso.slice(11, 16);
const pad = (n) => String(n).padStart(2, "0");
const offsetOf = (iso) => (iso.length > 19 ? iso.slice(19) : "");
const isoAt = (dayStr, minutes, offset) => `${dayStr}T${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}:00${offset}`;
const fmtMinutes = (m) => (m >= 60 ? `${Math.floor(m / 60)} h${m % 60 ? " " + (m % 60) + " min" : ""}` : `${m} min`);

async function load() {
  const r = await api(`/api/state?day=${day || ""}`);
  if (!r.ok) { toast(r.data.message || "Couldn't load your day.", true); return; }
  state = r.data;
  day = state.day;
  render();
}

function render() {
  const d = new Date(state.day + "T12:00:00");
  $("#day-title").textContent = state.day === state.today ? "Today" :
    d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  $("#pause").textContent = state.paused ? "Resume mail" : "Pause mail";
  renderTimeline();
  renderAsked();
  renderWaiting();
  renderDeadlines();
  renderHabits();
  renderSettings();
}

function renderTimeline() {
  const tl = $("#timeline");
  const startMin = state.day_start_hour * 60;
  tl.replaceChildren();
  tl.style.height = `${(24 * 60 - startMin) / 60 * HOUR}px`;
  const top = (m) => `${(m - startMin) / 60 * HOUR}px`;
  for (let h = state.day_start_hour; h < 24; h++) {
    tl.append(el("div", { class: "hour", style: `top:${top(h * 60)}` }, el("span", {}, `${pad(h)}:00`)));
  }
  // outside work hours, shaded
  const [ws, we] = state.window.map((t) => parseInt(t.slice(0, 2), 10) * 60 + parseInt(t.slice(3), 10));
  if (ws > startMin) tl.append(el("div", { class: "shade", style: `top:0;height:${(ws - startMin) / 60 * HOUR}px` }));
  if (we < 24 * 60) tl.append(el("div", { class: "shade", style: `top:${top(we)};height:${(24 * 60 - we) / 60 * HOUR}px` }));
  if (state.day === state.today) {
    const n = minutesOf(state.now);
    if (n >= startMin) tl.append(el("div", { class: "now-line", style: `top:${top(n)}` }));
  }
  const allday = $("#allday");
  allday.replaceChildren(...state.events.filter((e) => e.all_day).map((e) => el("span", {}, e.title)));
  for (const e of state.events.filter((e) => !e.all_day)) {
    const s = Math.max(minutesOf(e.start), startMin), en = e.end.slice(0, 10) > state.day ? 24 * 60 : minutesOf(e.end);
    if (en <= startMin) continue;
    tl.append(el("div", { class: "item event", style: `top:${top(s)};height:${Math.max(18, (en - s) / 60 * HOUR - 2)}px`,
                          title: `${e.title} (${e.calendar})` },
      el("div", { class: "time" }, `${hm(e.start)}-${hm(e.end)}`), e.title));
  }
  for (const b of state.blocks) {
    const s = minutesOf(b.start), en = minutesOf(b.end) || 24 * 60;
    const movable = (b.kind === "work" || b.kind === "habit") && b.status === "booked" && b.end > state.now;
    const node = el("div", { class: `item ${b.kind} ${["done", "partly", "notdone"].includes(b.status) ? b.status : ""}`,
                             style: `top:${top(s)};height:${Math.max(18, (en - s) / 60 * HOUR - 2)}px`,
                             title: movable ? "Drag to move; drag the bottom edge to resize" : b.title },
      el("div", { class: "time" }, `${hm(b.start)}-${hm(b.end)}`),
      b.title + (b.status === "done" ? " ✓" : b.status === "notdone" ? " ✗" : ""));
    if (movable) {
      node.append(el("div", { class: "grip" }));
      makeDraggable(node, b, startMin);
    }
    tl.append(node);
  }
}

function makeDraggable(node, block, startMin) {
  node.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    const resize = ev.target.classList.contains("grip");
    const s0 = minutesOf(block.start), e0 = minutesOf(block.end) || 24 * 60;
    const y0 = ev.clientY;
    let s = s0, e = e0;
    node.setPointerCapture(ev.pointerId);
    node.classList.add("dragging");
    const move = (mv) => {
      const delta = Math.round(((mv.clientY - y0) / HOUR * 60) / SNAP) * SNAP;
      if (resize) { e = Math.max(s0 + SNAP, Math.min(24 * 60, e0 + delta)); s = s0; }
      else { s = Math.max(startMin, Math.min(24 * 60 - (e0 - s0), s0 + delta)); e = s + (e0 - s0); }
      node.style.top = `${(s - startMin) / 60 * HOUR}px`;
      node.style.height = `${Math.max(18, (e - s) / 60 * HOUR - 2)}px`;
      node.querySelector(".time").textContent = `${pad(Math.floor(s / 60))}:${pad(s % 60)}-${pad(Math.floor(e / 60))}:${pad(e % 60)}`;
    };
    const up = async () => {
      node.removeEventListener("pointermove", move);
      node.removeEventListener("pointerup", up);
      node.removeEventListener("pointercancel", up);
      node.classList.remove("dragging");
      if (s === s0 && e === e0) return;
      const off = offsetOf(block.start);
      await act(`/api/blocks/${block.id}/move`, { start: isoAt(state.day, s, off), end: isoAt(state.day, e, off) });
    };
    node.addEventListener("pointermove", move);
    node.addEventListener("pointerup", up);
    node.addEventListener("pointercancel", up);
  });
}

function renderAsked() {
  const list = $("#asked");
  const asked = state.asked || [];
  $("#asked-panel").classList.toggle("hidden", asked.length === 0);
  list.replaceChildren(...asked.map((b) => el("li", {},
    el("div", {}, `${b.title} `, el("span", { class: "muted" }, `${hm(b.start)}-${hm(b.end)}`)),
    el("div", { class: "row" },
      el("button", { onclick: () => act(`/api/blocks/${b.id}/answer`, { answer: "done" }) }, "Done"),
      el("button", { onclick: () => act(`/api/blocks/${b.id}/answer`, { answer: "partly" }) }, "Partly"),
      el("button", { onclick: () => act(`/api/blocks/${b.id}/answer`, { answer: "notdone" }) }, "Not done")))));
}

function renderWaiting() {
  const list = $("#waiting");
  if (state.day !== state.today) { list.replaceChildren(el("li", { class: "empty" }, "Shown for today only.")); return; }
  if (!state.waiting.length) { list.replaceChildren(el("li", { class: "empty" }, "Nothing is waiting for a time.")); return; }
  list.replaceChildren(...state.waiting.map((w) => el("li", {},
    el("div", {}, w.title + (w.kind === "habit" ? " (habit)" : w.kind === "exam" ? " (exam prep)" : "")),
    el("div", { class: "muted" }, `${fmtMinutes(w.minutes)} today` + (w.kind === "habit" ? "" : `, due ${w.due.slice(5, 10)} ${hm(w.due)}`)
      + (w.chunk < w.minutes ? ` - pick a time for the first ${fmtMinutes(w.chunk)}` : "")),
    el("div", { class: "row" },
      w.options.length ? w.options.map(([s, e]) => el("button", { class: "primary",
        onclick: () => act(`/api/items/${w.id}/book`, { start: s, minutes: w.chunk }) }, `${hm(s)}-${hm(e)}`))
        : el("span", { class: "muted" }, "No free slot left today."),
      el("button", { onclick: () => act(`/api/items/${w.id}/not-today`) }, "Not today"),
      w.drop ? el("button", { onclick: () => confirm(w.drop) && act(`/api/items/${w.id}/drop`) }, "Drop") : null))));
}

function renderDeadlines() {
  const list = $("#deadlines");
  if (!state.deadlines.length) { list.replaceChildren(el("li", { class: "empty" }, "No deadlines in the next 2 weeks.")); return; }
  list.replaceChildren(...state.deadlines.map((d) => {
    const hours = el("input", { type: "number", min: "0", max: "200", step: "0.5", value: d.effort, style: "width:5em" });
    return el("li", {},
      el("div", {}, d.title, " ", el("span", { class: "muted" }, `due ${d.due.slice(5, 10)} ${hm(d.due)}`)),
      el("div", { class: "row" },
        el("button", { onclick: () => act(`/api/deadlines/${encodeURIComponent(d.id)}/done`) }, "Done"),
        hours, el("span", { class: "muted" }, "h of work"),
        el("button", { onclick: () => act(`/api/deadlines/${encodeURIComponent(d.id)}/effort`, { hours: parseFloat(hours.value) }) }, "Save"),
        el("button", { onclick: () => act(`/api/deadlines/${encodeURIComponent(d.id)}/move`, { days: 1 }) }, "+1 day")));
  }));
}

function renderHabits() {
  const list = $("#habits");
  if (!state.habits.length) { list.replaceChildren(el("li", { class: "empty" }, "No habits yet.")); return; }
  list.replaceChildren(...state.habits.map((h) => el("li", {},
    el("div", {}, h.name + (h.active ? "" : " (paused)"), " ",
      el("span", { class: "muted" }, h.describe + (h.streak ? ` - streak ${h.streak}` : ""))),
    el("div", { class: "row" },
      el("button", { onclick: () => act(`/api/habits/${h.id}/toggle`) }, h.active ? "Pause" : "Resume"),
      el("button", { onclick: () => confirm(`Delete ${h.name}?`) && act(`/api/habits/${h.id}/delete`) }, "Delete")))));
}

function renderSettings() {
  const list = $("#settings");
  list.replaceChildren(...state.settings.map((s) => {
    let input;
    if (s.kind === "bool" || s.kind === "channels") {
      input = el("select", {}, s.choices.map((c, i) => el("option", { value: i, ...(c === s.value ? { selected: "" } : {}) }, c)));
    } else {
      input = el("input", { value: s.value, list: `choices-${s.index}`, style: "width:9em" });
    }
    const save = () => act(`/api/settings/${s.index}`, s.kind === "bool" || s.kind === "channels"
      ? { choice: parseInt(input.value, 10) } : { value: input.value });
    return el("li", {},
      el("div", { class: "setting-row" }, el("span", {}, s.label), el("span", { class: "value" }, s.value)),
      el("datalist", { id: `choices-${s.index}` }, s.choices.map((c) => el("option", { value: c }))),
      el("div", { class: "row" }, input, el("button", { onclick: save }, "Save"),
        el("button", { class: "small", onclick: () => act(`/api/settings/${s.index}`, { reset: true }) }, "Reset")));
  }));
}

function shiftDay(n) {
  const d = new Date((day || state.today) + "T12:00:00");
  d.setDate(d.getDate() + n);
  day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  load();
}

$("#prev").addEventListener("click", () => shiftDay(-1));
$("#next").addEventListener("click", () => shiftDay(1));
$("#today").addEventListener("click", () => { day = null; load(); });
$("#check").addEventListener("click", () => act("/api/actions/check"));
$("#plan").addEventListener("click", () => act("/api/actions/plan"));
$("#pause").addEventListener("click", () => act(`/api/actions/${state && state.paused ? "resume" : "pause"}`));
$("#todo-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (await act("/api/todos", { text: $("#todo-text").value })) $("#todo-text").value = "";
});
$("#habit-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = ev.target;
  act("/api/habits", { name: f.name.value, minutes: parseInt(f.minutes.value, 10),
    days: [...f.querySelectorAll(".days input:checked")].map((c) => c.value),
    window_start: f.window_start.value, window_end: f.window_end.value });
});

// Refresh every minute while the page is visible (each refresh reads your calendars).
setInterval(() => { if (document.visibilityState === "visible") load(); }, 60000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") load(); });
load();
