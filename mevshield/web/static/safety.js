"use strict";
const q = s => document.querySelector(s);
const node = (tag, text, cls) => { const el = document.createElement(tag); if (text != null) el.textContent = text; if (cls) el.className = cls; return el; };
let current = null, busy = false, generation = 0;
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Check the inputs and try again.");
  return data;
}
function notice(text, error = false) { q("#notice").textContent = text; q("#notice").classList.toggle("error", error); }
function evidence(parent, value) { const d = node("details"); d.append(node("summary", "Inspect or copy the full evidence"), node("pre", JSON.stringify(value, null, 2))); parent.append(d); }
function table(parent, headings, rows) { const t = node("table"), head = node("tr"); headings.forEach(h => head.append(node("th", h))); t.append(head); for (const row of rows) { const tr = node("tr"); row.forEach(v => tr.append(node("td", v))); t.append(tr); } parent.append(t); }
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
    button.addEventListener("click", async () => { if (busy || current?.id !== r.id) return; busy = true; button.disabled = true; notice("Submitting the reviewed local transaction…"); try { const result = await api("/api/safety/execute", {report_id:r.id,binding:r.binding}); current = null; report(result); notice("Transaction included. Review its actual changes below."); await history(); } catch(e) { current = null; notice(e.message, true); } finally { busy = false; } });
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
q("#risk-form").addEventListener("submit",async e=>{
  e.preventDefault(); const button=e.currentTarget.querySelector("button"); button.disabled=true; const data=Object.fromEntries(new FormData(e.currentTarget));
  try { const result=await api("/api/risk",{amount:Number(data.amount),slippage:Number(data.slippage)/100}); const parent=q("#risk-result"); parent.replaceChildren(node("h3",`${result.status} · ${result.source}`)); parent.append(node("p", result.harm_usd == null ? "Risk is unknown. No safe verdict is inferred." : `Estimated sandwich harm: $${result.harm_usd.toFixed(2)}.`)); if(result.interval_usd) parent.append(node("p",`90% nominal synthetic calibration interval: $${result.interval_usd[0].toFixed(2)} – $${result.interval_usd[1].toFixed(2)}. Coverage can fail under drift.`)); for(const reason of result.reasons || []) parent.append(node("p",reason)); evidence(parent,result); } catch(e){notice(e.message,true);} finally{button.disabled=false;}
});
async function history(){const rows=await api("/api/safety/history"),parent=q("#history");parent.replaceChildren();if(!rows.length)parent.append(node("p","No previews recorded yet."));else table(parent,["Action","User","State","Receipt"],rows.map(r=>[r.action,r.role,r.status,r.receipt?.hash || "—"]));}
async function verification(){const result=await api("/api/verification"),parent=q("#verification-result");parent.replaceChildren(node("p",result.status === "passed" ? "Verification passed. Inspect the recorded commands, seed and mutation results." : result.message || `Recorded verification status: ${result.status}`));evidence(parent,result);}
q("#refresh-history").addEventListener("click",()=>history().catch(e=>notice(e.message,true)));
q("#refresh-verification").addEventListener("click",()=>verification().catch(e=>notice(e.message,true)));
api("/api/status").then(s=>notice(s.ready ? "Local chain ready. Choose an action to preview." : "Local lab is offline. Start it with mevshield start --build to preview transactions.")).catch(e=>notice(e.message,true));
history().catch(e=>notice(e.message,true));verification().catch(e=>notice(e.message,true));
