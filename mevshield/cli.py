"""`mevshield` — the command chain for the demo.

    mevshield up | down | status | reset
    mevshield arb
    mevshield swap <amount> [--public | --protect] [--slippage]
    mevshield predict <amount> [--slippage]
    mevshield attack oracle --target vulnerable|safe
    mevshield attack governance --target vulnerable|safe
    mevshield guardian start
    mevshield train
    mevshield benchmark [--quick]
    mevshield dashboard
    mevshield demo
"""
from __future__ import annotations

import sys
import time

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import config, store
from .chain import deploy, devnet
from .chain import Chain, fmt

app = typer.Typer(add_completion=False, help="MEV Shield — predict & prevent flash-loan and MEV attacks.",
                  no_args_is_help=True)
attack_app = typer.Typer(help="Launch a test-harness attack.")
app.add_typer(attack_app, name="attack")
guardian_app = typer.Typer(help="Protocol-side mempool guardian.")
app.add_typer(guardian_app, name="guardian")
extensions_app = typer.Typer(help="Routing, cashback, batching, firewall, coordinated detection and replay.")
app.add_typer(extensions_app, name="extensions")

console = Console()
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _chain() -> Chain:
    s = devnet.status()
    if not s["main"]["alive"]:
        console.print("[red]devnet is not running — run `mevshield up` first[/red]")
        raise typer.Exit(1)
    return Chain()


def _shadow():
    from .chain import ShadowFork, shadow_available
    if not shadow_available():
        console.print("[red]shadow fork is not running — run `mevshield up`[/red]")
        raise typer.Exit(1)
    return ShadowFork(_chain())


def _builder(chain, searchers, log=None):
    from .chain import BlockBuilder
    return BlockBuilder(chain, searchers, log=log or console.print)


def _default_searchers(chain, shadow):
    """The sandwich bot always lurks (unless an external guardian is running, which also lurks)."""
    from .harness.sandwich_bot import SandwichBot
    return [SandwichBot(chain, shadow, log=console.print)]


# --------------------------------------------------------------------- lifecycle
@app.command()
def up(build: bool = typer.Option(True, help="run `forge build` first")):
    """Start the devnet + shadow fork and deploy & seed everything."""
    if build:
        console.print("Building contracts…")
        deploy.forge_build()
    console.print("Starting main devnet (no-mining, fee-ordered)…")
    devnet.start_main()
    chain = Chain()
    console.print("Deploying & seeding the world…")
    deploy.deploy_all(chain, log=console.print)
    console.print("Starting shadow fork…")
    devnet.start_shadow()
    store.clear_runtime()
    console.print(Panel.fit(
        f"[green]MEV Shield is up.[/green]\n"
        f"main   {config.MAIN_RPC}\nshadow {config.SHADOW_RPC}\n"
        f"Pool A: {fmt(config.POOL_A[0])} WETH / {fmt(config.POOL_A[1])} USDC  (spot 2000)\n"
        f"Try:  [bold]mevshield predict 500[/bold]   then   [bold]mevshield swap 500 --protect[/bold]",
        title="ready"))


@app.command()
def down():
    """Stop the devnet and shadow fork (only the processes we started)."""
    stopped = devnet.stop()
    console.print(f"Stopped {len(stopped)} process(es)." if stopped else "Nothing was running.")


@app.command()
def status():
    """Show devnet status and recent activity."""
    s = devnet.status()
    t = Table(title="devnet")
    t.add_column("node"); t.add_column("rpc"); t.add_column("pid"); t.add_column("alive")
    for name in ("main", "shadow"):
        n = s[name]
        t.add_row(name, n["rpc"], str(n["pid"]), "[green]yes[/green]" if n["alive"] else "[red]no[/red]")
    console.print(t)
    if s["main"]["alive"]:
        c = _chain()
        console.print(f"block {c.block_number}  spot A {fmt(c.c('ammA').functions.spotPrice0().call())} USDC/WETH"
                      f"  twap {fmt(c.c('oracle').functions.twap().call())}")
        alerts = store.query("SELECT label, COUNT(*) n FROM alerts GROUP BY label")
        if alerts:
            console.print("alerts: " + ", ".join(f"{a['label']}={a['n']}" for a in alerts))


@app.command()
def reset():
    """Return the devnet to the freshly-deployed state."""
    chain = _chain()
    deploy.reset(chain)
    store.clear_runtime()
    console.print("[green]reset to the clean deployed state[/green]")


