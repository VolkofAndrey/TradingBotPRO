"""
core/strategy.py — адаптер стратегии в архитектуру event-bus.

Принцип:
  - Вся торговая логика живёт в strategy_logic.py (бывший strategy.py —
    переименован, чтобы не было конфликтов импортов).
  - Этот файл — тонкий адаптер: слушает BarEvent, вызывает чистые функции,
    публикует SignalEvent.
  - Стратегия НИЧЕГО не знает о брокере, режиме работы, async.

Состояние стратегии:
  - Окно свечей (candles deque, макс. CANDLE_WINDOW_SIZE).
  - active_sweep — текущий активный sweep (ожидаем возврата).
  Всё остальное пересчитывается на каждом баре.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional

from config.settings import Settings
from core.event_bus import EventBus
from core.events import BarEvent, RiskEvent, SignalEvent
from core.models import Signal

# Чистая логика — переименованный оригинальный strategy.py
import strategy_logic as sl

logger = logging.getLogger("bot.strategy")

# Сколько свечей держим в памяти (≈ 2 торговых дня M1)
CANDLE_WINDOW_SIZE = 800


class LiquiditySweepStrategy:
    """
    Event-driven адаптер стратегии Liquidity Sweep.

    Подписывается на BarEvent → вызывает strategy_logic → публикует SignalEvent.
    Управляет окном свечей и состоянием active_sweep.
    """

    def __init__(self, bus: EventBus, cfg: Settings, available_balance_fn) -> None:
        """
        bus                  — шина событий
        cfg                  — конфиг текущего режима
        available_balance_fn — callable() → float, возвращает актуальный баланс.
                               Передаётся как зависимость, чтобы стратегия не знала
                               о портфеле напрямую.
        """
        self._bus  = bus
        self._cfg  = cfg
        self._balance_fn = available_balance_fn

        self._candles: deque[dict] = deque(maxlen=CANDLE_WINDOW_SIZE)
        self._active_sweep: Optional[dict] = None

        # Подписываемся на бары
        bus.subscribe(BarEvent, self.on_bar)
        logger.info("LiquiditySweepStrategy инициализирована (figi=%s)", cfg.figi)

    # ─── Обработчик события ──────────────────────────────────────────────────

    def on_bar(self, event: BarEvent) -> None:
        """Вызывается шиной при каждой новой закрытой свече."""
        if event.figi != self._cfg.figi:
            return

        # Обновляем окно свечей
        for bar_dict in event.candles_snapshot:
            if not self._candles or bar_dict["datetime"] > self._candles[-1]["datetime"]:
                self._candles.append(bar_dict)

        candles = list(self._candles)
        if len(candles) < self._cfg.atr_period + 5:
            return

        balance = self._balance_fn()
        if balance <= 0:
            logger.warning("on_bar: баланс = %.0f, пропускаем", balance)
            return

        # Получаем GO из конфига (в live это будет актуальное значение из API)
        go = self._cfg.go_per_contract

        try:
            result = sl.generate_signal(
                candles=candles,
                available_balance=balance,
                active_sweep=self._active_sweep,
                go_per_contract=go,
                cfg=self._cfg,
            )
        except Exception:
            logger.exception("generate_signal упал — пропускаем бар")
            return

        self._handle_result(result)

    # ─── Внутренняя логика ───────────────────────────────────────────────────

    def _handle_result(self, result: dict) -> None:
        action = result.get("action", "no_signal")

        if action == "pending_sweep":
            # Нашли sweep, ждём возврата
            self._active_sweep = result["sweep"]
            logger.info(
                "Sweep обнаружен: %s на уровне %.2f",
                result["sweep"]["direction"],
                result["sweep"]["level_price"],
            )
            # Публикуем для трейсинга (engine / portfolio проигнорируют)
            self._bus.publish(SignalEvent(
                figi=self._cfg.figi,
                signal=Signal(
                    action="pending_sweep",
                    sweep=result.get("sweep"),
                    reason=result.get("reason", ""),
                ),
            ))

        elif action in ("open_long", "open_short"):
            # Сигнал на вход — сбрасываем sweep
            self._active_sweep = None
            params = result["params"]
            from core.models import Direction, TradeParams
            trade_params = TradeParams(
                entry=params["entry"],
                stop=params["stop"],
                take1=params["take1"],
                take2=params["take2"],
                risk_ticks=params["risk_ticks"],
                direction=Direction(params["direction"]),
                rr=params["rr"],
            )
            signal = Signal(
                action=action,
                params=trade_params,
                contracts=result["contracts"],
                sweep=result.get("sweep"),
                is_strong_trade=result.get("is_strong_trade", False),
                timeout_time=result.get("timeout_time"),
                atr=result.get("atr", 0.0),
                atr_fast=result.get("atr_fast", 0.0),
                reason=result.get("reason", ""),
            )
            logger.info("Сигнал: %s | %s", action, signal.reason)
            self._bus.publish(SignalEvent(figi=self._cfg.figi, signal=signal))

        elif action == "wait":
            # Ожидаем — sweep ещё может сработать, не сбрасываем
            logger.debug("wait: %s", result.get("reason"))

        elif action == "no_signal":
            # Нет условий — сбрасываем устаревший sweep
            if self._active_sweep is not None:
                logger.debug(
                    "Sweep сброшен (нет условий): %s",
                    result.get("reason"),
                )
                self._active_sweep = None

    # ─── Управление открытой позицией ────────────────────────────────────────

    def manage(self, candles: list[dict], position) -> dict:
        """
        Вызывается engine'ом каждую минуту пока есть открытая позиция.
        Возвращает dict из strategy_logic.manage_position().

        Намеренно НЕ публикует события сам — это делает engine,
        чтобы manage() оставался чистой функцией.
        """
        return sl.manage_position(candles=candles, position=position, cfg=self._cfg)

    # ─── Состояние (для тестов и DI) ─────────────────────────────────────────

    def reset(self) -> None:
        """Полный сброс состояния (используется в тестах)."""
        self._candles.clear()
        self._active_sweep = None

    @property
    def candles(self) -> list[dict]:
        return list(self._candles)

    @property
    def active_sweep(self) -> Optional[dict]:
        return self._active_sweep
