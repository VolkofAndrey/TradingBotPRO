"""
di.py — сборка графа зависимостей.

Не «магический контейнер», а явная фабрика.
Три публичные функции:

  build_backtest(data_path, initial_balance) → BacktestContainer
  build_paper(initial_balance)               → LiveContainer
  build_live()                               → LiveContainer

Ядро (стратегия, portfolio, risk, engine) создаётся одинаково для всех режимов.
Меняется только DataProvider и ExecutionProvider.

Принцип: ядро должно запускаться без DI (для тестов — все классы
инстанцируются напрямую). DI нужен только для сборки prod-пайплайна.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("bot.di")


# ─────────────────────────────────────────────────────────────────────────────
#  Контейнеры (именованные группы зависимостей)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestContainer:
    """Все объекты для бэктеста."""
    cfg:       object
    bus:       object
    data:      object
    execution: object
    portfolio: object
    strategy:  object
    risk:      object
    engine:    object


@dataclass
class LiveContainer:
    """Все объекты для paper/live торговли."""
    cfg:       object
    bus:       object
    data:      object
    execution: object   # async context manager
    portfolio: object
    strategy:  object
    risk:      object
    engine:    object


# ─────────────────────────────────────────────────────────────────────────────
#  Общая сборка ядра
# ─────────────────────────────────────────────────────────────────────────────

def _build_core(mode: str, initial_balance: float):
    """
    Создаёт общие компоненты: cfg, bus, portfolio, risk.
    Возвращает кортеж для дальнейшей сборки.
    """
    from config.settings import get_settings
    from core.event_bus import EventBus
    from core.portfolio import Portfolio
    from core.risk.manager import RiskManager
    from utils.logger import setup_logging

    cfg = get_settings(mode)
    setup_logging(cfg.log_level, cfg.log_file)

    # В backtest нет ограничения (0), в live/paper 200мс на обработчик
    max_ms    = 0 if mode == "backtest" else 200
    bus       = EventBus(max_handler_ms=max_ms)
    portfolio = Portfolio(bus, cfg, initial_balance)
    risk      = RiskManager(cfg, initial_balance)

    return cfg, bus, portfolio, risk


# ─────────────────────────────────────────────────────────────────────────────
#  BACKTEST
# ─────────────────────────────────────────────────────────────────────────────

def build_backtest(
    data_path: str | Path,
    initial_balance: float = 500_000.0,
    warmup_bars: int = 450,
    slippage_ticks: int = 1,
) -> BacktestContainer:
    """
    Собрать полный граф зависимостей для бэктеста.

    data_path      — путь к CSV или Parquet файлу с M1-свечами.
    initial_balance — стартовый виртуальный баланс в рублях.
    warmup_bars    — сколько баров накопить перед первым сигналом.
    slippage_ticks — симулируемый слипидж в тиках.
    """
    from core.engine import BacktestEngine
    from core.strategy import LiquiditySweepStrategy
    from data.historical import HistoricalDataProvider
    from execution.backtest import BacktestExecutionProvider

    cfg, bus, portfolio, risk = _build_core("backtest", initial_balance)

    data      = HistoricalDataProvider(cfg, data_path, warmup_bars)
    execution = BacktestExecutionProvider(cfg, initial_balance, slippage_ticks)

    strategy  = LiquiditySweepStrategy(
        bus                  = bus,
        cfg                  = cfg,
        available_balance_fn = execution.get_balance_sync,
    )

    engine = BacktestEngine(
        cfg       = cfg,
        bus       = bus,
        data      = data,
        execution = execution,
        portfolio = portfolio,
        strategy  = strategy,
        risk      = risk,
    )

    logger.info(
        "BacktestContainer собран | figi=%s | баланс=%.0f | данные=%s",
        cfg.figi, initial_balance, data_path,
    )
    return BacktestContainer(
        cfg=cfg, bus=bus, data=data, execution=execution,
        portfolio=portfolio, strategy=strategy, risk=risk, engine=engine,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  PAPER (sandbox)
# ─────────────────────────────────────────────────────────────────────────────

def build_paper(initial_balance: float = 1_000_000.0) -> LiveContainer:
    """
    Собрать граф зависимостей для paper trading (sandbox T-Инвестиций).

    execution — это async context manager, его нужно использовать через:
      async with container.execution:
          await container.engine.run()
    """
    from core.engine import LiveEngine
    from core.strategy import LiquiditySweepStrategy
    from data.live import LiveDataProvider
    from execution.paper import PaperExecutionProvider

    cfg, bus, portfolio, risk = _build_core("paper", initial_balance)

    data      = LiveDataProvider(cfg)
    execution = PaperExecutionProvider(cfg, initial_balance)

    strategy  = LiquiditySweepStrategy(
        bus                  = bus,
        cfg                  = cfg,
        available_balance_fn = portfolio.get_available_balance,
    )

    engine = LiveEngine(
        cfg       = cfg,
        bus       = bus,
        data      = data,
        execution = execution,
        portfolio = portfolio,
        strategy  = strategy,
        risk      = risk,
    )

    logger.info("PaperContainer собран | figi=%s", cfg.figi)
    return LiveContainer(
        cfg=cfg, bus=bus, data=data, execution=execution,
        portfolio=portfolio, strategy=strategy, risk=risk, engine=engine,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  LIVE (реальный брокер)
# ─────────────────────────────────────────────────────────────────────────────

def build_live(initial_balance_hint: float = 0.0) -> LiveContainer:
    """
    Собрать граф зависимостей для реальной торговли.

    initial_balance_hint — используется только для инициализации RiskManager
    (дневной лимит просадки). Реальный баланс запрашивается из API при старте.
    """
    from core.engine import LiveEngine
    from core.strategy import LiquiditySweepStrategy
    from data.live import LiveDataProvider
    from execution.tinkoff import TinkoffExecutionProvider

    if not initial_balance_hint:
        # Дефолт — будет перезаписан при первом вызове get_balance()
        initial_balance_hint = 100_000.0

    cfg, bus, portfolio, risk = _build_core("live", initial_balance_hint)

    data      = LiveDataProvider(cfg)
    execution = TinkoffExecutionProvider(cfg)

    strategy  = LiquiditySweepStrategy(
        bus                  = bus,
        cfg                  = cfg,
        available_balance_fn = portfolio.get_available_balance,
    )

    engine = LiveEngine(
        cfg       = cfg,
        bus       = bus,
        data      = data,
        execution = execution,
        portfolio = portfolio,
        strategy  = strategy,
        risk      = risk,
    )

    logger.info("LiveContainer собран | figi=%s", cfg.figi)
    return LiveContainer(
        cfg=cfg, bus=bus, data=data, execution=execution,
        portfolio=portfolio, strategy=strategy, risk=risk, engine=engine,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  MULTI-INSTRUMENT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def build_live_for(figi: str, initial_balance_hint: float = 0.0) -> LiveContainer:
    """
    Собрать граф для реальной торговли по конкретному FIGI.
    Конфиг берётся из live.yaml, но FIGI переопределяется.
    """
    import os
    os.environ["BOT_FIGI"] = figi   # pydantic-settings читает из env
    from functools import lru_cache
    # Сбрасываем кэш настроек чтобы подхватить новый FIGI
    from config.settings import get_settings
    get_settings.cache_clear()
    return build_live(initial_balance_hint)


def build_paper_for(figi: str, initial_balance: float = 1_000_000.0) -> LiveContainer:
    """Собрать граф для paper trading по конкретному FIGI."""
    import os
    os.environ["BOT_FIGI"] = figi
    from config.settings import get_settings
    get_settings.cache_clear()
    return build_paper(initial_balance)


def build_backtest_for(
    figi: str,
    data_path,
    initial_balance: float = 500_000.0,
    **kwargs,
) -> "BacktestContainer":
    """Собрать граф для бэктеста по конкретному FIGI."""
    import os
    os.environ["BOT_FIGI"] = figi
    from config.settings import get_settings
    get_settings.cache_clear()
    return build_backtest(data_path, initial_balance, **kwargs)
