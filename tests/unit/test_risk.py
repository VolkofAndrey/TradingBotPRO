"""
tests/unit/test_risk.py — тесты риск-менеджмента.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest
from config.settings import get_settings
from core.models import Direction, Signal, TradeParams
from core.risk.position_sizer import FixedFractionSizer, VolatilitySizer
from core.risk.drawdown import DrawdownGuard
from core.risk.manager import RiskManager


@pytest.fixture
def cfg():
    return get_settings("backtest")


@pytest.fixture
def long_signal():
    params = TradeParams(
        entry=95000, stop=94950, take1=95050, take2=95150,
        risk_ticks=50, direction=Direction.LONG, rr=3.0,
    )
    return Signal(action="open_long", params=params, contracts=2, reason="test")


# ─── FixedFractionSizer ───────────────────────────────────────────────────────

class TestFixedFractionSizer:
    def test_positive_result(self, cfg):
        s = FixedFractionSizer()
        n = s.calculate(500_000, 50.0, 5000.0, cfg)
        assert n >= 1

    def test_zero_balance(self, cfg):
        s = FixedFractionSizer()
        assert s.calculate(0, 50.0, 5000.0, cfg) == 0

    def test_zero_risk(self, cfg):
        s = FixedFractionSizer()
        assert s.calculate(500_000, 0, 5000.0, cfg) == 0

    def test_caps_at_max_contracts(self, cfg):
        s = FixedFractionSizer()
        n = s.calculate(500_000_000, 1.0, 100.0, cfg)
        assert n <= cfg.max_contracts

    def test_scales_with_balance(self, cfg):
        s = FixedFractionSizer()
        n1 = s.calculate(100_000, 50.0, 5000.0, cfg)
        n2 = s.calculate(500_000, 50.0, 5000.0, cfg)
        assert n2 >= n1

    def test_margin_limit_applies(self, cfg):
        """При очень маленьком ГО-лимите (max_margin_usage) — ограничение работает."""
        s  = FixedFractionSizer()
        n1 = s.calculate(100_000, 1.0, 50_000.0, cfg)   # ГО = 50к → 1 контракт
        n2 = s.calculate(100_000, 1.0, 1_000.0, cfg)    # ГО = 1к  → больше
        assert n2 >= n1


# ─── DrawdownGuard ────────────────────────────────────────────────────────────

class TestDrawdownGuard:
    def test_allows_when_no_drawdown(self, cfg):
        dg = DrawdownGuard(cfg, 500_000)
        ok, _ = dg.check(500_000)
        assert ok

    def test_allows_small_drawdown(self, cfg):
        dg = DrawdownGuard(cfg, 500_000)
        ok, _ = dg.check(491_000)   # 1.8% < 2% лимита
        assert ok

    def test_blocks_on_limit_reached(self, cfg):
        dg = DrawdownGuard(cfg, 500_000)
        ok, reason = dg.check(489_000)   # 2.2% > 2%
        assert not ok
        assert "просадка" in reason.lower()

    def test_blocks_consecutive_losses(self, cfg):
        dg = DrawdownGuard(cfg, 500_000)
        dg.record_trade(-100)
        dg.record_trade(-100)
        ok, reason = dg.check(500_000)
        assert not ok
        assert "убытк" in reason.lower()

    def test_resets_streak_on_win(self, cfg):
        dg = DrawdownGuard(cfg, 500_000)
        dg.record_trade(-100)
        dg.record_trade(+500)   # выиграли — сбрасываем серию
        dg.record_trade(-100)
        ok, _ = dg.check(500_000)
        assert ok   # только 1 убыток в серии

    def test_stays_halted_after_stop(self, cfg):
        dg = DrawdownGuard(cfg, 500_000)
        dg.check(489_000)    # триггер
        ok, _ = dg.check(500_000)  # даже если баланс восстановился — стоп
        assert not ok


# ─── RiskManager ─────────────────────────────────────────────────────────────

class TestRiskManager:
    def test_allows_valid_signal(self, cfg, long_signal):
        rm = RiskManager(cfg, 500_000)
        ok, _ = rm.check_entry(long_signal, 500_000, 5000.0)
        assert ok

    def test_blocks_when_drawdown_exceeded(self, cfg, long_signal):
        rm = RiskManager(cfg, 500_000)
        ok, reason = rm.check_entry(long_signal, 489_000, 5000.0)
        assert not ok

    def test_blocks_when_no_params(self, cfg):
        rm  = RiskManager(cfg, 500_000)
        sig = Signal(action="open_long", params=None, contracts=0, reason="test")
        ok, _ = rm.check_entry(sig, 500_000, 5000.0)
        assert not ok

    def test_blocks_when_balance_too_low(self, cfg):
        rm = RiskManager(cfg, 500_000)
        # Баланс 100 руб — невозможно купить даже 1 контракт
        params = TradeParams(
            entry=95000, stop=94000, take1=96000, take2=97000,
            risk_ticks=1000, direction=Direction.LONG, rr=2.0,
        )
        sig = Signal(action="open_long", params=params, contracts=1, reason="x")
        ok, _ = rm.check_entry(sig, 100, 5000.0)
        assert not ok
