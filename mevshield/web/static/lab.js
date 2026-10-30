"use strict";
const q = (s) => document.querySelector(s);
const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
const fmt = (v, digits = 2) => Number(v).toLocaleString("en-US", { maximumFractionDigits: digits });
const usd = (v) => "$" + fmt(v);
let lastReplay = null;
let chainBusy = false;
let chainReady = false;
let serverBusy = false;
let requestSequence = 0;

function updateChainButtons() {
  document.querySelectorAll(".feature-run,#run-all,#reset-lab").forEach(b => b.disabled = chainBusy || serverBusy || !chainReady);
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok || result.error) throw new Error(result.error || (typeof result.detail === "string" ? result.detail : "Check the input ranges and try again."));
  return result;
}
function notice(message, error = false) { q("#notice").textContent = message; q("#notice").classList.toggle("error", error); }
function metrics(parent, values) {
  const grid = node("div", null, "metric-grid");
  values.forEach(([label, value, good]) => { const card = node("div", null, "lab-metric" + (good ? " good" : "")); card.append(node("span", label, "label"), node("strong", value)); grid.append(card); });
  parent.append(grid);
}
function table(parent, headings, rows, selected = -1) {
  const wrap = node("div", null, "lab-table-wrap"), t = node("table", null, "lab-table"), head = node("thead"), tr = node("tr");
  headings.forEach(h => tr.append(node("th", h))); head.append(tr); t.append(head);
  const body = node("tbody");
  rows.forEach((values, i) => { const row = node("tr", null, i === selected ? "selected" : ""); values.forEach(v => row.append(node("td", v))); body.append(row); });
  t.append(body); wrap.append(t); parent.append(wrap);
}
function evidence(parent, result) {
  const details = node("details"); details.append(node("summary", "Inspect experiment evidence"));
  details.append(node("pre", JSON.stringify(result, null, 2), "evidence")); parent.append(details);
}
function renderFeature(name, r, parent) {
  const section = node("section"); section.append(node("h2", name));
  if (name === "routing") {
    metrics(section, [["Received WETH", fmt(r.actual_out / 1e18, 7)], ["Measured user gas", usd(r.gas_usd)], ["Attack transactions", r.attack_transactions]]);
    const candidates = r.quote.candidates;
    table(section, ["Pool A / B", "Quote · WETH", "Modeled MEV loss", "Expected net value"], candidates.map(c => [c.share_a_pct + "% / " + (100-c.share_a_pct) + "%", fmt(c.quoted_out/1e18, 6), usd(c.predicted_harm_usd), usd(c.expected_net_usd)]), candidates.findIndex(c => c.share_a_pct === r.quote.chosen.share_a_pct));
    section.append(node("p", "Atomic execution " + (r.reverted ? "reverted" : "completed") + ". Selected route highlighted; expected values use the public-orderflow model.", "lab-scope"));
  } else if (name === "cashback") {
    if (!r.settled) section.append(node("p", r.reason));
    else {
      metrics(section, [["User cashback", usd(r.rebate_usdc), true], ["Received WETH", fmt(r.swap_out_weth, 7)], ["Searcher gross profit", usd(r.searcher_profit_usdc)], ["Settlement gas", usd(r.settlement_gas_usd)]]);
      table(section, ["Searcher", "Rebate bid · USDC"], r.bids.map(b => [b.searcher, fmt(b.rebate_usdc)]), r.bids.findIndex(b => b.searcher === r.winner));
    }
  } else if (name === "batch") {
    metrics(section, [["Matched directly", fmt(r.matched_fraction * 100) + "%", true], ["Matched value", usd(r.matched_usdc)], ["Total gas used", fmt(r.gas_used)]]);
    table(section, ["User", "Received", "Standalone AMM quote"], [["Alice", fmt(r.alice_received_weth, 7) + " WETH", fmt(r.alice_amm_quote_weth, 7) + " WETH"], ["Bob", usd(r.bob_received_usdc), usd(r.bob_amm_quote_usdc)]]);
  } else if (name === "firewall") {
    metrics(section, [["Actual outflow", usd(r.actual_outflow_usdc)], ["Per-block cap", usd(r.block_limit_usdc)], ["Window used / cap", usd(r.window_used_usdc) + " / " + usd(r.window_limit_usdc)]]);
    table(section, ["Borrow", "Requested", "Outcome"], r.borrow_succeeded.map((ok,i) => [i+1, usd(r.requested_each_usdc), ok ? "Executed" : "Rejected"]));
  } else if (name === "coordinated") {
    metrics(section, [["Sequences tested", r.candidates_tested], ["Findings", r.findings.length, r.findings.length > 0], ["Analysis time", fmt(r.latency_ms) + " ms"]]);
    const flow = node("div", null, "sequence-flow");
    ["Front-run", "Victim swap", "Backrun"].forEach((label,i) => { if(i) flow.append(node("span", "→")); const step = node("div", label, "sequence-step"); step.append(node("small", ["Account A → token delivery", "Counterfactual victim loss", "Account B → proceeds"][i])); flow.append(step); }); section.append(flow);
    table(section, ["Verdict", "Victim harm", "Actor profit before gas", "Estimated actor gas"], r.findings.map(f => [f.label, usd(f.victim_harm_usd), usd(f.actor_profit_usd), usd(f.actor_gas_usd)]));
  }
  if (r.scope) section.append(node("p", r.scope, "lab-scope"));
  evidence(section, r); parent.append(section);
}
async function runFeature(feature, amount) {
  if(chainBusy || serverBusy || !chainReady) return;
  chainBusy = true;
  updateChainButtons();
  const replayRequest = feature === "all" ? ++requestSequence : null;
  const parent = q("#feature-results"); parent.replaceChildren(node("p", "Running the requested experiment…")); parent.classList.remove("hidden");
  notice("Running " + feature + ". The chain and shadow fork may take a few seconds.");
  try {
    const result = await api("/api/extensions/" + feature, amount === undefined ? {} : {amount});
    parent.replaceChildren();
    if(feature === "all") {
      Object.entries(result.results).filter(([name]) => name !== "replay").forEach(([name,r]) => renderFeature(name,r,parent));
      if (replayRequest === requestSequence && result.results.replay) {
        applyScenario(result.results.replay.scenario);
        renderReplay(result.results.replay);
      }
    }
    else renderFeature(feature,result,parent);
    notice(feature === "all" ? "Suite complete. Every experiment restored its starting chain state." : "Experiment complete. Evidence is shown below.");
  } catch(e) { parent.replaceChildren(node("p",e.message)); notice(e.message, true); }
  finally { chainBusy = false; await status(); }
}
document.querySelectorAll(".feature-run").forEach(button => button.addEventListener("click", () => {
  const card = button.closest(".feature-card"), input = card.querySelector("input");
  if (input.reportValidity()) runFeature(card.dataset.feature, Number(input.value));
}));
q("#run-all").addEventListener("click", () => runFeature("all"));
q("#reset-lab").addEventListener("click", async () => {
  if (chainBusy || serverBusy || !chainReady) return;
  chainBusy = true;
  updateChainButtons();
  const button = q("#reset-lab"); button.textContent = "Resetting…";
  notice("Restoring the local blockchain to its clean deployed state…");
  try {
    await api("/api/reset", {});
    const results = q("#feature-results"); results.replaceChildren(); results.classList.add("hidden");
    document.querySelectorAll(".feature-card input").forEach(input => input.value = input.defaultValue);
    q("#scenario-form").reset();
    invalidateReplay();
    await compareReplay();
    notice("Lab reset. The local blockchain and default inputs are restored; saved scenarios are retained.");
  } catch (e) { notice("Lab reset failed: " + e.message, true); }
  finally { button.textContent = "Reset lab"; chainBusy = false; await status(); }
});

