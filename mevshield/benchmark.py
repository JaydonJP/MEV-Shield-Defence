"""Mixed-workload experiments that produce the evaluation evidence in the report.

Three studies, all written to data/results.csv and the SQLite `experiments` table:
  1. defense_sweep  — user harm with no protection vs each fixed defense vs the adaptive selector, across
                      trade sizes; plus the paper's baselines (always-public, always-private, static-0.5 %,
                      best-in-hindsight) and the resulting regret.
  2. guardian_eval  — a labelled stream of benign swaps / borrows / arbitrage / sandwich / oracle attacks;
                      reports precision, recall, false-positive rate and detection latency.
  3. predictor_eval — ML fast predictor vs the simulator (MAE, R², latency).
"""
from __future__ import annotations

import random
import time
from dataclasses import asdict

import pandas as pd

from . import config, store
from .chain import deploy
from .chain import Chain, ShadowFork, BlockBuilder
from .defense import DEFENSES, DefenseSelector, execute
from .guardian import Guardian
from .harness.attacks import submit_arbitrage, submit_oracle_attack
from .harness.sandwich_bot import SandwichBot
from .harness.traffic import benign_mix, submit_user_swap
from .defense import HarmPredictor
from .defense import RiskEngine, SwapIntent

TRADE_SIZES = [5, 25, 100, 250, 500, 1000, 2500, 5000, 10000, 20000]


def _reset(chain: Chain, shadow: ShadowFork) -> None:
    deploy.reset(chain)
    shadow._forked_at = None


def defense_sweep(chain: Chain, shadow: ShadowFork, sizes=TRADE_SIZES, log=print) -> pd.DataFrame:
    """Realise each defense under the live sandwich bot at every trade size."""
    eng = RiskEngine(chain, shadow)
    sel = DefenseSelector(chain, eng)
    bot = SandwichBot(chain, shadow)
    builder = BlockBuilder(chain, [bot])
    rows = []
    strategies = ["none"] + DEFENSES + ["adaptive"]
    for size in sizes:
        intent = SwapIntent(float(size))
        selection = sel.evaluate(intent, mode="sim")
        chosen = selection.best
        for strat in strategies:
            _reset(chain, shadow)
            bot.attacked.clear()
            if strat == "none":
                res = execute(chain, builder, SwapIntent(float(size), slippage=0.5), "public",
                              {"slippage": 0.5}, log=lambda *a: None)
            elif strat == "adaptive":
                res = execute(chain, builder, intent, chosen.name, chosen.params, log=lambda *a: None)
            else:
                params = selection.get(strat).params if strat in [e.name for e in selection.evals] else {}
                res = execute(chain, builder, intent, strat, params, log=lambda *a: None)
            rows.append({"trade_usd": size, "strategy": strat,
                         "defense_used": chosen.name if strat == "adaptive" else strat,
                         "harm_usd": round(res.harm_usd, 4), "sandwiched": int(res.sandwiched),
                         "reverted": int(res.reverted), "blocks": len(res.blocks),
                         "predicted_harm_public": round(selection.get("public").harm, 4),
                         "J_best": round(chosen.J, 3)})
        log(f"  size ${size:>6}: adaptive→{chosen.name:18} harm ${rows[-1]['harm_usd']:.2f} "
            f"(public harm ${[r for r in rows if r['trade_usd']==size and r['strategy']=='public'][0]['harm_usd']:.2f})")
    df = pd.DataFrame(rows)
    _add_regret(df)
    store.save_experiment("defense_sweep", df.to_dict("records"))
    return df


def _add_regret(df: pd.DataFrame) -> None:
    """Regret of each fixed baseline vs the best-in-hindsight defense, per trade size (cost = harm + fee+gas proxy)."""
    df["cost"] = df["harm_usd"]
    best = df.groupby("trade_usd")["harm_usd"].min().to_dict()
    df["regret"] = df.apply(lambda r: r["harm_usd"] - best[r["trade_usd"]], axis=1)