# ------------------------------------------------------------------------- arb
@app.command()
def arb():
    """Act 1 — benign flash-loan arbitrage: attacker starts with nothing, profits, Guardian says BENIGN."""
    from .guardian import Guardian
    from .harness.attacks import plan_arbitrage, submit_arbitrage
    chain = _chain()
    shadow = _shadow()
    plan = plan_arbitrage(chain)
    if not plan:
        console.print("[yellow]no arbitrage opportunity right now[/yellow]")
        return
    bal0 = chain.balance("USDC", chain.deployments["contracts"]["flashArb"])
    console.print(Panel.fit(
        f"Pool A spot {chain.c('ammA').functions.spotPrice0().call() / config.E:,.0f}  "
        f"Pool B spot {chain.c('ammB').functions.spotPrice0().call() / config.E:,.0f}  "
        f"(gap {plan['gap'] * 100:.1f} %)\n"
        f"arbitrageur contract USDC balance before: {fmt(bal0)}\n"
        f"flash-borrow {fmt(plan['amount'])} USDC → buy on {plan['buy']} → sell on {plan['sell']} → repay",
        title="Act 1 — flash-loan arbitrage (benign)"))
    guardian = Guardian(chain, shadow, respond=False, log=console.print)
    h, _ = submit_arbitrage(chain)
    blk = _builder(chain, [guardian]).build_block()
    acct = chain.deployments["contracts"]["flashArb"]
    console.print(f"profit to arbitrageur: [green]{fmt(chain.balance('USDC', acct))} USDC[/green] "
                  f"(started with {fmt(bal0)})")
    _print_block(blk)
    pa, pb = chain.c("ammA").functions.spotPrice0().call(), chain.c("ammB").functions.spotPrice0().call()
    console.print(f"pools after: A {pa / config.E:,.1f}  B {pb / config.E:,.1f}  → prices converged; "
                  f"[green]no protocol was harmed[/green]")


# ------------------------------------------------------------------------ swap
@app.command()
def swap(amount: float, public: bool = typer.Option(False, "--public"),
         protect: bool = typer.Option(False, "--protect"),
         slippage: float = typer.Option(config.DEFAULT_SLIPPAGE), token_in: str = "USDC"):
    """Act 2 — send a swap. --public (exposed), --protect (selector picks a defense), or just quote."""
    from .defense import DefenseSelector, execute
    from .defense import RiskEngine, SwapIntent
    chain = _chain()
    shadow = _shadow()
    intent = SwapIntent(amount, token_in=token_in, slippage=slippage)
    searchers = _default_searchers(chain, shadow)
    builder = _builder(chain, searchers)

    if protect:
        sel = DefenseSelector(chain, RiskEngine(chain, shadow))
        selection = sel.evaluate(intent, mode="sim")
        best = selection.best
        console.print(Panel.fit(
            f"predicted public harm [red]${selection.get('public').harm:,.2f}[/red] → "
            f"selector picks [bold green]{best.name}[/bold green] (J=${best.J:,.2f})",
            title=f"Act 2 — swap {amount:,} {token_in} (protected)"))
        res = execute(chain, builder, intent, best.name, best.params, log=console.print)
        defense = best.name
    else:
        console.print(Panel.fit(f"swap {amount:,} {token_in} straight to the public mempool",
                                title=f"Act 2 — swap {amount:,} {token_in} (--public)"))
        res = execute(chain, builder, intent, "public", {"slippage": slippage}, log=console.print)
        defense = "public"

    for blk in res.blocks:
        _print_block(blk)
    verb = "reverted" if res.reverted else f"received {fmt(res.actual_out)} {intent.token_out}"
    color = "green" if res.harm_usd < 1 else "red"
    console.print(f"you {verb}; expected {fmt(res.expected_out)}; "
                  f"realised harm [{color}]${res.harm_usd:,.2f}[/{color}]  (sandwiched: {res.sandwiched})")
    store.add_swap({"tx_hash": res.tx_hashes[0], "user": intent.role, "amount": amount, "defense": defense,
                    "expected_out": res.expected_out / config.E, "actual_out": res.actual_out / config.E,
                    "harm_usd": res.harm_usd, "sandwiched": int(res.sandwiched), "block": chain.block_number})


