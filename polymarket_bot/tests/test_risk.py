"""
test_risk.py — tests for bot/risk.py

KEY FACTS (checked 2026-04-06):
- Kelly is DISABLED — calculate_stake() always returns STAKE_USD (fixed)
- In test mode: returns STAKE_USD with reason='test_fixed'
- In non-test mode: returns STAKE_USD with reason='fixed'
- estimate_win_probability():
  - confluence 3 → 0.60 base
  - confluence 4 → 0.70 base
  - confluence 5 → 0.80 base
  - atr_zone golden → +0.05
  - atr_zone dead → -0.05
  - cp 0.50–0.55 → +0.02
  - cp > 0.68 → -0.02
  - time_left 5–9 → +0.02
  - time_left < 3 → -0.03
  - clamped to [0.50, 0.90]
"""

import pytest

from bot.risk import (
    estimate_win_probability,
    calculate_edge,
    calculate_stake,
    format_risk_line,
)
from bot.config import STAKE_USD


def _signal(
    confluence: int = 3,
    atr_zone: str = "quiet",
    contract_price: float = 0.60,
    clob_ask: float | None = None,
    time_left: float = 7.0,
) -> dict:
    s = {
        "confluence": confluence,
        "atr_zone": atr_zone,
        "contract_price": contract_price,
        "time_left": time_left,
        "direction": "DOWN",
    }
    if clob_ask is not None:
        s["clob_ask"] = clob_ask
    return s


# ---------------------------------------------------------------------------
# estimate_win_probability tests
# ---------------------------------------------------------------------------

class TestWinProbability:
    def test_win_prob_conf3(self):
        """confluence=3, quiet zone, neutral modifiers → base 0.60."""
        sig = _signal(confluence=3, atr_zone="quiet", contract_price=0.60, time_left=7.0)
        prob = estimate_win_probability(sig)
        # base=0.60, time_left=7 → +0.02, cp=0.60 → no modifier
        # Expected: 0.62
        assert abs(prob - 0.62) < 0.01, f"Expected ~0.62 got {prob}"

    def test_win_prob_conf4(self):
        """confluence=4, quiet zone → base 0.70."""
        sig = _signal(confluence=4, atr_zone="quiet", contract_price=0.60, time_left=7.0)
        prob = estimate_win_probability(sig)
        # base=0.70, time=7 → +0.02 → 0.72
        assert abs(prob - 0.72) < 0.01, f"Expected ~0.72 got {prob}"

    def test_win_prob_conf5(self):
        """confluence=5, quiet zone → base 0.80."""
        sig = _signal(confluence=5, atr_zone="quiet", contract_price=0.60, time_left=7.0)
        prob = estimate_win_probability(sig)
        # base=0.80, time=7 → +0.02 → 0.82
        assert abs(prob - 0.82) < 0.01, f"Expected ~0.82 got {prob}"

    def test_win_prob_golden_atr(self):
        """atr_zone=golden → +0.05 modifier applied."""
        quiet_prob = estimate_win_probability(_signal(confluence=3, atr_zone="quiet", time_left=7.0))
        golden_prob = estimate_win_probability(_signal(confluence=3, atr_zone="golden", time_left=7.0))
        assert abs(golden_prob - quiet_prob - 0.05) < 0.001

    def test_win_prob_dead_atr(self):
        """atr_zone=dead → -0.05 modifier applied."""
        quiet_prob = estimate_win_probability(_signal(confluence=3, atr_zone="quiet", time_left=7.0))
        dead_prob = estimate_win_probability(_signal(confluence=3, atr_zone="dead", time_left=7.0))
        assert abs(quiet_prob - dead_prob - 0.05) < 0.001

    def test_win_prob_cheap_price(self):
        """cp in 0.50–0.55 → +0.02."""
        base = estimate_win_probability(_signal(confluence=3, contract_price=0.60, time_left=7.0))
        cheap = estimate_win_probability(_signal(confluence=3, contract_price=0.52, time_left=7.0))
        assert abs(cheap - base - 0.02) < 0.001

    def test_win_prob_expensive_price(self):
        """cp > 0.68 → -0.02."""
        base = estimate_win_probability(_signal(confluence=3, contract_price=0.60, time_left=7.0))
        exp = estimate_win_probability(_signal(confluence=3, contract_price=0.72, time_left=7.0))
        assert abs(base - exp - 0.02) < 0.001

    def test_win_prob_good_time(self):
        """time_left in 5–9 → +0.02."""
        neutral_time = estimate_win_probability(_signal(confluence=3, time_left=10.0))
        good_time = estimate_win_probability(_signal(confluence=3, time_left=7.0))
        assert abs(good_time - neutral_time - 0.02) < 0.001

    def test_win_prob_late_entry(self):
        """time_left < 3 → -0.03."""
        neutral_time = estimate_win_probability(_signal(confluence=3, time_left=10.0))
        late = estimate_win_probability(_signal(confluence=3, time_left=2.0))
        assert abs(neutral_time - late - 0.03) < 0.001

    def test_win_prob_clamp_min(self):
        """All negative modifiers → still >= 0.50."""
        sig = _signal(confluence=1, atr_zone="dead", contract_price=0.72, time_left=1.0)
        prob = estimate_win_probability(sig)
        assert prob >= 0.50

    def test_win_prob_clamp_max(self):
        """All positive modifiers → still <= 0.90."""
        sig = _signal(confluence=5, atr_zone="golden", contract_price=0.52, time_left=7.0)
        prob = estimate_win_probability(sig)
        assert prob <= 0.90

    def test_win_prob_uses_clob_ask_when_present(self):
        """clob_ask overrides contract_price for cp modifier calculation."""
        # With cp=0.80 (expensive, -0.02 modifier)
        sig_cp = _signal(confluence=3, contract_price=0.80, time_left=7.0)
        # With clob_ask=0.52 (cheap, +0.02 modifier)
        sig_clob = _signal(confluence=3, contract_price=0.80, clob_ask=0.52, time_left=7.0)
        prob_cp = estimate_win_probability(sig_cp)
        prob_clob = estimate_win_probability(sig_clob)
        assert prob_clob > prob_cp, "clob_ask=0.52 should give higher prob than cp=0.80"


