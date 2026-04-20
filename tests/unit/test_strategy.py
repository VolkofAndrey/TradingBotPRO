"""
tests/unit/test_strategy.py

Unit-тесты чистой логики стратегии.
Никаких моков брокера, никакого async, никакого I/O.
Работает напрямую с функциями из strategy_logic.py.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest
from datetime import datetime, timedelta

import strategy_logic as sl
from config.settings import get_settings
from tests.fixtures.bar_factory import make_bar, make_bars


@pytest.fixture(autouse=True)
def cfg():
    return get_settings("backtest")


# ─── Технические индикаторы ──────────────────────────────────────────────────

class TestATR:
    def test_returns_zero_when_insufficient_bars(self):
        bars = make_bars([100.0] * 5)
        assert sl.compute_atr(bars, 20) == 0.0

    def test_positive_on_enough_bars(self):
        bars = make_bars([100.0] * 25, high_delta=10, low_delta=10)
        atr = sl.compute_atr(bars, 20)
        assert atr > 0

    def test_higher_volatility_higher_atr(self):
        calm   = make_bars([100.0] * 25, high_delta=2, low_delta=2)
        choppy = make_bars([100.0] * 25, high_delta=20, low_delta=20)
        assert sl.compute_atr(choppy, 20) > sl.compute_atr(calm, 20)


class TestVolumeMA:
    def test_returns_zero_insufficient(self):
        bars = make_bars([100.0] * 5, volume=1000)
        assert sl.compute_volume_ma(bars, 20) == 0.0

    def test_correct_average(self):
        bars = make_bars([100.0] * 20, volume=2000)
        assert sl.compute_volume_ma(bars, 20) == pytest.approx(2000.0)


class TestMA:
    def test_returns_zero_insufficient(self):
        bars = make_bars([100.0] * 5)
        assert sl.compute_ma(bars, 10) == 0.0

    def test_correct_mean(self):
        closes = [100.0, 102.0, 98.0, 101.0, 99.0,
                  100.0, 102.0, 98.0, 101.0, 99.0]
        bars = make_bars(closes)
        ma = sl.compute_ma(bars, 10)
        assert ma == pytest.approx(sum(closes) / 10)


# ─── H1 агрегация ────────────────────────────────────────────────────────────

class TestAggregateH1:
    def test_groups_by_hour(self):
        start = datetime(2024, 6, 3, 10, 0)
        bars  = make_bars([100.0] * 60, start=start)
        h1    = sl.aggregate_h1(bars)
        assert len(h1) == 1
        assert h1[0]["datetime"].hour == 10

    def test_two_hours(self):
        start = datetime(2024, 6, 3, 9, 0)
        bars  = make_bars([100.0] * 120, start=start)
        h1    = sl.aggregate_h1(bars)
        assert len(h1) == 2

    def test_high_is_max(self):
        start = datetime(2024, 6, 3, 10, 0)
        closes = [100.0] * 60
        bars   = make_bars(closes, start=start, high_delta=10)
        # Руками делаем один бар с высоким high
        bars[30]["high"] = 200.0
        h1 = sl.aggregate_h1(bars)
        assert h1[0]["high"] == pytest.approx(200.0)


# ─── Уровни ликвидности ──────────────────────────────────────────────────────

class TestLiquidityLevels:
    def test_returns_empty_on_few_bars(self):
        bars = make_bars([100.0] * 5)
        cfg = get_settings("backtest")
        assert sl.find_liquidity_levels(bars, cfg) == []

    def test_finds_prev_high_low(self):
        """Вчерашние High/Low должны определяться с приоритетом 1."""
        from datetime import date, timedelta
        import config as cfg_mod

        yesterday = datetime(2024, 6, 2, 10, 0)
        today     = datetime(2024, 6, 3, 10, 0)

        # 30 вчерашних + 30 сегодняшних
        bars = []
        for i in range(30):
            dt = yesterday + timedelta(minutes=i)
            bars.append(make_bar(close=100.0 + i * 0.1,
                                 high=102.0, low=98.0, dt=dt))
        for i in range(30):
            dt = today + timedelta(minutes=i)
            bars.append(make_bar(close=100.0, high=101.0, low=99.0, dt=dt))

        cfg = get_settings("backtest")
        levels = sl.find_liquidity_levels(bars, cfg)
        priorities = {lv["priority"] for lv in levels}
        # Должны быть уровни с приоритетом 1
        assert 1 in priorities


# ─── Расчёт параметров сделки ─────────────────────────────────────────────────

class TestCalculateTradeParams:
    def make_short_sweep(self, level=100.0, extreme=105.0):
        return {
            "direction":      "short",
            "sweep_extreme":  extreme,
            "level_price":    level,
            "level_type":     "prev_high",
            "level_priority": 1,
            "bar_time":       datetime(2024, 6, 3, 10, 30),
        }

    def make_long_sweep(self, level=100.0, extreme=95.0):
        return {
            "direction":      "long",
            "sweep_extreme":  extreme,
            "level_price":    level,
            "level_type":     "prev_low",
            "level_priority": 1,
            "bar_time":       datetime(2024, 6, 3, 10, 30),
        }

    def test_short_stop_above_entry(self):
        cfg = get_settings("backtest")
        params = sl.calculate_trade_params(self.make_short_sweep(), atr=20.0, cfg=cfg)
        assert params is not None
        assert params["stop"] > params["entry"], "SHORT: stop должен быть выше entry"

    def test_long_stop_below_entry(self):
        cfg = get_settings("backtest")
        params = sl.calculate_trade_params(self.make_long_sweep(), atr=20.0, cfg=cfg)
        assert params is not None
        assert params["stop"] < params["entry"], "LONG: stop должен быть ниже entry"

    def test_short_take1_below_entry(self):
        cfg = get_settings("backtest")
        params = sl.calculate_trade_params(self.make_short_sweep(), atr=20.0, cfg=cfg)
        assert params is not None
        assert params["take1"] < params["entry"], "SHORT: take1 должен быть ниже entry"

    def test_long_take1_above_entry(self):
        cfg = get_settings("backtest")
        params = sl.calculate_trade_params(self.make_long_sweep(), atr=20.0, cfg=cfg)
        assert params is not None
        assert params["take1"] > params["entry"], "LONG: take1 должен быть выше entry"

    def test_returns_none_when_stop_too_wide(self):
        """Стоп больше STOP_MAX_ATR_MULT × ATR → None."""
        sweep = self.make_short_sweep(level=100.0, extreme=200.0)  # огромный свип
        cfg = get_settings("backtest")
        params = sl.calculate_trade_params(sweep, atr=1.0, cfg=cfg)
        assert params is None


# ─── Расчёт размера позиции ───────────────────────────────────────────────────

class TestCalculatePositionSize:
    def test_positive_result(self):
        n = sl.calculate_position_size(500_000, 50.0, 5000.0)
        assert n >= 1

    def test_zero_on_zero_balance(self):
        assert sl.calculate_position_size(0, 50.0) == 0

    def test_zero_on_zero_risk(self):
        assert sl.calculate_position_size(500_000, 0) == 0

    def test_respects_max_contracts(self):
        cfg = get_settings("backtest")
        n = sl.calculate_position_size(500_000_000, 1.0, 100.0)
        assert n <= cfg.max_contracts

    def test_larger_balance_more_contracts(self):
        n1 = sl.calculate_position_size(100_000, 50.0, 5000.0)
        n2 = sl.calculate_position_size(500_000, 50.0, 5000.0)
        assert n2 >= n1


# ─── Изоляция состояния ──────────────────────────────────────────────────────

class TestStateIsolation:
    def test_generate_signal_is_pure(self):
        """Два вызова с одинаковыми данными дают одинаковый результат."""
        bars = make_bars([100.0] * 50)
        r1 = sl.generate_signal(bars, 500_000)
        r2 = sl.generate_signal(bars, 500_000)
        assert r1["action"] == r2["action"]

    def test_generate_signal_no_side_effects_on_input(self):
        """generate_signal не изменяет переданный список свечей."""
        bars = make_bars([100.0] * 50)
        original_len = len(bars)
        sl.generate_signal(bars, 500_000)
        assert len(bars) == original_len
