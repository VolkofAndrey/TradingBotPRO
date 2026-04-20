"""
tests/integration/test_execution_backtest.py

Интеграционный тест: BacktestEngine с реальной стратегией и HistoricalDataProvider.
Не моки — настоящие объекты. Данные генерируются в памяти.

Цель: убедиться что все слои работают вместе без паники.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest
from datetime import datetime, timedelta, date, time

from config.settings import get_settings
from core.event_bus import EventBus
from core.events import BarEvent, PositionOpened, PositionClosed, SignalEvent
from core.portfolio import Portfolio
from core.risk.manager import RiskManager
from core.strategy import LiquiditySweepStrategy
from core.engine import BacktestEngine
from data.base import DataProvider
from execution.backtest import BacktestExecutionProvider
from tests.fixtures.bar_factory import make_bars


# ─── Фейковый DataProvider на основе списка баров ────────────────────────────

class InMemoryDataProvider(DataProvider):
    """Провайдер данных прямо из памяти — для тестов."""

    def __init__(self, bars: list[dict], figi: str, warmup: int = 50):
        self._bars   = bars
        self._figi   = figi
        self._warmup = warmup

    def get_figi(self) -> str:
        return self._figi

    def iter_bars(self):
        window = []
        for bar in self._bars:
            window.append(bar)
            if len(window) >= self._warmup:
                yield window[:]


# ─── Генерация реалистичных данных ───────────────────────────────────────────

def make_realistic_bars(n: int = 600) -> list[dict]:
    """
    Генерирует n минутных баров с умеренной волатильностью.
    Цены колеблются около 95000 (фьючерс Si).
    """
    import math, random
    random.seed(42)
    bars   = []
    price  = 95000.0
    start  = datetime(2024, 6, 3, 9, 0, 0)   # начало торговой сессии

    for i in range(n):
        dt     = start + timedelta(minutes=i)
        change = random.gauss(0, 15)           # ~15 пунктов std
        price  = max(90000, min(100000, price + change))

        high   = price + abs(random.gauss(0, 8))
        low    = price - abs(random.gauss(0, 8))
        volume = int(random.gauss(15000, 3000))

        bars.append({
            "datetime": dt,
            "date":     dt.date(),
            "time":     dt.time(),
            "open":     price - change / 2,
            "high":     high,
            "low":      low,
            "close":    price,
            "volume":   max(1000, volume),
            "complete": True,
        })
    return bars


# ─── Тесты ───────────────────────────────────────────────────────────────────

@pytest.fixture
def cfg():
    return get_settings("backtest")


@pytest.fixture
def backtest_setup(cfg):
    """Собирает полный граф зависимостей для теста."""
    bars     = make_realistic_bars(600)
    figi     = cfg.figi
    balance  = 500_000.0

    bus       = EventBus()
    bus.enable_history()
    portfolio = Portfolio(bus, cfg, balance)
    risk      = RiskManager(cfg, balance)
    execution = BacktestExecutionProvider(cfg, balance)
    data      = InMemoryDataProvider(bars, figi, warmup=50)

    strategy = LiquiditySweepStrategy(
        bus                  = bus,
        cfg                  = cfg,
        available_balance_fn = execution.get_balance_sync,
    )

    engine = BacktestEngine(
        cfg=cfg, bus=bus, data=data, execution=execution,
        portfolio=portfolio, strategy=strategy, risk=risk,
    )

    return dict(
        engine=engine, bus=bus, portfolio=portfolio,
        execution=execution, strategy=strategy, cfg=cfg,
    )


class TestBacktestIntegration:
    def test_engine_runs_without_exception(self, backtest_setup):
        """Главный тест: engine не падает на 600 барах."""
        stats = backtest_setup["engine"].run()
        assert isinstance(stats, dict)
        assert "total_trades" in stats
        assert "net_pnl" in stats

    def test_bar_events_published(self, backtest_setup):
        """Каждый бар публикует BarEvent."""
        s = backtest_setup
        s["engine"].run()
        bar_events = s["bus"].get_history(BarEvent)
        assert len(bar_events) > 0

    def test_balance_never_negative(self, backtest_setup):
        """Баланс не уходит в минус."""
        s = backtest_setup
        s["engine"].run()
        assert s["execution"].get_balance_sync() >= 0

    def test_stats_structure(self, backtest_setup):
        """Статистика содержит все ожидаемые поля."""
        stats = backtest_setup["engine"].run()
        required = {"total_trades", "wins", "losses", "winrate", "net_pnl", "balance"}
        assert required.issubset(stats.keys())

    def test_positions_properly_closed(self, backtest_setup):
        """После завершения бэктеста открытых позиций нет."""
        s = backtest_setup
        s["engine"].run()
        assert not s["portfolio"].has_position

    def test_winrate_in_valid_range(self, backtest_setup):
        """Win rate от 0 до 1."""
        stats = backtest_setup["engine"].run()
        assert 0.0 <= stats["winrate"] <= 1.0

    def test_event_bus_strategy_isolation(self, cfg):
        """
        Стратегия не знает о балансе напрямую:
        available_balance_fn — это замыкание, не ссылка на Portfolio.
        """
        bus         = EventBus()
        balance_box = [100_000.0]   # изменяемый контейнер

        strategy = LiquiditySweepStrategy(
            bus                  = bus,
            cfg                  = cfg,
            available_balance_fn = lambda: balance_box[0],
        )
        # Стратегия не хранит ссылку на portfolio — только callable
        assert not hasattr(strategy, '_portfolio')