# ---------------------------------------------------------------------------
# calculate_edge tests
# ---------------------------------------------------------------------------

class TestCalculateEdge:
    def test_edge_formula(self):
        """edge = win_prob - contract_price."""
        sig = _signal(confluence=3, contract_price=0.60, time_left=7.0)
        edge = calculate_edge(sig)
        win_prob = estimate_win_probability(sig)
        expected = win_prob - 0.60
        assert abs(edge - expected) < 0.001

    def test_edge_can_be_negative(self):
        """Edge can be negative if contract price > win probability."""
        sig = _signal(confluence=3, contract_price=0.90, time_left=7.0)
        edge = calculate_edge(sig)
        assert edge < 0

    def test_edge_positive_for_good_signal(self):
        """High confluence + low contract price → positive edge."""
        sig = _signal(confluence=5, contract_price=0.52, time_left=7.0)
        edge = calculate_edge(sig)
        assert edge > 0


# ---------------------------------------------------------------------------
# calculate_stake tests — Kelly DISABLED
# ---------------------------------------------------------------------------

class TestCalculateStake:
    def test_kelly_disabled_returns_fixed(self, mock_state_light):
        """Kelly is disabled — always returns STAKE_USD."""
        sig = _signal(confluence=4, contract_price=0.65)
        result = calculate_stake(sig)
        assert result["stake_usd"] == STAKE_USD
        assert result["reason"] == "fixed"

    def test_fixed_stake_zero_edge(self, mock_state_light):
        """Even with zero/negative edge: STAKE_USD is returned."""
        sig = _signal(confluence=2, contract_price=0.90)
        result = calculate_stake(sig)
        assert result["stake_usd"] == STAKE_USD

    def test_fixed_stake_positive_edge(self, mock_state_light):
        """Even with positive edge: still returns fixed STAKE_USD (Kelly disabled)."""
        sig = _signal(confluence=5, contract_price=0.52)
        result = calculate_stake(sig)
        assert result["stake_usd"] == STAKE_USD

    def test_stake_test_mode(self, mock_state_test):
        """In test mode: stake_usd=STAKE_USD, reason='test_fixed'."""
        sig = _signal(confluence=4, contract_price=0.65)
        result = calculate_stake(sig)
        assert result["stake_usd"] == STAKE_USD
        assert result["reason"] == "test_fixed"

    def test_stake_has_win_prob(self, mock_state_light):
        """Result dict contains win_prob field."""
        sig = _signal(confluence=3, contract_price=0.60)
        result = calculate_stake(sig)
        assert "win_prob" in result
        assert 0 <= result["win_prob"] <= 1

    def test_stake_has_edge(self, mock_state_light):
        """Result dict contains edge field."""
        sig = _signal(confluence=3, contract_price=0.60)
        result = calculate_stake(sig)
        assert "edge" in result

    def test_stake_shares_calculated(self, mock_state_light):
        """shares = stake / contract_price."""
        sig = _signal(confluence=3, contract_price=0.50)
        result = calculate_stake(sig)
        expected_shares = round(STAKE_USD / 0.50, 2)
        assert abs(result["shares"] - expected_shares) < 0.01

    def test_stake_uses_clob_ask_for_shares(self, mock_state_light):
        """When clob_ask provided, shares = stake / clob_ask (not contract_price)."""
        sig = _signal(confluence=3, contract_price=0.60, clob_ask=0.65)
        result = calculate_stake(sig)
        expected_shares = round(STAKE_USD / 0.65, 2)
        assert abs(result["shares"] - expected_shares) < 0.01

    def test_stake_always_fixed_regardless_of_bankroll(self, mock_state_light):
        """Bankroll parameter ignored when Kelly is disabled."""
        sig = _signal(confluence=4, contract_price=0.65)
        result1 = calculate_stake(sig, bankroll=10)
        result2 = calculate_stake(sig, bankroll=10_000)
        assert result1["stake_usd"] == result2["stake_usd"] == STAKE_USD