@app.command()
def predict(amount: float, slippage: float = typer.Option(config.DEFAULT_SLIPPAGE), token_in: str = "USDC"):
    """Act 2 — BEFORE sending: predicted loss, attacker profit, harm per defense, recommendation."""
    from .defense import DefenseSelector
    from .defense import RiskEngine, SwapIntent
    chain = _chain()
    shadow = _shadow()
    intent = SwapIntent(amount, token_in=token_in, slippage=slippage)
    sel = DefenseSelector(chain, RiskEngine(chain, shadow))
    selection = sel.evaluate(intent, mode="sim")
    r = selection.report
    head = Table.grid(padding=(0, 2))
    head.add_row("trade", f"{amount:,} {token_in}  (~${r.trade_usd:,.0f})")
    head.add_row("if sent public", f"[red]harm ${r.harm_public_usd:,.2f}[/red], "
                 f"attacker profit ${r.attacker_profit_usd:,.2f}, P(attack)={r.p_attack:.0%}")
    head.add_row("hint leakage", f"harm ${r.harm_hint_usd:,.2f}")
    head.add_row("closed-form check", f"${r.closed_form_harm_usd:,.2f}  (error {r.closed_form_error_pct:.2f} %)")
    head.add_row("simulation", f"{r.sim_ms:.0f} ms, {r.evals} counterfactual evals")
    console.print(Panel(head, title=f"prediction for {amount:,} {token_in}"))

    t = Table(title="defense options  (J = harm + gas + latency + revert-risk, all in USD)")
    for col in ("defense", "harm $", "gas $", "fee $", "latency", "revert", "J $"):
        t.add_column(col, justify="right" if col != "defense" else "left")
    best = selection.best
    for e in sorted(selection.evals, key=lambda e: e.J):
        style = "bold green" if e.name == best.name else ""
        extra = f"  s*={e.params.get('slippage', slippage) * 100:.3f}%" if e.name == "adaptive-slippage" else ""
        t.add_row(e.name + extra, f"{e.harm:,.2f}", f"{e.gas:,.2f}", f"{e.fee:,.2f}",
                  f"{e.latency_blocks:g} blk", f"{e.revert_risk:.1%}", f"{e.J:,.2f}", style=style)
    console.print(t)
    console.print(f"recommendation: [bold green]{best.name}[/bold green] — "
                  + ("public is fine, no defense is worth paying for" if best.name == "public"
                     else f"cuts harm from ${selection.get('public').harm:,.2f} to ${best.harm:,.2f}"))
    store.add_prediction({"amount": amount, "token_in": token_in, "slippage": slippage,
                          "harm_public": r.harm_public_usd, "attacker_profit": r.attacker_profit_usd,
                          "p_attack": r.p_attack, "harm_hint": r.harm_hint_usd, "harm_private": r.harm_private_usd,
                          "closed_form_harm": r.closed_form_harm_usd, "recommendation": best.name,
                          "costs": {e.name: round(e.J, 2) for e in selection.evals}, "sim_ms": r.sim_ms})


# ---------------------------------------------------------------------- attack
@attack_app.command("oracle")
def attack_oracle(target: str = typer.Option("vulnerable", help="vulnerable|safe")):
    """Act 3 — flash-loan oracle manipulation against a lending market."""
    from .guardian import Guardian
    from .harness.attacks import submit_oracle_attack
    chain = _chain()
    shadow = _shadow()
    if target not in ("vulnerable", "safe"):
        console.print("[red]--target must be 'vulnerable' or 'safe'[/red]"); raise typer.Exit(1)
    guardian_running = store.is_alive("guardian")
    liq0 = chain.c(target).functions.liquidity().call()
    console.print(Panel.fit(
        f"target: [bold]{target}Lending[/bold]  (liquidity {fmt(liq0)} USDC)\n"
        f"guardian live: {'[green]yes[/green]' if guardian_running else 'no'}",
        title="Act 3 — flash-loan oracle attack"))
    searchers = []
    if not guardian_running:
        searchers = [Guardian(chain, shadow, respond=False, log=console.print)]  # detect only, no in-proc response
    h, plan = submit_oracle_attack(chain, target)
    blk = _builder(chain, searchers).build_block()
    _print_block(blk)
    liq1 = chain.c(target).functions.liquidity().call()
    rcpt = chain.receipt(h)
    if rcpt["status"] == 1:
        from .chain import account
        profit = chain.balance("USDC", account("oracle_attacker").address)
        console.print(f"[red]attack SUCCEEDED[/red]: {target} liquidity {fmt(liq0)} → {fmt(liq1)} "
                      f"(drained ${(liq0 - liq1) / config.E:,.0f}; attacker USDC now {fmt(profit)})")
    else:
        console.print(f"[green]attack REVERTED[/green]: {target} liquidity unchanged at {fmt(liq1)} — funds safe")


