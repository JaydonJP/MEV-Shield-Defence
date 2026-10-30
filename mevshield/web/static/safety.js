"use strict";
const q = s => document.querySelector(s);
const node = (tag, text, cls) => { const el = document.createElement(tag); if (text != null) el.textContent = text; if (cls) el.className = cls; return el; };
let current = null, busy = false, generation = 0;
const money = v => v > 0 && v < .01 ? "<$0.01" : `$${v.toFixed(2)}`;
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Check the inputs and try again.");
  return data;
}
function notice(text, error = false) { q("#notice").textContent = text; q("#notice").classList.toggle("error", error); }
function evidence(parent, value) { const d = node("details"); d.append(node("summary", "Inspect or copy the full evidence"), node("pre", JSON.stringify(value, null, 2))); parent.append(d); }
function table(parent, headings, rows) { const wrap = node("div", null, "safety-table-wrap"), t = node("table"), head = node("tr"); headings.forEach(h => head.append(node("th", h))); t.append(head); for (const row of rows) { const tr = node("tr"); row.forEach(v => tr.append(node("td", v))); t.append(tr); } wrap.append(t); parent.append(wrap); }
function tokens(raw) { const value = BigInt(raw), negative = value < 0n, abs = negative ? -value : value; return (negative ? "−" : "+") + (abs / 10n ** 18n).toString() + "." + (abs % 10n ** 18n).toString().padStart(18, "0").slice(0, 6); }
function report(r) {
  const parent = q("#report"); parent.replaceChildren(node("h2", r.status === "blocked" ? "Contract policy blocked this action" : "Transaction report"));
  parent.append(node("p", `${r.action} · ${r.role} · chain ${r.chain_id} · simulated at block ${r.block}`));
  parent.append(node("strong", r.status, r.status === "blocked" || r.status === "reverted" ? "status-blocked" : "status-pass"));
  if (r.reason) parent.append(node("p", r.reason));
  table(parent, ["Change", "Predicted tokens", "Observed tokens"], Object.entries(r.predicted_changes).map(([k,v]) => [k, tokens(v), r.receipt ? tokens(r.receipt.observed_changes[k]) : "Awaiting execution"]));
  parent.append(node("p", `Estimated gas: ${r.gas_estimate.toLocaleString()} units (about $${r.estimated_gas_usd.toFixed(4)} at the lab price).`));
  if (r.min_out != null) parent.append(node("p", `Minimum received: ${tokens(r.min_out).replace("+", "")} ${r.token_in === "USDC" ? "WETH" : "USDC"}; deadline ${new Date(r.deadline * 1000).toLocaleString()}.`));
  if (r.approval) parent.append(node("p", `Existing ${r.approval.token} allowance to ${r.approval.spender}: ${r.approval.unlimited ? "unlimited" : r.approval.allowance}. No new approval requested.`));
  const warnings = node("ul"); r.warnings.forEach(w => warnings.append(node("li", w))); parent.append(warnings);
  if (r.receipt) { parent.append(node("p", `Receipt: ${r.receipt.hash}`), node("p", r.receipt.reconciliation.matches ? "Observed changes match the preview. Gas paid is recorded separately." : "Observed changes differ from the preview; inspect reconciliation evidence.")); }
  if (r.status === "previewed") {
    const button = node("button", "Execute this exact local transaction", "button primary"); button.id = "execute-preview";
    button.addEventListener("click", async () => { if (busy || current?.id !== r.id) return; busy = true; button.disabled = true; notice("Submitting the reviewed local transaction…"); try { const result = await api("/api/safety/execute", {report_id:r.id,binding:r.binding}); current = null; report(result); notice(result.status === "included" ? "Transaction included. Review its actual changes below." : "Transaction reverted. Review the receipt below.", result.status !== "included"); await history(); } catch(e) { current = null; notice(e.message, true); } finally { busy = false; } });
    parent.append(button, node("p", "This preview expires in two minutes. New chain activity requires another simulation."));
  }
  evidence(parent, r);
}
q("#preview-form").addEventListener("input", () => { generation++; current = null; const button=q("#execute-preview"); if (button) button.disabled=true; });
q("#preview-form").addEventListener("submit", async e => {
  e.preventDefault(); if (busy) return; busy=true; current=null;
  const request = ++generation, form=e.currentTarget, button=form.querySelector("button[type=submit]"); button.disabled=true;
  const old=q("#execute-preview"); if(old) old.disabled=true;
  const body=Object.fromEntries(new FormData(form)); body.amount=Number(body.amount); body.slippage=Number(body.slippage)/100;
  notice("Simulating on the shadow fork. No main-chain transaction is submitted during preview.");
  try { const result=await api("/api/safety/preview",body); if (request !== generation) { notice("Inputs changed while simulating; create a new preview."); return; } current=result; report(result); notice(result.simulation_passed ? "Preview ready. Review the changes and warnings before executing." : "The contract rejected this action in simulation.", !result.simulation_passed); await history(); } catch(e) {notice(e.message,true);} finally {busy=false;button.disabled=false;}
});
let riskGeneration = 0;
q("#risk-form").addEventListener("input", () => {
  riskGeneration++;
  q("#risk-result").replaceChildren(node("p", "Inputs changed. Assess risk again to update the result."));
});
q("#risk-form").addEventListener("submit",async e=>{
  e.preventDefault(); const button=e.currentTarget.querySelector("button"); button.disabled=true; const data=Object.fromEntries(new FormData(e.currentTarget)), request=++riskGeneration;
  try { const result=await api("/api/risk",{amount:Number(data.amount),slippage:Number(data.slippage)/100}); if(request !== riskGeneration) return; const parent=q("#risk-result"); parent.replaceChildren(node("h3",`${result.status} · ${result.source}`)); parent.append(node("p", result.harm_usd == null ? "Risk is unknown. No safe verdict is inferred." : `Estimated sandwich harm: ${money(result.harm_usd)}.`)); if(result.interval_usd) parent.append(node("p",`90% nominal calibration interval: $${result.interval_usd[0].toFixed(2)} – $${result.interval_usd[1].toFixed(2)}. Coverage can fail under drift.`)); for(const reason of result.reasons || []) parent.append(node("p",reason)); evidence(parent,result); } catch(e){if(request === riskGeneration)notice(e.message,true);} finally{button.disabled=false;}
});
async function history(){const rows=await api("/api/safety/history"),parent=q("#history");parent.replaceChildren();if(!rows.length)parent.append(node("p","No previews recorded yet."));else table(parent,["Action","User","State","Receipt"],rows.map(r=>[r.action,r.role,r.status,r.receipt?.hash || "—"]));}
async function verification(){const result=await api("/api/verification"),parent=q("#verification-result");parent.replaceChildren(node("p",result.status === "passed" ? "Verification passed. Inspect the recorded commands, seed and mutation results." : result.message || `Recorded verification status: ${result.status}`));evidence(parent,result);}
q("#refresh-history").addEventListener("click",()=>history().catch(e=>notice(e.message,true)));
q("#refresh-verification").addEventListener("click",()=>verification().catch(e=>notice(e.message,true)));
api("/api/status").then(s=>notice(s.ready ? "Local chain ready. Choose an action to preview." : "Local lab is offline. Start it with mevshield start --build to preview transactions.")).catch(e=>notice(e.message,true));
history().catch(e=>notice(e.message,true));verification().catch(e=>notice(e.message,true));