# ---------------------------------------------------------------------------
# format_risk_line tests
# ---------------------------------------------------------------------------

class TestFormatRiskLine:
    def test_format_contains_edge(self):
        """Output string contains Edge field."""
        risk = {"edge": 0.05, "win_prob": 0.65, "stake_usd": 10.0, "reason": "fixed"}
        line = format_risk_line(risk)
        assert "Edge" in line
        assert "+5.0%" in line

    def test_format_contains_win_prob(self):
        """Output string contains WinProb field."""
        risk = {"edge": 0.05, "win_prob": 0.65, "stake_usd": 10.0, "reason": "fixed"}
        line = format_risk_line(risk)
        assert "WinProb" in line
        assert "65%" in line

    def test_format_contains_stake(self):
        """Output string contains Stake amount."""
        risk = {"edge": 0.05, "win_prob": 0.65, "stake_usd": 10.0, "reason": "fixed"}
        line = format_risk_line(risk)
        assert "$10.00" in line

    def test_format_contains_reason(self):
        """Output string contains reason."""
        risk = {"edge": -0.02, "win_prob": 0.58, "stake_usd": 10.0, "reason": "fixed"}
        line = format_risk_line(risk)
        assert "fixed" in line

    def test_format_negative_edge(self):
        """Negative edge displayed with minus sign."""
        risk = {"edge": -0.05, "win_prob": 0.55, "stake_usd": 10.0, "reason": "fixed"}
        line = format_risk_line(risk)
        assert "-5.0%" in line
