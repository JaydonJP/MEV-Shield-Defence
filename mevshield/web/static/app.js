"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const text = (selector, value) => { $(selector).textContent = value; };
const show = (selector, visible = true) => $(selector).classList.toggle("hidden", !visible);
const node = (tag, className, content) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content != null) element.textContent = String(content);
  return element;
};
const number = (value, digits = 2) => Number(value).toLocaleString("en-US", { maximumFractionDigits: digits });
const money = (value) => "$" + number(value, 2);
const short = (value) => value ? value.slice(0, 6) + "…" + value.slice(-4) : "—";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const EXPLORER = "https://sepolia.etherscan.io";
const SEPOLIA = "0xaa36a7";
function explorerLink(path, label) {
  const a = node("a", "", label); a.href = EXPLORER + path; a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
}
async function api(path, options = {}) {
  const response = await fetch(path, { signal: AbortSignal.timeout(25000), ...options });
  const data = await response.json();
  if (!response.ok || data.error) {
    const detail = data.error || data.detail;
    throw new Error(typeof detail === "string" ? detail : "The request could not be completed. Check your inputs and connection.");
  }
  return data;
}
let toastTimer;
function toast(message) {
  text("#toast", message); show("#toast"); clearTimeout(toastTimer);
  toastTimer = setTimeout(() => show("#toast", false), 9000);
}

// Navigation stays usable if the local lab or wallet is offline.
function navigate(view, updateHash = true) {
  if (!["lab", "network"].includes(view)) view = "lab";
  $$(".view").forEach((el) => el.classList.toggle("hidden", el.id !== `view-${view}`));
  $$(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
    if (el.dataset.view === view) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current");
  });
  text("#page-name", { lab: "Attack lab", network: "Live blockchain" }[view]);
  if (updateHash) history.replaceState(null, "", `#${view}`);
}
document.addEventListener("click", (event) => {
  const navigation = event.target.closest("[data-view]");
  if (navigation) navigate(navigation.dataset.view);
});
window.addEventListener("hashchange", () => navigate(location.hash.slice(1), false));
navigate(location.hash.slice(1), false);

