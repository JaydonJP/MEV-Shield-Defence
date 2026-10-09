"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const money = (v) => "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const num = (v, d = 2) => Number(v).toLocaleString("en-US", { maximumFractionDigits: d });
const time = () => new Date().toLocaleTimeString("en-US", { hour12: false });

let busy = false;
const buttons = () => Array.from(document.querySelectorAll(".btn"));
function setBusy(b) { busy = b; buttons().forEach((x) => (x.disabled = b)); }

/* ----------------------------------------------------------------- websocket */
let ws;
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (m) => handle(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connect, 1500);
}
connect();

/* ----------------------------------------------------------------- actions */
document.addEventListener("click", (e) => {
  const b = e.target.closest(".btn");
  if (!b || busy) return;
  const act = b.dataset.act;
  if (act === "reset") return post("/api/reset", {}, false);
  if (act === "predict") { clearStage(b.textContent); return post("/api/predict", { amount: amount() }); }
  if (act === "sandwich") { clearStage(b.textContent); return post("/api/sandwich", { amount: amount(), protect: b.dataset.protect }); }
  if (act === "oracle") { clearStage(b.textContent); return post("/api/oracle", { target: b.dataset.target, guard: b.dataset.guard }); }
  if (act === "arbitrage") { clearStage(b.textContent); return post("/api/arbitrage", {}); }
});
const amount = () => Math.max(1, Number($("#amount").value) || 500);

async function post(url, params, markBusy = true) {
  if (markBusy) setBusy(true);
  const qs = new URLSearchParams(params).toString();
  try { await fetch(qs ? `${url}?${qs}` : url, { method: "POST" }); }
  catch (_) {}
  finally { if (markBusy) setBusy(false); }
}

/* ----------------------------------------------------------------- stage reset */
function clearStage(title) {
  $("#phases").innerHTML = "";
  $("#block").innerHTML = "";
  $("#mempool").innerHTML = "";
  $("#result-banner").classList.add("hidden");
  $("#prediction").classList.add("hidden");
  const div = el("div", "log-divider", (title || "run").toUpperCase());
  $("#stream").appendChild(div);
  $("#stream").scrollTop = $("#stream").scrollHeight;
}

/* ----------------------------------------------------------------- event handler */
const PHASE_ORDER = { intercept: 1, expose: 1, attack: 1, arbitrage: 1, predict: 2, detect: 2, prevent: 3, settle: 4 };
let phaseSteps = {};

function handle(ev) {
  switch (ev.kind) {
    case "status": return renderStatus(ev);
    case "log": return addLog(ev);
    case "phase": return addPhase(ev);
    case "mempool": return renderMempool(ev.txs);
    case "block": return renderBlock(ev);
    case "prediction": return renderPrediction(ev);
    case "alert": return renderAlert(ev);
    case "result": return renderResult(ev);
  }
}

/* ----------------------------------------------------------------- status */
function renderStatus(s) {
  $("#offline").classList.toggle("hidden", !!s.ready);
  for (const k of ["main", "shadow", "model"]) {
    const pill = document.querySelector(`.pill[data-k="${k}"]`);
    if (pill) pill.className = "pill " + (s[k] ? "on" : "off");
  }
  if (s.block != null) $("#v-block").textContent = s.block;
  if (s.spot != null) setMetric("#m-spot", num(s.spot));
  if (s.twap != null) setMetric("#m-twap", num(s.twap));
  if (s.lending) {
    setMetric("#m-vuln", num(s.lending.vulnerable, 0));
    setMetric("#m-safe", num(s.lending.safe, 0));
  }
  if (s.model_metrics && s.model_metrics.r2 != null) renderModel(s.model_metrics);
}
function setMetric(sel, val) {
  const e = $(sel); if (!e) return;
  if (e.textContent !== val) { e.textContent = val; e.classList.add("flash"); setTimeout(() => e.classList.remove("flash"), 500); }
}
function renderModel(m) {
  $("#mm-r2").textContent = m.r2.toFixed(3);
  $("#mm-cv").textContent = m.cv_r2_mean.toFixed(3);
  $("#mm-mae").textContent = money(m.mae);
  $("#mm-auc").textContent = m.roc_auc.toFixed(3);
  const imp = m.feature_importances || {};
  const vals = Object.values(imp); const max = Math.max(...vals, 1e-9);
  const wrap = $("#model-imp"); wrap.innerHTML = "";
  Object.entries(imp).slice(0, 5).forEach(([k, v]) => {
    const row = el("div", "imp-row");
    row.appendChild(el("span", null, k));
    const bar = el("div", "imp-bar"); const fill = el("i"); fill.style.width = `${Math.max(2, (v / max) * 100)}%`;
    bar.appendChild(fill); row.appendChild(bar); wrap.appendChild(row);
  });
}

/* ----------------------------------------------------------------- stream */
function addLog(ev) {
  const row = el("div", `log-row ${ev.level || "info"}`);
  row.appendChild(el("span", "log-dot"));
  const body = el("div", "log-body");
  body.appendChild(el("div", "log-text", escape(ev.text)));
  body.appendChild(el("div", "log-time", time()));
  row.appendChild(body);
  const s = $("#stream"); s.appendChild(row); s.scrollTop = s.scrollHeight;
}

