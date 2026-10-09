"""Scripted end-to-end demo (Acts 1–4), a reliable backup for the live walkthrough."""
from __future__ import annotations

from rich.console import Console
from rich.rule import Rule

from . import config
from .chain import deploy
from .chain import Chain, fmt


def _pause(console: Console, pause: bool, msg: str = "press Enter to continue") -> None:
    if pause:
        try:
            console.input(f"[dim]({msg})[/dim] ")
        except EOFError:
            pass


def run_demo(console: Console, pause: bool = True) -> None:
    from .defense import DefenseSelector, execute
    from .guardian import Guardian
    from .harness.attacks import submit_arbitrage, submit_oracle_attack
    from .harness.sandwich_bot import SandwichBot
    from .defense import RiskEngine, SwapIntent
    from .chain import ShadowFork
    from .chain import BlockBuilder
    from .cli import _print_block

    chain = Chain()
    shadow = ShadowFork(chain)
    deploy.reset(chain)
    eng = RiskEngine(chain, shadow)
    sel = DefenseSelector(chain, eng)

    def block(searchers):
        return BlockBuilder(chain, searchers, log=console.print)

    # ---------------------------------------------------------------- Act 1
    console.print(Rule("[bold]Act 1 — flash loans are powerful (and benign)"))
    bal0 = chain.balance("USDC", chain.deployments["contracts"]["flashArb"])
    g = Guardian(chain, shadow, respond=False, log=console.print)
    submit_arbitrage(chain)
    blk = block([g]).build_block()
    _print_block(blk)
    console.print(f"arbitrageur went from {fmt(bal0)} to "
                  f"[green]{fmt(chain.balance('USDC', chain.deployments['contracts']['flashArb']))}[/green] USDC; "
                  f"Guardian verdict: BENIGN (prices converged, no protocol harmed)")
    _pause(console, pause)

    # ---------------------------------------------------------------- Act 2
    console.print(Rule("[bold]Act 2 — sandwich: predict & prevent (user side)"))
    deploy.reset(chain); shadow._forked_at = None
    bot = SandwichBot(chain, shadow, log=console.print)
    console.print("[dim]-- a $500 swap sent to the public mempool --[/dim]")
    res = execute(chain, block([bot]), SwapIntent(500), "public", {"slippage": 0.02}, log=console.print)
    for b in res.blocks:
        _print_block(b)
    console.print(f"victim realised harm: [red]${res.harm_usd:,.2f}[/red] (sandwiched: {res.sandwiched})")
    _pause(console, pause)

    console.print("[dim]-- predict BEFORE sending the same $500 swap --[/dim]")
    deploy.reset(chain); shadow._forked_at = None; bot.attacked.clear()
    selection = sel.evaluate(SwapIntent(500), mode="sim")
    console.print(f"predicted public harm ${selection.get('public').harm:,.2f}; "
                  f"recommendation: [bold green]{selection.best.name}[/bold green]")
    res = execute(chain, block([bot]), SwapIntent(500), selection.best.name, selection.best.params,
                  log=console.print)
    for b in res.blocks:
        _print_block(b)
    console.print(f"with protection the victim's harm is [green]${res.harm_usd:,.2f}[/green] "
                  f"(sandwiched: {res.sandwiched})")
    _pause(console, pause)

    console.print("[dim]-- a tiny $5 swap: the shield should NOT over-protect --[/dim]")
    deploy.reset(chain); shadow._forked_at = None; bot.attacked.clear()
    sel_small = sel.evaluate(SwapIntent(5), mode="sim")
    console.print(f"recommendation for $5: [bold green]{sel_small.best.name}[/bold green] "
                  f"(public harm ${sel_small.get('public').harm:,.2f})")
    _pause(console, pause)

    # ---------------------------------------------------------------- Act 3
    console.print(Rule("[bold]Act 3 — flash-loan oracle attack: predict & prevent (protocol side)"))
    deploy.reset(chain); shadow._forked_at = None
    console.print("[dim]-- attack vs VulnerableLending, no Guardian --[/dim]")
    liq0 = chain.c("vulnerable").functions.liquidity().call()
    submit_oracle_attack(chain, "vulnerable")
    _print_block(block([]).build_block())
    console.print(f"VulnerableLending drained: {fmt(liq0)} → "
                  f"[red]{fmt(chain.c('vulnerable').functions.liquidity().call())}[/red] USDC")
    _pause(console, pause)

    console.print("[dim]-- same attack, Guardian now responds --[/dim]")
    deploy.reset(chain); shadow._forked_at = None
    g2 = Guardian(chain, shadow, respond=True, log=console.print)
    submit_oracle_attack(chain, "vulnerable")
    _print_block(block([g2]).build_block())
    console.print(f"Guardian paused the market in the same block → attack reverted; "
                  f"liquidity safe at [green]{fmt(chain.c('vulnerable').functions.liquidity().call())}[/green]")
    _pause(console, pause)

    console.print("[dim]-- same attack vs SafeLending (TWAP + deviation guard) --[/dim]")
    deploy.reset(chain); shadow._forked_at = None
    g3 = Guardian(chain, shadow, respond=True, log=console.print)
    submit_oracle_attack(chain, "safe")
    _print_block(block([g3]).build_block())
    console.print(f"SafeLending: attack unprofitable by design; liquidity "
                  f"[green]{fmt(chain.c('safe').functions.liquidity().call())}[/green] USDC")
    _pause(console, pause)

    console.print(Rule("[bold]Demo complete"))
    console.print("Run [bold]mevshield benchmark[/bold] for the full evidence table, then "
                  "[bold]mevshield dashboard[/bold].")
    deploy.reset(chain)
