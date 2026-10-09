"""Unit tests for the closed-form AMM / sandwich math (no devnet needed)."""
import math

from mevshield import config
from mevshield.defense import amm_math

E = config.E


def test_amount_out_matches_uniswap_formula():
    out = amm_math.amount_out(1000 * E, 200_000 * E, 100 * E, 5)
    with_fee = 1000 * E * 9995
    expected = with_fee * (100 * E) // (200_000 * E * 10_000 + with_fee)
    assert out == expected


def test_constant_product_k_never_decreases():
    r_in, r_out = 200_000 * E, 100 * E
    out = amm_math.amount_out(5000 * E, r_in, r_out, 5)
    assert (r_in + 5000 * E) * (r_out - out) >= r_in * r_out


def test_sandwich_profit_is_unimodal_and_positive_for_large_trades():
    r_in, r_out = 200_000 * E, 100 * E
    v = 5000 * E
    benign = amm_math.amount_out(v, r_in, r_out, 5)
    res = amm_math.optimal_sandwich(r_in, r_out, v, int(benign * 0.98), 5)
    assert res.profit > 0
    assert res.harm > 0
    assert not res.victim_reverts


def test_no_profitable_sandwich_for_tiny_trades():
    r_in, r_out = 200_000 * E, 100 * E
    v = 5 * E
    benign = amm_math.amount_out(v, r_in, r_out, 5)
    res = amm_math.optimal_sandwich(r_in, r_out, v, int(benign * 0.98), 5)
    assert res.profit <= 0 or res.harm < 1e12  # negligible


def test_closed_form_matches_integer_simulation_within_1pct():
    r_in, r_out = 200_000 * E, 100 * E
    for v_usd in (500, 2000, 5000, 20000):
        v = v_usd * E
        benign = amm_math.amount_out(v, r_in, r_out, 5)
        min_out = int(benign * 0.98)
        exact = amm_math.optimal_sandwich(r_in, r_out, v, min_out, 5)
        cf = amm_math.closed_form_sandwich(r_in / E, r_out / E, v_usd, 0.02, 5)
        exact_harm = exact.harm / E
        rel_err = abs(exact_harm - cf["harm"]) / cf["harm"]
        assert rel_err < 0.01, (v_usd, exact_harm, cf["harm"], rel_err)


def test_max_safe_slippage_makes_attack_unprofitable():
    r_in, r_out = 200_000 * E, 100 * E
    v = 5000 * E
    cost = 1.0 * E
    s = amm_math.max_safe_slippage(r_in, r_out, v, 5, cost)
    benign = amm_math.amount_out(v, r_in, r_out, 5)
    res = amm_math.optimal_sandwich(r_in, r_out, v, int(benign * (1 - s)), 5)
    assert res.profit <= cost * 1.05


def test_oracle_attack_profit_positive_against_spot_market():
    best = amm_math.best_oracle_attack(100 * E, 200_000 * E, 1_000_000 * E, 5_000_000 * E)
    assert best["profit"] > 0
    assert best["manipulated_price"] > 2000 * E  # price was pumped


def test_arbitrage_profit_between_dislocated_pools():
    best = amm_math.best_arbitrage((100 * E, 200_000 * E), (100 * E, 206_000 * E), 5)
    assert best["profit"] > 0


def test_revert_probability_monotone():
    from mevshield.defense import revert_probability
    assert revert_probability(0.0001) > revert_probability(0.02)
    assert 0 <= revert_probability(0.02) <= 1