const scenarios = {
  sandwich: { title: "A trade with an unwanted guest", description: "A bot buys just before you and sells just after. You receive fewer tokens; the bot keeps the difference.", user: "Your wallet", input: "USDC → WETH", pool: "Liquidity pool", output: "Tokens are exchanged", actor: "Sandwich bot", actorDetail: "Watching for a profitable trade" },
  oracle: { title: "When a price becomes a trap", description: "A huge borrowed trade distorts the market price. A vulnerable lender mistakes that inflated price for real collateral value.", user: "Flash lender", input: "Temporary borrowed funds", pool: "Lending market", output: "Collateral → borrowed USDC", actor: "Oracle manipulator", actorDetail: "Trying to inflate collateral value" },
  arbitrage: { title: "A flash loan that helps the market", description: "Buy in the cheaper pool, sell in the more expensive one, and repay the loan. Profit alone doesn’t make a transaction malicious.", user: "Flash lender", input: "Loan repaid in one transaction", pool: "Two trading pools", output: "Prices move closer together", actor: "Arbitrage trader", actorDetail: "Closing a price gap, without draining a protocol" },
  governance: { title: "Borrowed tokens. Borrowed power?", description: "An attacker borrows voting tokens to take over a DAO. Snapshot voting and a timelock stop instant control of the treasury.", user: "Flash lender", input: "Borrowed governance tokens", pool: "DAO treasury", output: "Votes control the funds", actor: "Governance attacker", actorDetail: "Trying to turn a flash loan into voting power" }
};
let scenario = "sandwich", protection = false, labReady = false, serverBusy = false, requestBusy = false, replaying = false;
let story = [], queue = [], draining = false, steps = 0, eventCount = 0, priceSamples = [];
let runScenario = "sandwich", runProtected = false;
function isBusy() { return serverBusy || requestBusy || draining || replaying; }
function updateControls() {
  const busy = isBusy();
  $$("#experiment-form button, #experiment-form input, #experiment-form select, .scenario-tab").forEach((el) => { el.disabled = busy; });
  $("#amount").disabled = busy || scenario !== "sandwich";
  $("#run-experiment").disabled = busy || !labReady;
  $("#predict").disabled = busy || !labReady;
  $("#reset-lab").disabled = busy || !labReady;
  $("#replay").disabled = busy || !story.some((ev) => ev.kind === "phase" || ev.kind === "result");
  text("#run-label", busy ? (replaying ? "Replaying story…" : "Experiment in progress…") : "Run experiment");
}
function chooseScenario(name, force = false) {
  if (!scenarios[name] || (!force && isBusy())) return;
  scenario = name;
  $$(".scenario-tab").forEach((el) => {
    const selected = el.dataset.scenario === name;
    el.classList.toggle("active", selected); el.setAttribute("aria-selected", String(selected)); el.tabIndex = selected ? 0 : -1;
  });
  $("#experiment").setAttribute("aria-labelledby", `tab-${name}`);
  const s = scenarios[name];
  text("#journey-title", s.title); text("#scenario-description", s.description);
  text("#user-node-title", s.user); text("#user-node-detail", s.input);
  text("#pool-node-title", s.pool); text("#pool-node-detail", s.output);
  text("#attacker-title", s.actor); text("#attacker-detail", s.actorDetail);
  show("#risk-mode-field", name === "sandwich"); show("#swap-controls", name === "sandwich"); show("#predict", name === "sandwich");
  show("#protection-controls", name !== "arbitrage");
  text("#control-description", name === "arbitrage" ? "Useful activity should pass through." : "Same attack. A different outcome.");
  updateProtection(); clearStory(false); updateControls();
}
$$(".scenario-tab").forEach((tab, index, tabs) => {
  tab.addEventListener("click", () => chooseScenario(tab.dataset.scenario));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || isBusy()) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    chooseScenario(tabs[next].dataset.scenario); tabs[next].focus();
  });
});
function updateProtection() {
  $$("[data-protection]").forEach((el) => { const on = el.dataset.protection === String(protection); el.classList.toggle("selected", on); el.setAttribute("aria-pressed", String(on)); });
  show("#defense-field", protection && scenario === "sandwich"); show("#oracle-field", protection && scenario === "oracle");
  $("#setting-note").classList.toggle("protected", protection || scenario === "arbitrage");
  let note;
  if (scenario === "sandwich") note = protection ? "The shield evaluates the trade before sending it. Compare defenses or pick one yourself." : "Your trade is public. A bot may move the price before it reaches the pool.";
  if (scenario === "oracle") note = protection ? "The Guardian can pause first; the hardened market also checks prices, deposit age and borrowing volume." : "This lender trusts the current market price, even when a flash loan has distorted it.";
  if (scenario === "governance") note = protection ? "Past token balances determine voting power, and execution must wait through a timelock." : "The vulnerable DAO counts borrowed tokens as votes immediately.";
  if (scenario === "arbitrage") note = "The Guardian checks for protocol loss. A profitable flash loan can be benign.";
  $("#setting-note p").textContent = note;
  const bypassed = !protection && scenario !== "arbitrage";
  $("#node-shield").classList.toggle("bypassed", bypassed);
  text("#shield-node-detail", bypassed ? "Protection bypassed" : scenario === "arbitrage" ? "Check for protocol harm" : "Inspect & protect");
}
$$("[data-protection]").forEach((el) => el.addEventListener("click", () => { if (!isBusy()) { protection = el.dataset.protection === "true"; updateProtection(); } }));
async function labAction(action, params = {}) {
  if (isBusy() || !labReady) return;
  requestBusy = true; updateControls();
  try { await api(`/api/${action}?${new URLSearchParams(params)}`, { method: "POST" }); }
  catch (error) { toast(error.message); caption("Experiment stopped", error.message, "!"); }
  finally { requestBusy = false; updateControls(); }
}
$("#experiment-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (scenario === "sandwich") labAction("sandwich", { amount: $("#amount").value, protect: protection, defense: $("#defense-choice").value, mode: $("#risk-mode").value });
  if (scenario === "oracle") labAction("oracle", { target: protection && $("#oracle-choice").value === "safe" ? "safe" : "vulnerable", guard: protection && $("#oracle-choice").value === "guardian" });
  if (scenario === "arbitrage") labAction("arbitrage");
  if (scenario === "governance") labAction("governance", { target: protection ? "safe" : "vulnerable" });
});
$("#predict").addEventListener("click", () => { if ($("#amount").reportValidity()) labAction("predict", { amount: $("#amount").value, mode: $("#risk-mode").value }); });
$("#reset-lab").addEventListener("click", () => labAction("reset"));
function caption(title, description, index = "→") { text("#story-title", title); text("#story-text", description); text("#caption-index", index); }
function clearStory(clearRecording = true) {
  steps = 0; eventCount = 0;
  if (clearRecording) story = [];
  $$(".flow-node").forEach((el) => { el.classList.remove("active", "danger"); });
  $("#node-attacker").className = "attacker-node"; $("#journey").classList.remove("running");
  text("#threat-badge", "Waiting"); updateProtection();
  text("#journey-state", "Ready to explore"); $("#journey-state").className = "badge";
  $("#block-transactions").replaceChildren(node("li", "empty-state", "Run a scenario to reveal the actual transaction order."));
  text("#block-number", "No block yet"); text("#event-count", "0 events");
  $("#event-stream").replaceChildren(); show("#guardian-verdict", false); show("#prediction-panel", false);
  text("#outcome-title", "Run an experiment to see the difference"); text("#outcome-detail", "Measured results appear here after the contracts execute.");
  text("#harm-value", "—"); text("#output-comparison", "Awaiting execution"); $("#result-strip").className = "result-strip";
  caption("What happens next?", scenarios[scenario].description, "↗");
}