@attack_app.command("governance")
def attack_governance(target: str = typer.Option("vulnerable", help="vulnerable|safe (→ DAO)")):
    """Act 3b (stretch) — flash-loan governance takeover vs snapshot voting."""
    from .harness.attacks import submit_governance_attack
    chain = _chain()
    dao = "vulnerableDAO" if target == "vulnerable" else "snapshotDAO"
    t0 = chain.balance("USDC", chain.deployments["contracts"][dao])
    console.print(Panel.fit(f"target: [bold]{dao}[/bold]  (treasury {fmt(t0)} USDC)",
                            title="Act 3b — flash-loan governance attack"))
    h, plan = submit_governance_attack(chain, dao)
    blk = _builder(chain, []).build_block()
    _print_block(blk)
    t1 = chain.balance("USDC", chain.deployments["contracts"][dao])
    if chain.receipt(h)["status"] == 1:
        console.print(f"[red]DAO treasury drained[/red]: {fmt(t0)} → {fmt(t1)}")
    else:
        console.print(f"[green]attack REVERTED[/green] (snapshot voting: flash-borrowed tokens have no past "
                      f"balance) — treasury safe at {fmt(t1)}")


# -------------------------------------------------------------------- guardian
@guardian_app.command("start")
def guardian_start(respond: bool = typer.Option(True, help="auto-pause on CRITICAL"),
                   interval: float = typer.Option(0.2)):
    """Run the live mempool guardian (2nd terminal). Watches, scores and (optionally) pauses protocols."""
    from .guardian import Guardian
    chain = _chain()
    shadow = _shadow()
    guardian = Guardian(chain, shadow, respond=respond, log=console.print)
    console.print(Panel.fit(f"Guardian watching {config.MAIN_RPC}; auto-response: "
                            f"{'[green]on[/green]' if respond else 'off'}\nCtrl-C to stop", title="guardian"))
    try:
        while True:
            store.heartbeat("guardian", {"respond": respond})
            pending = chain.txpool()
            if pending:
                guardian.on_pending(chain, pending)
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("guardian stopped.")


# ------------------------------------------------------------- train/benchmark
@app.command()
def train(samples: int = 20000):
    """Train the ML risk predictor on synthesised attack scenarios and report its accuracy."""
    from .defense import HarmPredictor
    console.print(f"synthesising {samples:,} attack scenarios and training gradient-boosted models…")
    m = HarmPredictor()
    metrics = m.train(samples, log=console.print)
    m.save()
    t = Table(title="ML risk predictor")
    t.add_column("metric"); t.add_column("value", justify="right")
    t.add_row("harm R²", f"{metrics.r2:.4f}")
    t.add_row("harm R² (5-fold CV)", f"{metrics.cv_r2_mean:.4f} ± {metrics.cv_r2_std:.4f}")
    t.add_row("harm MAE / RMSE", f"${metrics.mae:.2f} / ${metrics.rmse:.2f}")
    t.add_row("attack classifier F1", f"{metrics.classifier_f1:.3f}")
    t.add_row("attack ROC-AUC", f"{metrics.roc_auc:.4f}")
    t.add_row("predict latency", f"{metrics.predict_us:.0f} µs")
    console.print(t)
    top = list(metrics.feature_importances.items())[:4]
    console.print("top features: " + ", ".join(f"{k} ({v:.2f})" for k, v in top))
    console.print(f"[green]saved to {config.MODEL_FILE}[/green]")


@app.command()
def benchmark(quick: bool = typer.Option(False, help="fewer trade sizes / samples")):
    """Act 4 — run the mixed-workload experiments and write data/results.csv."""
    from .benchmark import run_all
    chain = _chain()
    shadow = _shadow()
    console.print(Panel.fit("running defense sweep, guardian evaluation and predictor evaluation…",
                            title="Act 4 — benchmark"))
    summary = run_all(chain, shadow, quick=quick, log=console.print)
    deploy.reset(chain)
    t = Table(title="summary")
    t.add_column("metric"); t.add_column("value", justify="right")
    t.add_row("harm, no protection", f"${summary['total_harm_no_protection']:,.2f}")
    t.add_row("harm, always-public", f"${summary['total_harm_public']:,.2f}")
    t.add_row("harm, adaptive selector", f"${summary['total_harm_adaptive']:,.2f}")
    t.add_row("harm reduction", f"{summary['harm_reduction_pct']:.1f} %")
    g = summary["guardian"]
    t.add_row("guardian precision / recall", f"{g['precision']} / {g['recall']}")
    t.add_row("arbitrage false positives", str(g["arbitrage_false_positives"]))
    p = summary["predictor"]
    t.add_row("predictor MAE / R²", f"${p['mae']} / {p['r2']}")
    console.print(t)
    console.print(f"[green]wrote {config.RESULTS_CSV}[/green]  →  view with [bold]mevshield serve[/bold]")