def guardian_eval(chain: Chain, shadow: ShadowFork, n_benign: int = 40, seed: int = 1, log=print) -> dict:
    """Labelled stream → confusion matrix, precision/recall/FPR, detection latency."""
    rng = random.Random(seed)
    _reset(chain, shadow)
    store.clear_runtime()
    guardian = Guardian(chain, shadow, respond=False)
    results = []  # (truth_is_attack, predicted_label, latency_ms)

    def classify_last() -> tuple[str, float]:
        v = guardian.verdicts[-1]
        return v.label, v.latency_ms

    # benign swaps & borrows & arbitrage (truth = not attack)
    events = []
    for _ in range(n_benign):
        events.append(("benign", benign_mix(rng)))
    for _ in range(max(3, n_benign // 10)):
        events.append(("arb", None))
    for _ in range(max(5, n_benign // 6)):
        events.append(("oracle", None))
    rng.shuffle(events)

    for kind, spec in events:
        _reset(chain, shadow)
        if kind == "benign" and spec["kind"] == "swap":
            submit_user_swap(chain, spec["role"], spec["amount"], spec["token_in"], spec["amm"], spec["slippage"])
            truth = 0
        elif kind == "benign":
            from .harness.traffic import submit_borrow
            submit_borrow(chain, spec["role"], spec["weth"])
            truth = 0
        elif kind == "arb":
            h, _ = submit_arbitrage(chain)
            if h is None:
                continue
            truth = 0
        else:
            submit_oracle_attack(chain, "vulnerable")
            truth = 1
        pending = chain.txpool()
        guardian.seen.clear()
        guardian.on_pending(chain, pending)
        if not guardian.verdicts:
            continue
        label, lat = classify_last()
        results.append((truth, 1 if label == "CRITICAL" else 0, lat, kind, label))

    tp = sum(1 for t, p, *_ in results if t == 1 and p == 1)
    fn = sum(1 for t, p, *_ in results if t == 1 and p == 0)
    fp = sum(1 for t, p, *_ in results if t == 0 and p == 1)
    tn = sum(1 for t, p, *_ in results if t == 0 and p == 0)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    fpr = fp / (fp + tn) if fp + tn else 0.0
    arb_flagged = sum(1 for t, p, _, k, _ in results if k == "arb" and p == 1)
    metrics = {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "precision": round(precision, 3),
               "recall": round(recall, 3), "false_positive_rate": round(fpr, 3),
               "f1": round(2 * precision * recall / (precision + recall), 3) if precision + recall else 0.0,
               "arbitrage_false_positives": arb_flagged,
               "mean_detection_latency_ms": round(sum(r[2] for r in results) / len(results), 1) if results else 0,
               "n": len(results)}
    store.save_experiment("guardian_eval", {"metrics": metrics,
                                            "samples": [{"truth": t, "pred": p, "latency_ms": round(l, 1),
                                                         "kind": k, "label": lab} for t, p, l, k, lab in results]})
    log(f"  precision={metrics['precision']} recall={metrics['recall']} FPR={metrics['false_positive_rate']} "
        f"arb false-positives={arb_flagged} latency={metrics['mean_detection_latency_ms']}ms")
    return metrics


def predictor_eval(chain: Chain, shadow: ShadowFork, n: int = 40, seed: int = 2, log=print) -> dict:
    """ML predictor vs the shadow-fork simulator on live swaps."""
    if HarmPredictor.available():
        model = HarmPredictor.load()
    else:
        model = HarmPredictor()
        model.train()
        model.save()
    eng = RiskEngine(chain, shadow)
    rng = random.Random(seed)
    rows = []
    _reset(chain, shadow)
    r0, r1 = chain.reserves("ammA")
    liq_usd = r1 / config.E
    for _ in range(n):
        size = round(10 ** rng.uniform(0.7, 4.3), 2)
        intent = SwapIntent(float(size))
        rep = eng.assess(intent, mode="sim", leakage_levels=False)
        t0 = time.perf_counter()
        pred = model.predict(size, liq_usd, intent.slippage, config.POOL_FEE_BPS, 1.0)
        pred_us = (time.perf_counter() - t0) * 1e6
        rows.append({"trade_usd": size, "sim_harm": rep.harm_public_usd, "ml_harm": pred["harm_usd"],
                     "sim_ms": rep.sim_ms, "ml_us": pred_us, "p_attack": pred["p_attack"]})
    df = pd.DataFrame(rows)
    mae = (df["sim_harm"] - df["ml_harm"]).abs().mean()
    ss_res = ((df["sim_harm"] - df["ml_harm"]) ** 2).sum()
    ss_tot = ((df["sim_harm"] - df["sim_harm"].mean()) ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0
    metrics = {"mae": round(float(mae), 3), "r2": round(float(r2), 4),
               "mean_sim_ms": round(float(df["sim_ms"].mean()), 1), "mean_ml_us": round(float(df["ml_us"].mean()), 1),
               "speedup": round(float(df["sim_ms"].mean() * 1000 / df["ml_us"].mean()), 0), "n": n}
    store.save_experiment("predictor_eval", {"metrics": metrics, "model_metrics": model.metrics_dict(),
                                             "samples": df.to_dict("records")})
    log(f"  MAE=${metrics['mae']} R2={metrics['r2']} sim={metrics['mean_sim_ms']}ms ml={metrics['mean_ml_us']}us "
        f"(~{metrics['speedup']:.0f}× faster)")
    return metrics


def run_all(chain: Chain, shadow: ShadowFork, quick: bool = False, log=print) -> dict:
    sizes = [5, 100, 500, 2500, 10000] if quick else TRADE_SIZES
    log("[1/3] defense sweep…")
    df = defense_sweep(chain, shadow, sizes, log=log)
    log("[2/3] guardian evaluation…")
    gm = guardian_eval(chain, shadow, n_benign=25 if quick else 50, log=log)
    log("[3/3] predictor evaluation…")
    pm = predictor_eval(chain, shadow, n=25 if quick else 50, log=log)
    df.to_csv(config.RESULTS_CSV, index=False)
    summary = summarize(df, gm, pm)
    store.save_experiment("summary", summary)
    return summary


def summarize(df: pd.DataFrame, guardian_metrics: dict, predictor_metrics: dict) -> dict:
    pub = df[df.strategy == "public"].set_index("trade_usd")["harm_usd"]
    ada = df[df.strategy == "adaptive"].set_index("trade_usd")["harm_usd"]
    baselines = {}
    for strat in ["public", "private-relay", "adaptive"]:
        sub = df[df.strategy == strat]
        baselines[strat] = {"total_harm": round(sub["harm_usd"].sum(), 2),
                            "total_regret": round(sub["regret"].sum(), 2)}
    return {"total_harm_no_protection": round(df[df.strategy == 'none']["harm_usd"].sum(), 2),
            "total_harm_public": round(pub.sum(), 2), "total_harm_adaptive": round(ada.sum(), 2),
            "harm_reduction_pct": round(100 * (1 - ada.sum() / max(pub.sum(), 1e-9)), 1),
            "baselines": baselines, "guardian": guardian_metrics, "predictor": predictor_metrics}