// The narrative is paced from actual backend events, and can replay without resubmitting a transaction.
async function enqueue(event) {
  queue.push(event); if (draining) return;
  draining = true; updateControls();
  while (queue.length) {
    const ev = queue.shift();
    if (ev.kind === "run" && ev.busy) {
      // Preserve the scenario sent by the server, including when another viewer starts a run.
      serverBusy = false; draining = false;
      if (ev.scenario) {
        chooseScenario(ev.scenario, true); protection = !!ev.protected;
        if (ev.amount != null) $("#amount").value = ev.amount;
        if (ev.defense_choice) $("#defense-choice").value = ev.defense_choice;
        if (ev.risk_mode) $("#risk-mode").value = ev.risk_mode;
        if (ev.scenario === "oracle") $("#oracle-choice").value = ev.target === "safe" ? "safe" : "guardian";
        updateProtection();
      }
      draining = true; serverBusy = true; clearStory(); runScenario = scenario; runProtected = protection;
      text("#playback-source", "Live local contract execution"); $("#journey").classList.add("running");
    } else if (ev.kind === "run") {
      serverBusy = false; $("#journey").classList.remove("running");
    } else {
      if (["baseline", "phase", "block", "execution", "prediction", "alert", "result", "log"].includes(ev.kind)) story.push(ev);
      renderEvent(ev);
    }
    const duration = ["phase", "execution"].includes(ev.kind) ? 1400 : ev.kind === "block" ? 900 : ev.kind === "result" ? 700 : 70;
    await sleep(duration * Number($("#playback-speed").value));
  }
  draining = false; updateControls();
}
$("#replay").addEventListener("click", async () => {
  if (isBusy() || !story.length) return;
  const recording = [...story]; chooseScenario(runScenario); protection = runProtected; updateProtection();
  replaying = true; clearStory(false); updateControls(); text("#playback-source", "Replay · no new transactions");
  for (const ev of recording) { renderEvent(ev); await sleep((["phase", "execution"].includes(ev.kind) ? 1500 : ev.kind === "block" ? 1100 : 100) * Number($("#playback-speed").value)); }
  replaying = false; updateControls();
});
const phaseCopy = {
  intercept: ["Your trade enters the journey", "The app reads your swap intent before it reaches the trading pool.", "user"],
  expose: ["The trade becomes visible to bots", "Pending transactions wait in the public mempool. A searcher can calculate whether buying before you would pay.", "user"],
  predict: ["Try the trade before risking it", "ML estimates risk first; uncertain decisions and proposed active protections are checked on a private copy of the chain before the cost comparison is finalized.", "shield"],
  prevent: ["Change the conditions of the trade", "The selected defense changes the price bound, submission route, or timing before the trade executes.", "shield"],
  attack: ["Borrowed funds enter the picture", "The attacker attempts to turn temporary borrowed funds into a lasting loss for the target protocol.", "user"],
  detect: ["Look for harm, not just a flash loan", "The Guardian replays the pending transaction and examines price changes, protocol losses and sender profit.", "shield"],
  arbitrage: ["Trade the difference between two prices", "The trader borrows temporarily, buys where it is cheaper, and sells where it is more expensive.", "user"]
};
function renderEvent(ev) {
  if (ev.kind === "baseline") { priceSamples = []; renderStatus(ev.snapshot); return; }
  if (ev.kind === "reset") { priceSamples = []; clearStory(); text("#playback-source", "Lab reset · fresh state"); return; }
  if (ev.kind === "status") return renderStatus(ev);
  if (ev.kind === "mempool" && ev.txs?.length) {
    const pending = ev.txs[0];
    if (pending.amount_usd != null) text("#user-node-detail", `${number(pending.amount_usd, 0)} USDC → WETH`);
  }
  if (ev.kind === "phase") {
    const [title, detail, active] = phaseCopy[ev.name] || [ev.label, "Follow the next contract operation.", "pool"];
    $$(".flow-node").forEach((el) => el.classList.remove("active")); $("#node-" + active).classList.add("active");
    caption(title, detail, String(++steps).padStart(2, "0")); text("#journey-state", ev.name === "predict" ? "Testing outcomes" : "In progress");
    if (ev.name === "expose" || ev.name === "attack") { $("#node-attacker").classList.add("attacking"); text("#threat-badge", "Opportunity"); }
    if (ev.name === "prevent") text("#shield-node-detail", ev.label.replace("Prevention — ", ""));
  }
  if (ev.kind === "log") addLog(ev);
  if (ev.kind === "block") renderBlock(ev);
  if (ev.kind === "execution") renderExecution(ev);
  if (ev.kind === "prediction") renderPrediction(ev);
  if (ev.kind === "alert") {
    show("#guardian-verdict"); $("#guardian-verdict").className = ev.label === "CRITICAL" ? "critical" : "";
    text("#guardian-verdict", `Guardian: ${ev.label}. ${(ev.reasons || []).join(" · ")}`);
    caption(ev.label === "BENIGN" ? "The Guardian lets this one through" : "The Guardian found a threat", (ev.reasons || []).join(". "), "!");
  }
  if (ev.kind === "result") renderResult(ev);
}
function addLog(ev) {
  const row = node("div", "event-row " + (ev.level || "info"));
  row.append(node("time", "", new Date((ev.ts || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour12: false })), node("span", "", ev.text));
  const stream = $("#event-stream"); stream.append(row); if (stream.children.length > 150) stream.firstElementChild.remove();
  stream.scrollTop = stream.scrollHeight; ++eventCount; text("#event-count", `${eventCount} ${eventCount === 1 ? "event" : "events"}`);
}
function renderStatus(state) {
  if (state.ready != null) {
    labReady = state.ready;
    $("#lab-dot").className = "status-dot " + (labReady ? "connected" : "error");
    text("#lab-status", labReady ? `Local Ethereum lab online · Block ${number(state.block, 0)} · Real Solidity execution on Anvil` : "Local lab offline. Run ‘mevshield start’ to launch it. Live Sepolia browsing is available independently.");
  }
  if (state.spot != null && state.twap != null) {
    text("#spot-value", money(state.spot)); text("#twap-value", money(state.twap));
    const previous = priceSamples.at(-1);
    if (!previous || previous.block !== state.block) priceSamples.push({ block: state.block, spot: state.spot, twap: state.twap });
    priceSamples = priceSamples.slice(-12); renderChart();
  }
  if (state.lending) { text("#funds-vulnerable", money(state.lending.vulnerable)); text("#funds-safe", money(state.lending.safe)); }
  updateControls();
}
const txNames = { frontrun: "Bot buys before your trade", victim: "Your trade executes", backrun: "Bot sells after your trade", defense: "Defense executes first", attack: "Attack transaction", arbitrage: "Buy, sell & repay the flash loan", other: "Contract operation" };
function renderBlock(ev) {
  const block = $("#block-transactions");
  if (block.querySelector(".empty-state")) block.replaceChildren();
  text("#block-number", `Local block ${number(ev.number, 0)}`);
  (ev.txs || []).forEach((tx, index) => {
    const attack = ["frontrun", "backrun", "attack"].includes(tx.kind);
    const row = node("li", `transaction-row ${attack ? "attack" : tx.kind === "defense" ? "defense" : ""} ${tx.status ? "" : "reverted"}`);
    row.title = `Block ${ev.number} · ${tx.hash}`;
    row.dataset.hash = tx.hash;
    const description = node("div", "tx-description");
    description.append(node("strong", "", txNames[tx.kind] || "Contract operation"), node("small", "", `${tx.fn || tx.role} · ${short(tx.hash)}`));
    row.append(node("span", "tx-order", String(index + 1).padStart(2, "0")), description, node("span", "tx-result", tx.status ? tx.private ? "Private" : "Executed" : "Reverted"));
    block.append(row);
  });
  $("#node-pool").classList.add("active");
  caption("The block records the real order", "Read from top to bottom. A reverted transaction changed no contract balances; a successful one executed its calls.", String(++steps).padStart(2, "0"));
}
function renderExecution(ev) {
  const tx = ev.tx;
  $$(".transaction-row").forEach((row) => row.classList.toggle("executing", row.dataset.hash === tx.hash));
  const trades = ev.swaps.map((swap) => `${number(swap.amount_in, 5)} ${swap.token_in} → ${number(swap.amount_out, 5)} ${swap.token_out} on Pool ${swap.pool === "ammA" ? "A" : "B"}`);
  const explanations = {
    frontrun: "The bot buys first, moving the price before your trade can execute.",
    victim: "Your trade executes at the pool’s current price, subject to your minimum output.",
    backrun: "The bot sells after your trade. Its two trades surround yours inside the same block.",
    defense: "The defense executes in this position. The next transaction sees its effects.",
    attack: "The attack’s operations execute atomically within this transaction.",
    arbitrage: "Both trades and the flash-loan repayment happen in a single atomic transaction."
  };
  caption(tx.status ? txNames[tx.kind] || "Contract operation" : "The contract rejected this transaction", tx.status ? (trades.join(". ") || explanations[tx.kind] || tx.fn) : "The transaction reverted. No contract state changes or swap events survived; gas was spent.", String(++steps).padStart(2, "0"));
  if (tx.kind === "frontrun") { $("#node-attacker").classList.add("attacking"); text("#threat-badge", "Buying first"); }
  if (tx.kind === "backrun") text("#threat-badge", "Selling after you");
  for (const observation of ev.price_steps.filter((step) => step.pool === "ammA")) {
    priceSamples.push({ block: ev.block, spot: observation.price, twap: ev.twap });
    priceSamples = priceSamples.slice(-12);
    text("#spot-value", money(observation.price)); text("#twap-value", money(ev.twap));
    text("#pool-node-detail", money(observation.price) + " / WETH");
  }
  if (priceSamples.length) renderChart();
}
const defenseNames = { public: "Public transaction", "adaptive-slippage": "Tighter price limit", "private-relay": "Private relay", "commit-reveal": "Commit, then reveal", split: "Smaller trade chunks" };
function renderPrediction(ev) {
  show("#prediction-panel"); text("#prediction-meta", `${ev.decision?.requested_mode || "sim"} · ${number(ev.decision?.decision_ms ?? ev.sim_ms, 0)} ms · ${ev.decision?.shadow_bundles ?? "—"} shadow bundles`);
  const body = $("#defense-rows"); body.replaceChildren();
  ev.defenses.forEach((defense) => {
    const row = node("tr", defense.chosen ? "chosen" : "");
    [defenseNames[defense.name] + (defense.chosen ? " · Recommended" : ""), money(defense.harm), money(defense.gas), defense.latency, number(defense.revert * 100, 1) + "%", money(defense.J)].forEach((value) => row.append(node("td", "", value)));
    body.append(row);
  });
  text("#prediction-summary", `Public harm estimate: ${money(ev.harm_public)} from ${ev.decision?.risk_source || "shadow-fork"}. Recommended: ${defenseNames[ev.recommendation]}. Verification: ${ev.decision?.verification || "shadow-fork"}. ${(ev.decision?.fallback_reasons || []).join("; ")}. These are conditional local-lab estimates.`);
  caption("The shield has compared the options", `Predicted public-trade loss: ${money(ev.harm_public)}. The lowest total score belongs to ${defenseNames[ev.recommendation].toLowerCase()}.`, "↗");
}
function renderResult(ev) {
  $("#journey").classList.remove("running");
  const lost = ev.outcome === "attacked", benign = ev.outcome === "benign", reverted = ev.outcome === "reverted";
  $("#node-attacker").className = "attacker-node " + (lost ? "attacking" : "blocked");
  text("#threat-badge", lost ? "Value extracted" : benign ? "Benign activity" : reverted ? "Trade reverted" : ev.sandwiched ? "Low realised harm" : "No value lost");
  $("#node-pool").classList.toggle("danger", lost);
  $("#journey-state").className = "badge " + (lost ? "bad" : "good");
  text("#journey-state", lost ? "Attack succeeded" : benign ? "Benign" : reverted ? "Trade reverted" : "Funds preserved");
  text("#outcome-title", ev.title); text("#outcome-detail", ev.detail); text("#harm-value", money(ev.harm_usd || 0));
  text("#output-comparison", ev.actual_out != null ? `Expected ${number(ev.expected_out, 6)} → received ${number(ev.actual_out, 6)} WETH` : ev.liquidity_before != null ? `${money(ev.liquidity_before)} → ${money(ev.liquidity_after)} in the pool` : "Measured protocol loss");
  $("#result-strip").className = "result-strip " + (lost ? "attacked" : "");
  caption(lost ? "This is where the value was lost" : benign ? "Useful activity, allowed through" : reverted ? "The trade was stopped" : "The defense preserved the funds", ev.detail, "✓");
}
function renderChart() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg"); svg.setAttribute("viewBox", "0 0 440 110"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Observed Pool A market and average prices from transaction receipt reserve events and block snapshots");
  const add = (tag, attrs, content) => { const e = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v)); if (content) e.textContent = content; svg.append(e); return e; };
  const values = priceSamples.flatMap((sample) => [sample.spot, sample.twap]);
  const low = Math.min(...values), high = Math.max(...values), range = Math.max(high - low, high * 0.02, 1);
  const bottom = low - range * 0.2, top = high + range * 0.2;
  const y = (value) => 85 - ((value - bottom) / (top - bottom)) * 75;
  const x = (index) => priceSamples.length === 1 ? 245 : 55 + index * 370 / (priceSamples.length - 1);
  for (const value of [top, (top + bottom) / 2, bottom]) {
    add("line", { x1: 52, x2: 428, y1: y(value), y2: y(value), class: "chart-grid" });
    add("text", { x: 0, y: y(value) + 3, class: "chart-label" }, number(value, 0));
  }
  for (const key of ["spot", "twap"]) {
    const points = priceSamples.map((sample, i) => `${x(i)},${y(sample[key])}`).join(" ");
    add("polyline", { points, class: key === "spot" ? "spot-line" : "twap-line" });
    priceSamples.forEach((sample, i) => add("circle", { cx: x(i), cy: y(sample[key]), r: key === "spot" ? 2.8 : 1.8, fill: key === "spot" ? "#087f78" : "#648cbe" }));
  }
  add("text", { x: 55, y: 106, class: "chart-label" }, `Block ${priceSamples[0].block}`);
  if (priceSamples.length > 1) add("text", { x: 428, y: 106, class: "chart-label", "text-anchor": "end" }, `Block ${priceSamples.at(-1).block}`);
  $("#price-chart").replaceChildren(svg);
}
let socket, reconnectTimer;
function connectStream() {
  socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket.onmessage = (event) => { try { enqueue(JSON.parse(event.data)); } catch { toast("A lab update could not be read."); } };
  socket.onclose = () => { serverBusy = false; labReady = false; updateControls(); text("#lab-status", "Lab event stream disconnected. Reconnecting…"); reconnectTimer = setTimeout(connectStream, 3000); };
}