api("/api/model").then(data=>{
  for(const [address,pool] of Object.entries(data.pools)){const option=node("option",pool.name);option.value=address;q("#mainnet-pool").append(option);}
  const parent=q("#model-evidence"), report=data.metrics?.report;
  if(report){parent.append(node("p",report.evaluation_scope),node("p","Trade-size and slippage policies are explicit assumptions. Labels measure modeled economics, not confirmed attacks. The observed-size holdout has no profitable cases, so observed-traffic attack recall is not established."));const m=report.test;table(parent,["Measure","Held-out value"],[["Observed mainnet inputs",report.observed_input_rows],["Balanced accuracy",`${(m.balanced_accuracy*100).toFixed(2)}%`],["Recall",`${(m.recall*100).toFixed(2)}%`],["Harm MAE",`$${m.harm_mae_usd.toFixed(2)}`],["Harm R²",m.harm_r2.toFixed(4)],["Promotion gates",report.promotion_passed?"Passed":"Advisory only"]]);}
  else parent.append(node("p","Real-market model not available. Mainnet assessment uses explicit V2 calculation."));
  evidence(parent,data.metrics);
}).catch(e=>q("#model-evidence").append(node("p",e.message)));
let mainnetGeneration = 0;
q("#mainnet-form").addEventListener("input", () => {
  mainnetGeneration++;
  q("#mainnet-result").replaceChildren(node("p", "Inputs changed. Read the pool again to update the comparison."));
});
q("#mainnet-form").addEventListener("submit",async e=>{
  e.preventDefault();const button=e.currentTarget.querySelector("button"),parent=q("#mainnet-result"),data=Object.fromEntries(new FormData(e.currentTarget));button.disabled=true;parent.replaceChildren(node("p","Reading Ethereum reserves…"));
  const request = ++mainnetGeneration;
  try{
    const r=await api("/api/mainnet/risk",{pool:data.pool,amount:Number(data.amount),slippage:Number(data.slippage)/100});
    if (request !== mainnetGeneration) return;
    parent.replaceChildren(node("h3",`${r.status} · ${r.source}`));
    parent.append(node("p",`Modeled sandwich harm: ${money(r.harm_usd)} at ${Number((r.inputs.slippage*100).toFixed(3))}% slippage · ${r.pool_name} · block ${r.block.toLocaleString()}.`));
    table(parent,["Slippage", "Modeled harm", "Attacker profit before gas", "Attacker gas", "Profitable after gas?"],r.comparison.map(row=>[
      `${Number((row.slippage*100).toFixed(3))}%${row.slippage === r.inputs.slippage ? " (selected)" : ""}`,
      money(row.harm_usd),money(row.economics.gross_profit_usd),money(row.economics.attacker_gas_usd),row.p_profitable ? "Yes, in model" : "No"
    ]));
    parent.append(node("p",`All rows share reserves at block ${r.block.toLocaleString()}, gas ${r.inputs.gas_price_gwei.toFixed(3)} gwei and ETH price $${r.inputs.eth_price_usd.toFixed(2)} from pool spot. Assumes $5m attacker capital, ${r.assumptions.attacker_gas_units.toLocaleString()} attacker gas units and stablecoins at $1. Private order flow, builder competition and subsequent price moves are not modeled. A new assessment refreshes the snapshot.`));
    for(const reason of r.reasons||[])parent.append(node("p",reason));
    const ml = r.model_estimate;
    if (ml?.accepted && ml.harm_usd != null) {
      parent.append(node("p",`Advisory ML harm for the selected tolerance: ${money(ml.harm_usd)}. Regression residuals can be positive even when the economic calculation finds no profitable attack.`));
      if(ml.interval_usd)parent.append(node("p",`ML-only 90% nominal interval: ${money(ml.interval_usd[0])} – ${money(ml.interval_usd[1])}. Coverage can change with market conditions.`));
    } else parent.append(node("p","ML abstained or is unavailable for this input. The economic calculation above still applies."));
    evidence(parent,r);
  }
  catch(e){if(request === mainnetGeneration)parent.replaceChildren(node("p",e.message));}finally{button.disabled=false;}
});

async function hybridEvidence(){
  const r=await api("/api/hybrid/benchmark"),parent=q("#hybrid-result");parent.replaceChildren();
  if(!r.summary){parent.append(node("p",r.message));return;}
  parent.append(node("p",`${r.status} · ${r.scope}`));
  const s=r.summary;table(parent,["Mode","Cases","Decision p50","Shadow bundles","Realized loss","Reverts"],Object.entries(s.modes).map(([mode,m])=>[mode,m.n,`${m.decision_p50_ms.toFixed(0)} ms`,m.shadow_bundles,`$${m.realized_harm_usd.toFixed(2)}`,m.reverts]));
  parent.append(node("p",`Hybrid decision time reduction: ${s.hybrid_latency_reduction_pct.toFixed(1)}%; shadow bundle reduction: ${s.hybrid_shadow_bundle_reduction_pct.toFixed(1)}%. Recommendation agreement: ${(s.recommendation_agreement*100).toFixed(1)}%.`),node("p",s.interpretation));
  evidence(parent,r);
}
q("#refresh-hybrid").addEventListener("click",()=>hybridEvidence().catch(e=>notice(e.message,true)));
hybridEvidence().catch(e=>q("#hybrid-result").append(node("p",e.message)));