/* ----------------------------------------------------------------- phases */
function addPhase(ev) {
  Object.values(phaseSteps).forEach((n) => n.classList.remove("active"));
  if (!phaseSteps[ev.name]) {
    const step = el("div", "phase-step");
    const idx = Object.keys(phaseSteps).length + 1;
    step.appendChild(el("span", "num", idx));
    step.appendChild(el("span", "label", escape(ev.label)));
    $("#phases").appendChild(step);
    phaseSteps[ev.name] = step;
    // mark earlier steps done
    Object.entries(phaseSteps).forEach(([k, n]) => { if (k !== ev.name) { n.classList.add("done"); } });
  }
  phaseSteps[ev.name].classList.add("active");
  phaseSteps[ev.name].classList.remove("done");
}
// reset the phase map whenever a divider is added
const _clear = clearStage;
clearStage = function (t) { phaseSteps = {}; _clear(t); };

/* ----------------------------------------------------------------- mempool */
function renderMempool(txs) {
  const wrap = $("#mempool");
  wrap.innerHTML = "";
  if (!txs || !txs.length) { wrap.appendChild(el("div", "empty", "mempool empty")); return; }
  txs.forEach((t) => {
    const row = el("div", `tx k-${t.kind || "other"}`);
    row.appendChild(el("div", "pos", "·"));
    const who = el("div", "who");
    who.appendChild(el("div", "role", escape(t.role || "user")));
    who.appendChild(el("div", "call", escape(t.label || "") + (t.amount_usd ? ` — ${money(t.amount_usd)}` : "")));
    row.appendChild(who);
    const risk = t.risk_harm != null ? `ML harm ${money(t.risk_harm)}` : "";
    row.appendChild(el("div", "tip", risk ? `<small>${risk}</small>` : ""));
    row.appendChild(el("div", "flag", t.p_attack != null ? `P ${(t.p_attack * 100).toFixed(0)}%` : "pending"));
    wrap.appendChild(row);
  });
}

/* ----------------------------------------------------------------- block */
function renderBlock(ev) {
  $("#block-label").textContent = `Block ${ev.number}`;
  const wrap = $("#block");
  wrap.innerHTML = "";
  if (!ev.txs.length) { wrap.appendChild(el("div", "empty", "no transactions")); return; }
  ev.txs.forEach((t, i) => {
    setTimeout(() => {
      const row = el("div", `tx k-${t.kind} ${t.status ? "" : "revert"}`);
      row.appendChild(el("div", "pos", t.pos));
      const who = el("div", "who");
      who.appendChild(el("div", "role", escape(t.role)));
      who.appendChild(el("div", "call", escape(t.fn)));
      row.appendChild(who);
      row.appendChild(el("div", "tip", `${t.tip}<small> gwei</small>`));
      let flag = el("div", "flag " + (t.status ? "ok" : "crit"), t.status ? "ok" : "revert");
      if (t.labels && t.labels.includes("CRITICAL")) flag = el("div", "flag crit", "CRITICAL");
      else if (t.private) flag = el("div", "flag priv", "private");
      row.appendChild(flag);
      wrap.appendChild(row);
    }, i * 140);
  });
}

/* ----------------------------------------------------------------- prediction */
function renderPrediction(ev) {
  $("#prediction").classList.remove("hidden");
  const body = $("#deftable-body"); body.innerHTML = "";
  ev.defenses.forEach((d) => {
    const tr = el("tr", d.chosen ? "chosen" : "");
    const label = d.name + (d.slippage != null && d.name === "adaptive-slippage" ? ` (${(d.slippage * 100).toFixed(3)}%)` : "");
    [label, money(d.harm), money(d.gas), `${d.latency} blk`, `${(d.revert * 100).toFixed(1)}%`, money(d.J)]
      .forEach((c, i) => { const td = el("td", null, i === 0 ? escape(c) : c); tr.appendChild(td); });
    body.appendChild(tr);
  });
  $("#pred-meta").innerHTML =
    `public harm ${money(ev.harm_public)} · attacker profit ${money(ev.attacker_profit)} · ` +
    `closed-form ${money(ev.closed_form_harm)} (err ${ev.closed_form_error}%) · sim ${ev.sim_ms}ms`;
}

/* ----------------------------------------------------------------- alert */
function renderAlert(ev) {
  const a = $("#alert");
  a.className = "alert " + ev.severity;
  $("#alert-label").textContent = ev.label;
  $("#alert-by").textContent = ev.sender_role;
  const ul = $("#alert-reasons"); ul.innerHTML = "";
  (ev.reasons || []).forEach((r) => ul.appendChild(el("li", null, escape(r))));
}

/* ----------------------------------------------------------------- result */
function renderResult(ev) {
  const b = $("#result-banner");
  b.className = "result-banner " + ev.outcome;
  const harm = ev.outcome === "benign" ? "no harm" : money(ev.harm_usd || 0);
  b.innerHTML =
    `<div class="rb-title"><span>${escape(ev.title)}</span><span class="rb-harm">${harm}</span></div>` +
    `<div class="rb-detail">${escape(ev.detail || "")}</div>`;
}

function escape(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