function scenario() {
  const data = Object.fromEntries(new FormData(q("#scenario-form")));
  const s = {}; Object.entries(data).forEach(([key,value]) => s[key] = Number(value));
  s.slippage = s.slippage_pct / 100; delete s.slippage_pct; return s;
}
function syncRanges() { q("#liquidity-out").textContent = q('[name="liquidity_scale"]').value + "×"; q("#slippage-out").textContent = q('[name="slippage_pct"]').value + "%"; }
function invalidateReplay() {
  requestSequence++;
  lastReplay = null;
  q("#export-result").disabled = true;
  q("#saved-replays").value = "";
  q("#replay-results").replaceChildren(node("p", "Scenario changed. Compare defenses to update the results."));
  syncRanges();
}
q("#scenario-form").addEventListener("input", event => { if(event.target.id !== "saved-replays") invalidateReplay(); });
function applyScenario(s) {
  Object.entries(s).forEach(([key,value]) => { const input = q('[name="' + (key === "slippage" ? "slippage_pct" : key) + '"]'); if(input) input.value = key === "slippage" ? value*100 : value; });
  syncRanges();
}
function renderReplay(r) {
  lastReplay = r; q("#export-result").disabled = false;
  const parent = q("#replay-results"); parent.replaceChildren();
  metrics(parent, [["Best completed strategy", r.best_completed], ["Modeled price move", fmt(r.seeded_price_move * 100, 3) + "%"], ["Guardian can precede", r.guardian.can_precede_attack ? "Yes, under assumptions" : "No"]]);
  parent.classList.add("replay-metrics");
  parent.append(node("p", "Modeled MEV loss · lower is better", "lab-scope"));
  const maxLoss = Math.max(1, ...r.rows.map(x => x.mev_loss_usd));
  r.rows.forEach(row => {
    const bar = node("div", null, "bar-row" + (row.strategy === r.best_completed ? " best" : ""));
    const track = node("progress", null, "bar-track"); track.max = maxLoss; track.value = Math.max(0, row.mev_loss_usd); track.setAttribute("aria-label", row.strategy + " modeled loss in USD");
    bar.append(node("span", row.strategy + (row.completed ? "" : " (failed)")), track, node("span", row.completed ? usd(row.mev_loss_usd) : "Failed swap")); parent.append(bar);
  });
  table(parent, ["Strategy", "Completed", "WETH received", "MEV loss", "Gas + fee", "Comparison cost*"], r.rows.map(x => [x.strategy, x.completed ? "Yes" : "No", fmt(x.received_weth,7), usd(x.mev_loss_usd), usd(x.gas_usd+x.fee_usd), usd(x.comparison_cost_usd)]), r.rows.findIndex(x => x.strategy === r.best_completed));
  parent.append(node("p", "*Comparison cost uses a common initial quote, gas and fees, plus a retry penalty for failed swaps. Price improvements can make it negative. Failed swaps are excluded from the winner. The router requotes at inclusion; other strategies retain the initial quote.", "lab-scope"));
  parent.append(node("p", r.scope + ". Guardian ordering is a separate model check, not an executed pause transaction.", "lab-scope"));
  evidence(parent, r);
}
async function compareReplay() {
  const sequence = ++requestSequence;
  lastReplay = null; q("#export-result").disabled = true;
  q("#replay-results").replaceChildren(node("p", "Calculating this scenario…"));
  try { const result = await api("/api/replay", scenario()); if(sequence === requestSequence) renderReplay(result); }
  catch(e) { if(sequence === requestSequence) { q("#replay-results").replaceChildren(node("p",e.message)); notice(e.message,true); } }
}
q("#scenario-form").addEventListener("submit", event => { event.preventDefault(); compareReplay(); });
async function refreshSaved(selected = "") {
  const saved = await api("/api/replays"), select = q("#saved-replays");
  select.replaceChildren(node("option", "Choose a saved scenario")); select.firstChild.value = "";
  saved.forEach(item => { const option = node("option", item.id + " · " + usd(item.scenario.amount_usdc)); option.value = item.id; select.append(option); }); select.value = selected;
}
q("#save-replay").addEventListener("click", async () => {
  if (!q("#scenario-form").reportValidity()) return;
  try { const saved = await api("/api/replays", scenario()); await refreshSaved(saved.id); notice("Saved scenario " + saved.id + ". Reload it to reproduce the same model result."); }
  catch(e) { notice(e.message,true); }
});
q("#saved-replays").addEventListener("change", async event => {
  const sequence = ++requestSequence;
  if(!event.target.value) { invalidateReplay(); return; }
  lastReplay = null; q("#export-result").disabled = true;
  q("#replay-results").replaceChildren(node("p", "Loading the saved scenario…"));
  try { const result = await api("/api/replays/" + event.target.value); if(sequence !== requestSequence) return;
    applyScenario(result.scenario); renderReplay(result);
  } catch(e) { if(sequence === requestSequence) { q("#replay-results").replaceChildren(node("p",e.message)); notice(e.message,true); } }
});
q("#export-result").addEventListener("click", () => {
  if(!lastReplay) return; const url = URL.createObjectURL(new Blob([JSON.stringify(lastReplay,null,2)],{type:"application/json"}));
  const a = node("a"); a.href = url; a.download = "mev-shield-replay.json"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
async function status() {
  try {
    const s = await api("/api/status");
    serverBusy = Boolean(s.busy);
    if (!serverBusy) chainReady = Boolean(s.ready);
    q("#chain-state").textContent = serverBusy ? "● Devnet busy · replay available" : chainReady ? "● Devnet connected · block " + s.block : "○ Devnet offline · replay available";
  } catch { chainReady = false; serverBusy = false; q("#chain-state").textContent = "Connection unavailable · replay needs the server"; }
  updateChainButtons();
}
updateChainButtons();
status(); setInterval(status,10000);
refreshSaved().catch(e => notice(e.message,true));
compareReplay();