// Public network reads use the backend; wallet signatures remain in the browser wallet.
let wallet = null, walletAccount = null, walletChain = null, walletBusy = false, vaultAddress = null, vaultState = null, networkRefreshing = false;
const storage = { get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } }, set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Private browsing may disable persistence. */ } }, remove(key) { try { localStorage.removeItem(key); } catch {} } };
vaultAddress = storage.get("mevshield.sepolia.vault");
if (typeof vaultAddress !== "string" || !/^0x[0-9a-f]{40}$/i.test(vaultAddress)) vaultAddress = null;
if (vaultAddress) $("#vault-address").value = vaultAddress;
function walletMessage(message, kind = "") { text("#wallet-message", message); $("#wallet-message").className = "wallet-message " + kind; }
function walletControls() {
  const ready = walletAccount && walletChain === SEPOLIA;
  const attached = ready && vaultState && vaultAddress;
  $("#connect-wallet").disabled = walletBusy;
  $("#deploy-vault").disabled = walletBusy;
  $("#attach-vault").disabled = walletBusy;
  $("#deposit-vault").disabled = walletBusy || !attached;
  $("#withdraw-vault").disabled = walletBusy || !attached;
  $("#pause-vault").disabled = walletBusy || !attached || vaultState.owner.toLowerCase() !== walletAccount.toLowerCase();
  text("#connect-wallet", walletAccount ? walletChain === SEPOLIA ? short(walletAccount) : "Switch to Sepolia ↗" : "Connect wallet ↗");
  text("#wallet-description", walletAccount ? `${short(walletAccount)} · ${walletChain === SEPOLIA ? "Connected to Sepolia" : "Different network selected"}` : "Use an Ethereum browser wallet, such as MetaMask.");
}
async function connectWallet() {
  wallet = WalletConnection.pickProvider(window.ethereum, [...announcedWallets]);
  if (!wallet) { openWalletSetup(); throw Object.assign(new Error("No Ethereum wallet detected. Follow the setup instructions, then open this page in your wallet-enabled browser."), { code: "NO_WALLET" }); }
  walletMessage("Open your wallet extension, unlock it, and approve the connection request.");
  const connected = await WalletConnection.connect(wallet);
  walletAccount = connected.account;
  walletChain = connected.chainId;
  bindWallet(); walletControls();
  walletMessage("Wallet connected to Ethereum Sepolia. Deploy a vault or attach an existing one.", "success");
  if (vaultAddress) await refreshVault().catch(() => {});
  return walletAccount;
}
const announcedWallets = new Set();
window.addEventListener("eip6963:announceProvider", (event) => {
  if (typeof event.detail?.provider?.request === "function") announcedWallets.add(event.detail.provider);
});
window.dispatchEvent(new Event("eip6963:requestProvider"));
function openWalletSetup() {
  $("#wallet-demo-url").value = location.origin + "/#network";
  if (!$("#wallet-setup").open) $("#wallet-setup").showModal();
}
$("#close-wallet-setup").addEventListener("click", () => $("#wallet-setup").close());
$("#copy-demo-url").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#wallet-demo-url").value); text("#wallet-setup-status", "Link copied. Paste it into the browser where you installed MetaMask."); }
  catch { $("#wallet-demo-url").focus(); $("#wallet-demo-url").select(); text("#wallet-setup-status", "Select and copy the address above, then open it in your wallet-enabled browser."); }
});
let boundWallet;
function bindWallet() {
  if (boundWallet === wallet || !wallet.on) return;
  boundWallet = wallet;
  wallet.on("accountsChanged", (accounts) => {
    walletAccount = accounts[0] || null; vaultState = null; clearVaultDisplay(); walletControls();
    walletMessage(walletAccount ? "Wallet account changed. Refreshing your vault balance…" : "Wallet disconnected.");
    if (walletAccount && vaultAddress) refreshVault().catch(() => {});
  });
  wallet.on("chainChanged", (chain) => {
    walletChain = chain.toLowerCase(); vaultState = null; clearVaultDisplay(); walletControls();
    walletMessage(walletChain === SEPOLIA ? "Sepolia selected. Refreshing your vault…" : "This app sends transactions only on Sepolia. Switch networks to continue.");
    if (walletChain === SEPOLIA && vaultAddress && walletAccount) refreshVault().catch(() => {});
  });
}
async function requestWalletConnection() {
  if (walletBusy) return;
  walletBusy = true; walletControls(); $("#retry-wallet").disabled = true;
  text("#connect-wallet", "Open your wallet…");
  try {
    await connectWallet();
    if ($("#wallet-setup").open) $("#wallet-setup").close();
    toast("Wallet connected to Ethereum Sepolia.");
  } catch (error) {
    const message = WalletConnection.errorMessage(error);
    walletMessage(message, "error");
    if ($("#wallet-setup").open) text("#wallet-setup-status", message); else toast(message);
  } finally { walletBusy = false; walletControls(); $("#retry-wallet").disabled = false; }
}
$("#connect-wallet").addEventListener("click", requestWalletConnection);
$("#retry-wallet").addEventListener("click", requestWalletConnection);
function formatEth(wei) {
  const value = BigInt(wei), whole = value / 10n ** 18n, fraction = (value % 10n ** 18n).toString().padStart(18, "0").replace(/0+$/, "");
  return String(whole) + (fraction ? "." + fraction : "");
}
function parseEth(value) {
  if (!/^(?:0|[1-9]\d*)(?:\.\d{1,18})?$/.test(value)) throw new Error("Enter an ETH amount with at most 18 decimal places.");
  const [whole, fraction = ""] = value.split(".");
  const wei = BigInt(whole) * 10n ** 18n + BigInt(fraction.padEnd(18, "0"));
  if (wei <= 0n || wei > 10n ** 16n) throw new Error("Choose more than 0 and at most 0.01 test ETH.");
  return wei.toString();
}
function clearVaultDisplay() {
  text("#vault-title", vaultAddress ? "Vault needs a fresh read" : "No vault connected");
  text("#vault-balance", "— ETH"); text("#cooldown-state", "60 seconds after deposit"); text("#limit-state", "0.005 ETH / wallet"); text("#pause-state", "Owner controlled");
  $("#vault-dot").className = "status-dot"; show("#vault-explorer", false);
}
async function refreshVault(address = vaultAddress) {
  if (!address || !walletAccount) return;
  const account = walletAccount;
  try {
    const state = await api(`/api/network/vault?${new URLSearchParams({ address, account })}`);
    if (walletAccount !== account || walletChain !== SEPOLIA) return;
    vaultAddress = state.address; vaultState = state; storage.set("mevshield.sepolia.vault", vaultAddress);
    text("#vault-title", "ShieldVault " + short(vaultAddress)); $("#vault-address").value = vaultAddress;
    $("#vault-dot").className = "status-dot connected";
    text("#vault-balance", formatEth(state.balance_wei) + " ETH");
    text("#cooldown-state", state.unlock_at > state.timestamp ? `${state.unlock_at - state.timestamp}s remaining at block ${state.block}` : "Ready to withdraw");
    text("#limit-state", `${formatEth(state.remaining_wei)} ETH available`);
    text("#pause-state", state.paused ? "Paused · transfers blocked" : "Active · transfers allowed");
    text("#pause-vault", state.paused ? "Unpause vault (owner)" : "Pause vault (owner)");
    $("#vault-explorer").href = EXPLORER + "/address/" + vaultAddress; show("#vault-explorer"); walletControls();
    return state;
  } catch (error) { vaultState = null; clearVaultDisplay(); walletControls(); walletMessage(error.message, "error"); throw error; }
}
$("#attach-vault").addEventListener("click", async () => {
  if (walletBusy) return; walletBusy = true; walletControls();
  try {
    const address = $("#vault-address").value.trim();
    if (!/^0x[0-9a-f]{40}$/i.test(address)) throw new Error("Enter a valid 42-character vault address, beginning with 0x.");
    if (!walletAccount || walletChain !== SEPOLIA) await connectWallet();
    await refreshVault(address); walletMessage("Vault bytecode verified on Sepolia. Your balances and defenses are now visible.", "success");
  }
  catch (error) { walletMessage(error.message, "error"); } finally { walletBusy = false; walletControls(); }
});
function transactionStep(step, failed = false) {
  show("#transaction-progress");
  $$(".tx-step").forEach((el) => { el.classList.toggle("active", el.dataset.step === step && !failed); el.classList.toggle("failed", el.dataset.step === step && failed); });
}
function transactionLink(hash) {
  $("#wallet-message").append(document.createTextNode(" "), explorerLink("/tx/" + hash, "View transaction ↗"));
}
function addReceipt(receipt, action) {
  const row = node("div", "chain-receipt"); const content = node("div");
  content.append(node("strong", "", `${action[0].toUpperCase() + action.slice(1)} · ${receipt.status ? "Included successfully" : "Reverted on-chain"}`));
  content.append(node("small", "", `Sepolia block ${number(receipt.block, 0)} · ${number(receipt.gas_used, 0)} gas · Inclusion is not finality`));
  if (receipt.events.length) content.append(node("small", "", receipt.events.map((event) => `${event.name}(${Object.entries(event.args).map(([key, value]) => `${key}: ${key === "user" || key === "by" ? short(value) : value}`).join(", ")})`).join(" · ")));
  row.append(content, explorerLink("/tx/" + receipt.hash, "Receipt ↗")); $("#chain-receipts").prepend(row);
}
async function finishReceipt(pending, receipt) {
  if (storage.get("mevshield.sepolia.pending")?.hash === pending.hash) storage.remove("mevshield.sepolia.pending");
  addReceipt(receipt, pending.action);
  transactionStep("done", !receipt.status);
  if (!receipt.status) { walletMessage("Transaction reverted on-chain. Its state changes were rolled back; gas was spent.", "error"); transactionLink(pending.hash); return; }
  if (pending.action === "deploy" && receipt.contract_address) {
    vaultAddress = receipt.contract_address; storage.set("mevshield.sepolia.vault", vaultAddress); $("#vault-address").value = vaultAddress;
  }
  walletMessage("Transaction included on Sepolia. The receipt below is the on-chain evidence.", "success"); transactionLink(pending.hash);
  if (walletAccount && vaultAddress) await refreshVault();
}
let pendingChecking = false;
function stopTrackingControl(pending) {
  const button = node("button", "text-button", "Stop tracking this hash");
  button.addEventListener("click", () => {
    if (storage.get("mevshield.sepolia.pending")?.hash === pending.hash) storage.remove("mevshield.sepolia.pending");
    walletMessage("Tracking stopped in this browser. This does not cancel the transaction; it may still execute. Follow any replacement or cancellation in your wallet.");
    transactionLink(pending.hash);
  });
  $("#wallet-message").append(document.createTextNode(" · "), button);
}
async function checkPending() {
  const pending = storage.get("mevshield.sepolia.pending");
  if (!pending || pendingChecking || walletBusy) return;
  if (!/^0x[0-9a-f]{64}$/i.test(pending.hash || "") || !["deploy", "deposit", "withdraw", "pause", "unpause"].includes(pending.action)) { storage.remove("mevshield.sepolia.pending"); return; }
  pendingChecking = true;
  try {
    const receipt = await api("/api/network/receipt/" + pending.hash);
    if (storage.get("mevshield.sepolia.pending")?.hash !== pending.hash) return;
    if (!receipt.pending) await finishReceipt(pending, receipt);
    else { transactionStep("mine"); walletMessage("A previously submitted transaction is still pending. Its status is checked automatically."); transactionLink(pending.hash); stopTrackingControl(pending); }
  }
  catch { /* Keep the hash so a temporary RPC error cannot lose a submitted transaction. */ }
  finally { pendingChecking = false; }
}
async function transact(action) {
  if (walletBusy) return;
  if (storage.get("mevshield.sepolia.pending")) { walletMessage("A transaction is still pending. Wait for its receipt before starting another action."); await checkPending(); return; }
  walletBusy = true; walletControls(); let currentStep = "check", submitted = null;
  try {
    transactionStep(currentStep); if (!walletAccount || walletChain !== SEPOLIA) await connectWallet();
    const account = walletAccount;
    const amount_wei = ["deposit", "withdraw"].includes(action) ? parseEth($("#vault-amount").value.trim()) : "0";
    walletMessage("Checking the contract’s rules and estimating gas…");
    const prepared = await api("/api/network/prepare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, account, address: vaultAddress, amount_wei }) });
    currentStep = "sign"; transactionStep(currentStep);
    const chain = (await wallet.request({ method: "eth_chainId" })).toLowerCase();
    const accounts = await wallet.request({ method: "eth_accounts" });
    if (chain !== SEPOLIA || !accounts[0] || accounts[0].toLowerCase() !== account.toLowerCase()) throw new Error("Wallet account or network changed. Start the action again.");
    walletMessage(`Policy check passed. Review the ${action} transaction in your wallet.`);
    const hash = await wallet.request({ method: "eth_sendTransaction", params: [prepared.transaction] });
    submitted = { hash, action, account, address: vaultAddress }; storage.set("mevshield.sepolia.pending", submitted);
    currentStep = "mine"; transactionStep(currentStep); walletMessage("Submitted to Sepolia. Waiting for network inclusion…"); transactionLink(hash);
    for (let i = 0; i < 40; i++) {
      await sleep(3000);
      const receipt = await api("/api/network/receipt/" + hash);
      if (!receipt.pending) { await finishReceipt(submitted, receipt); return; }
    }
    walletMessage("Still waiting for inclusion. Automatic checks will continue; the explorer also tracks your transaction."); transactionLink(hash);
  } catch (error) {
    transactionStep(currentStep, true);
    const message = error.code === 4001 ? "Wallet request declined. No new transaction was submitted by this request." : error.message;
    walletMessage(submitted ? "Transaction was submitted, but the receipt could not be confirmed yet. Automatic checks will continue." : message, submitted ? "" : "error");
    if (submitted) transactionLink(submitted.hash);
  } finally { walletBusy = false; walletControls(); }
}
$("#deploy-vault").addEventListener("click", () => transact("deploy"));
$("#deposit-vault").addEventListener("click", () => transact("deposit"));
$("#withdraw-vault").addEventListener("click", () => transact("withdraw"));
$("#pause-vault").addEventListener("click", () => transact(vaultState?.paused ? "unpause" : "pause"));
async function refreshNetwork() {
  if (networkRefreshing) return; networkRefreshing = true;
  try {
    const state = await api("/api/network");
    const connected = state.connected;
    text("#side-network", connected ? "Sepolia connected" : "Sepolia unavailable");
    $("#side-network-dot").className = "status-dot " + (connected ? "connected" : "error");
    text("#network-badge", connected ? "Sepolia · live network" : "Sepolia · unavailable");
    text("#live-connection", connected ? "Connected" : "Unavailable");
    text("#live-source", connected ? "Verified chain 11155111" : "Connection lost");
    if (connected) {
      text("#live-height", number(state.block, 0)); text("#live-fee", number(state.base_fee_gwei, 9));
      text("#live-updated", "Observed " + new Date(state.observed_at * 1000).toLocaleTimeString());
      const wrap = $("#live-blocks"); wrap.replaceChildren();
      state.blocks.forEach((block, index) => {
        const card = node("div", "live-block");
        card.append(node("span", "mini-label", index === 0 ? "LATEST BLOCK" : "PREVIOUS BLOCK"), node("strong", "", "#" + number(block.number, 0)), node("small", "", `${number(block.transactions, 0)} transactions`), node("small", "", `${number(block.gas_used / block.gas_limit * 100, 1)}% gas capacity`), explorerLink("/block/" + block.number, "Verify block ↗"));
        wrap.append(card);
      });
    } else {
      text("#live-height", "—"); text("#live-fee", "—"); text("#live-updated", state.error || "The public RPC is unavailable.");
      $("#live-blocks").replaceChildren(node("p", "empty-state", "No live data available. Retrying automatically; check the RPC configuration if this persists."));
    }
    if (walletAccount && vaultAddress && !walletBusy) await refreshVault().catch(() => {});
    await checkPending();
  } catch {
    text("#live-connection", "Unavailable"); text("#network-badge", "Sepolia · connection lost"); text("#live-updated", "Could not reach the server. Retrying automatically.");
    text("#side-network", "Sepolia connection lost"); $("#side-network-dot").className = "status-dot error";
  } finally { networkRefreshing = false; }
}

chooseScenario("sandwich"); walletControls(); connectStream(); refreshNetwork();
setInterval(refreshNetwork, 12000);
setInterval(async () => {
  if (isBusy()) return;
  try { renderStatus(await api("/api/status")); } catch { labReady = false; updateControls(); text("#lab-status", "Unable to reach the lab server. Reconnecting automatically…"); }
}, 15000);