def _ensure_up(build: bool = False) -> None:
    """Start the devnet + shadow fork and deploy if they are not already running."""
    if build:
        console.print("Building contracts…")
        deploy.forge_build()
    s = devnet.status()
    if not s["main"]["alive"]:
        console.print("Starting main devnet…")
        devnet.start_main()
        deploy.deploy_all(Chain(), log=console.print)
    if not devnet.status()["shadow"]["alive"]:
        console.print("Starting shadow fork…")
        devnet.start_shadow()
    store.clear_runtime()


def _ensure_model() -> None:
    from .defense import HarmPredictor
    if not HarmPredictor.available():
        console.print("No trained model found — training one now (this runs once)…")
        m = HarmPredictor(); m.train(log=console.print); m.save()
        console.print("[green]model trained[/green]")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True):
    """Launch the website. Sepolia works independently; `mevshield start` also starts the local lab."""
    import uvicorn
    if not devnet.status()["main"]["alive"]:
        console.print("[yellow]Local lab offline; the Sepolia view is available. Use `mevshield start --build` for both.[/yellow]")
    url = f"http://{host}:{port}"
    console.print(Panel.fit(f"MEV Shield dashboard → [bold]{url}[/bold]\nCtrl-C to stop", title="serve"))
    if open_browser:
        import threading, webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("mevshield.web.server:app", host=host, port=port, log_level="warning")


@app.command()
def start(port: int = 8000, build: bool = typer.Option(False, help="run `forge build` first")):
    """One command: boot the chain + shadow fork, deploy, train the model if needed, and open the dashboard."""
    _ensure_up(build=build)
    _ensure_model()
    console.print(Panel.fit("Everything is up. Opening the live dashboard…", title="MEV Shield"))
    serve(port=port, open_browser=True)


@app.command()
def demo(pause: bool = typer.Option(True, help="press Enter between acts")):
    """Run the scripted demo in the terminal (backup for the live web demo)."""
    from .demo import run_demo
    run_demo(console, pause=pause)


# ------------------------------------------------------------------- helpers
def _print_block(blk):
    t = Table(title=f"block {blk.number}  (ordered by priority fee)")
    t.add_column("#"); t.add_column("tx"); t.add_column("from"); t.add_column("call")
    t.add_column("tip", justify="right"); t.add_column("ok"); t.add_column("flags")
    for i, tx in enumerate(blk.txs):
        label = ",".join(tx.labels)
        style = "red" if any(x in ("CRITICAL",) for x in tx.labels) else (
            "yellow" if tx.private or "SUSPICIOUS" in tx.labels else "")
        ok = "[green]✓[/green]" if tx.status == 1 else "[red]✗[/red]"
        flags = ("private " if tx.private else "") + label
        t.add_row(str(i), tx.hash[:10], tx.sender_role or (tx.sender[:8]), tx.fn, f"{tx.tip_gwei:.1f}", ok,
                  flags, style=style)
    console.print(t)


@extensions_app.command("run")
def extensions_run(feature: str, amount: float | None = typer.Option(None)):
    """Run routing|cashback|batch|firewall|coordinated, or all (isolated comparative suite)."""
    import json
    from .extensions.service import run_feature, run_suite
    chain = _chain()
    shadow = _shadow()
    if feature == "all" and amount is not None:
        raise typer.BadParameter("all uses scenario-specific default amounts")
    try:
        result = run_suite(chain, shadow) if feature == "all" else run_feature(chain, shadow, feature, amount)
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print_json(json.dumps(result))


@extensions_app.command("scan")
def extensions_scan():
    """Analyze the current pending set as related sequences without mining it."""
    import json
    from .extensions.coordinated import analyze_sequences
    chain = _chain()
    console.print_json(json.dumps(analyze_sequences(chain, _shadow(), chain.txpool())))


@extensions_app.command("replay")
def extensions_replay(amount: float = 10000, liquidity: float = 1, slippage: float = .02,
                      capital: float = 50000, gas: float = 2, delay: int = 0, seed: int = 42,
                      save: bool = False, replay_id: str | None = None, guardian_tip: float = 2):
    """Compare deterministic AMM-model scenarios offline; optionally save/load an immutable scenario."""
    import json
    from .extensions.replay import Scenario, load_replay, run_replay, save_replay
    try:
        scenario = load_replay(replay_id) if replay_id else Scenario(
            amount_usdc=amount, liquidity_scale=liquidity, slippage=slippage,
            attacker_capital_usdc=capital, gas_gwei=gas, guardian_delay_blocks=delay, seed=seed,
            guardian_tip_gwei=guardian_tip)
        result = run_replay(scenario)
        if save:
            result["saved"] = save_replay(scenario)
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print_json(json.dumps(result))


if __name__ == "__main__":
    app()
