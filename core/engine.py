"""
core/engine.py — дирижёр: run-loop и только он.

Два класса:
  BacktestEngine  — синхронный, итерирует бары из HistoricalDataProvider.
  LiveEngine      — асинхронный, слушает aiter_bars() из LiveDataProvider.

Оба делают одно и то же:
  1. Получить снапшот свечей.
  2. Если нет позиции → опубликовать BarEvent (стратегия сгенерирует сигнал).
  3. Если есть позиция → вызвать strategy.manage() → применить действие.
  4. При сигнале на вход → RiskManager.check_entry() → place_order → OrderFilled.

Engine НЕ знает о логике стратегии. Он только маршрутизирует события.
"""

from __future__ import annotations

import logging
from datetime import datetime

from config.settings import Settings
from core.event_bus import EventBus
from core.events import (
    BarEvent, OrderEvent, OrderFilled,
    PositionClosed, RiskEvent, SessionEvent, SignalEvent,
)
from core.models import Direction, ManageAction
from core.portfolio import Portfolio
from core.risk.manager import RiskManager
from core.strategy import LiquiditySweepStrategy
from data.base import DataProvider
from execution.base import ExecutionProvider
from execution.order_poller import FillResult, OrderNotFilledError, wait_for_fill

logger = logging.getLogger("bot.engine")


# ─────────────────────────────────────────────────────────────────────────────
#  BACKTEST ENGINE (синхронный)
# ─────────────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Синхронный движок бэктеста.

    run() итерирует все бары из DataProvider, на каждом баре:
      - публикует BarEvent (стратегия подписана на него через bus)
      - обрабатывает SignalEvent (если стратегия что-то сгенерировала)
      - управляет открытой позицией
    """

    def __init__(
        self,
        cfg: Settings,
        bus: EventBus,
        data: DataProvider,
        execution: ExecutionProvider,
        portfolio: Portfolio,
        strategy: LiquiditySweepStrategy,
        risk: RiskManager,
    ) -> None:
        self._cfg       = cfg
        self._bus       = bus
        self._data      = data
        self._exec      = execution
        self._portfolio = portfolio
        self._strategy  = strategy
        self._risk      = risk

        # Накапливаем SignalEvent синхронно (bus вызывает обработчики сразу)
        self._pending_signal: SignalEvent | None = None
        bus.subscribe(SignalEvent, self._on_signal)

    def run(self) -> dict:
        """
        Запустить бэктест. Возвращает статистику сессии.
        """
        logger.info("Бэктест запущен. figi=%s", self._cfg.figi)
        self._bus.publish(SessionEvent(kind="session_start"))
        bar_count = 0

        for candles_snapshot in self._data.iter_bars():
            bar_count += 1
            figi = self._cfg.figi

            # Проверяем дневные лимиты
            if self._portfolio.is_halted:
                continue

            # Публикуем бар → стратегия обработает синхронно через bus
            self._bus.publish(BarEvent(figi=figi, candles_snapshot=candles_snapshot))

            # Если есть открытая позиция — управляем ею
            if self._portfolio.has_position:
                self._manage_position(candles_snapshot)

            # Если стратегия сгенерировала сигнал — обрабатываем
            if self._pending_signal is not None:
                signal_event = self._pending_signal
                self._pending_signal = None
                self._handle_signal(signal_event, candles_snapshot)

        self._bus.publish(SessionEvent(kind="session_end"))
        stats = self._portfolio.session_stats()
        stats["bars_processed"] = bar_count
        logger.info(
            "Бэктест завершён: %d баров | %d сделок | PnL=%.0f руб.",
            bar_count, stats["total_trades"], stats["net_pnl"],
        )
        return stats

    # ─── Обработчики ─────────────────────────────────────────────────────────

    def _on_signal(self, event: SignalEvent) -> None:
        """Сохраняем сигнал — обработаем после публикации бара."""
        if event.signal and event.signal.action in ("open_long", "open_short"):
            self._pending_signal = event

    def _handle_signal(self, event: SignalEvent, candles: list[dict]) -> None:
        """Проверяем риск и открываем позицию."""
        if self._portfolio.has_position:
            return   # уже в позиции

        signal   = event.signal
        balance  = self._exec.get_balance_sync()
        go       = self._exec.get_go_per_contract_sync()

        allowed, reason = self._risk.check_entry(signal, balance, go)
        if not allowed:
            self._bus.publish(RiskEvent(reason=reason))
            return

        # Берём цену исполнения = close последней свечи
        fill_price = candles[-1]["close"]
        direction  = Direction(signal.params.direction.value)

        # Сохраняем параметры для Portfolio.on_order_filled
        self._portfolio.set_pending_order_params({
            "stop":            signal.params.stop,
            "take1":           signal.params.take1,
            "take2":           signal.params.take2,
            "risk_ticks":      signal.params.risk_ticks,
            "is_strong_trade": signal.is_strong_trade,
            "timeout_time":    signal.timeout_time or "21:00",
            "reason":          signal.reason,
        })

        order = self._exec.place_order_sync(
            direction = direction,
            quantity  = signal.contracts,
            price     = fill_price,
        )

        # Публикуем OrderFilled → Portfolio создаст Position
        self._bus.publish(OrderFilled(
            order_id   = order.order_id,
            figi       = self._cfg.figi,
            direction  = direction,
            quantity   = order.quantity,
            fill_price = order.fill_price,
            commission = order.commission,
        ))

    def _manage_position(self, candles: list[dict]) -> None:
        """Управление открытой позицией: стоп, тейки, таймаут."""
        pos    = self._portfolio.position
        result = self._strategy.manage(candles, pos)
        action = result.get("action", "hold")

        if action == "hold":
            return

        manage_action = ManageAction(
            action     = action,
            reason     = result.get("reason", ""),
            new_stop   = result.get("new_stop"),
            exit_price = result.get("exit_price"),
            pnl_approx = result.get("pnl_approx", 0.0),
            psar_update= result.get("psar_update"),
        )

        if action == "update_stop":
            self._portfolio.apply_manage_action(manage_action)
            return

        if action == "close_take1":
            # Закрываем половину позиции
            half = max(1, pos.contracts_total // 2)
            self._portfolio.apply_manage_action(manage_action)
            self._close_partial(pos, half, candles[-1]["close"], "take1")
            return

        # Полное закрытие: stop, take2, timeout, session, psar
        self._portfolio.set_pending_order_params({"reason": action})
        exit_price = result.get("exit_price") or candles[-1]["close"]
        close_order = self._exec.place_order_sync(
            direction = Direction.SHORT if pos.direction == Direction.LONG else Direction.LONG,
            quantity  = pos.contracts_remaining,
            price     = exit_price,
        )
        # PnL считает Portfolio через OrderFilled
        self._bus.publish(OrderFilled(
            order_id   = close_order.order_id,
            figi       = self._cfg.figi,
            direction  = close_order.side.value == "buy" and Direction.LONG or Direction.SHORT,
            quantity   = close_order.quantity,
            fill_price = close_order.fill_price,
            commission = close_order.commission,
        ))
        # Зачисляем PnL на виртуальный баланс бэктеста
        pnl = manage_action.pnl_approx
        self._exec.credit_pnl(pnl)
        self._risk.on_trade_closed(pnl)

    def _close_partial(self, pos, qty: int, price: float, reason: str) -> None:
        close_order = self._exec.place_order_sync(
            direction = Direction.SHORT if pos.direction == Direction.LONG else Direction.LONG,
            quantity  = qty,
            price     = price,
        )
        self._portfolio.set_pending_order_params({"reason": reason})
        self._bus.publish(OrderFilled(
            order_id   = close_order.order_id,
            figi       = self._cfg.figi,
            direction  = Direction.SHORT if pos.direction == Direction.LONG else Direction.LONG,
            quantity   = close_order.quantity,
            fill_price = close_order.fill_price,
            commission = close_order.commission,
        ))


# ─────────────────────────────────────────────────────────────────────────────
#  LIVE ENGINE (асинхронный)
# ─────────────────────────────────────────────────────────────────────────────

class LiveEngine:
    """
    Асинхронный движок для paper и live торговли.

    run() подписывается на aiter_bars() и на каждом новом баре выполняет
    ту же логику что и BacktestEngine, но через async execution provider.

    Стратегия, Portfolio, RiskManager — идентичны бэктесту.
    Отличается только execution и data provider.
    """

    def __init__(
        self,
        cfg: Settings,
        bus: EventBus,
        data: DataProvider,
        execution: ExecutionProvider,
        portfolio: Portfolio,
        strategy: LiquiditySweepStrategy,
        risk: RiskManager,
    ) -> None:
        self._cfg       = cfg
        self._bus       = bus
        self._data      = data
        self._exec      = execution
        self._portfolio = portfolio
        self._strategy  = strategy
        self._risk      = risk

        self._pending_signal: SignalEvent | None = None
        bus.subscribe(SignalEvent, self._on_signal)

    async def run(self) -> None:
        logger.info("LiveEngine запущен. figi=%s", self._cfg.figi)
        self._bus.publish(SessionEvent(kind="session_start"))

        # Обновляем баланс до старта
        balance = await self._exec.get_balance()
        self._portfolio.set_balance(balance)
        self._risk.set_day_start_balance(balance)

        async for candles_snapshot in self._data.aiter_bars():
            # Синхронизируем баланс каждые N баров (каждую минуту)
            try:
                balance = await self._exec.get_balance()
                self._portfolio.set_balance(balance)
            except Exception as e:
                logger.warning("Не удалось обновить баланс: %s", e)

            if self._portfolio.is_halted:
                continue

            self._bus.publish(BarEvent(
                figi             = self._cfg.figi,
                candles_snapshot = candles_snapshot,
            ))

            if self._portfolio.has_position:
                await self._manage_position(candles_snapshot)

            if self._pending_signal is not None:
                signal_event = self._pending_signal
                self._pending_signal = None
                await self._handle_signal(signal_event, candles_snapshot)

    # ─── Обработчики ─────────────────────────────────────────────────────────

    def _on_signal(self, event: SignalEvent) -> None:
        if event.signal and event.signal.action in ("open_long", "open_short"):
            self._pending_signal = event

    async def _handle_signal(self, event: SignalEvent, candles: list[dict]) -> None:
        if self._portfolio.has_position:
            return

        signal  = event.signal
        balance = self._portfolio.get_available_balance()
        go      = await self._exec.get_go_per_contract()

        allowed, reason = self._risk.check_entry(signal, balance, go)
        if not allowed:
            self._bus.publish(RiskEvent(reason=reason))
            return

        direction = Direction(signal.params.direction.value)

        self._portfolio.set_pending_order_params({
            "stop":            signal.params.stop,
            "take1":           signal.params.take1,
            "take2":           signal.params.take2,
            "risk_ticks":      signal.params.risk_ticks,
            "is_strong_trade": signal.is_strong_trade,
            "timeout_time":    signal.timeout_time or "21:00",
            "reason":          signal.reason,
        })

        try:
            order = await self._exec.place_order(
                direction = direction,
                quantity  = signal.contracts,
            )
        except Exception as e:
            logger.error("Ошибка размещения ордера: %s", e)
            self._portfolio.set_pending_order_params(None)
            return

        # Ждём реального исполнения (с таймаутом и отменой при зависании)
        try:
            fill = await wait_for_fill(
                order_id      = order.order_id,
                requested_qty = signal.contracts,
                initial_price = order.fill_price or 0.0,
                execution     = self._exec,
            )
        except OrderNotFilledError as e:
            logger.error("Ордер не исполнен: %s", e)
            self._portfolio.set_pending_order_params(None)
            return

        self._bus.publish(OrderFilled(
            order_id   = fill.order_id,
            figi       = self._cfg.figi,
            direction  = direction,
            quantity   = fill.quantity,       # фактически исполненное кол-во
            fill_price = fill.fill_price,     # гарантированно > 0
            commission = fill.commission,
        ))

    async def _manage_position(self, candles: list[dict]) -> None:
        pos    = self._portfolio.position
        result = self._strategy.manage(candles, pos)
        action = result.get("action", "hold")

        if action == "hold":
            return

        manage_action = ManageAction(
            action     = action,
            reason     = result.get("reason", ""),
            new_stop   = result.get("new_stop"),
            exit_price = result.get("exit_price"),
            pnl_approx = result.get("pnl_approx", 0.0),
            psar_update= result.get("psar_update"),
        )

        if action == "update_stop":
            self._portfolio.apply_manage_action(manage_action)
            return

        if action == "close_take1":
            half = max(1, pos.contracts_total // 2)
            self._portfolio.apply_manage_action(manage_action)
            await self._close_partial_async(pos, half, "take1")
            return

        # Полное закрытие
        close_dir = Direction.SHORT if pos.direction == Direction.LONG else Direction.LONG
        self._portfolio.set_pending_order_params({"reason": action})
        try:
            order = await self._exec.place_order(
                direction = close_dir,
                quantity  = pos.contracts_remaining,
            )
        except Exception as e:
            logger.error("Ошибка закрытия позиции: %s", e)
            return

        self._bus.publish(OrderFilled(
            order_id   = order.order_id,
            figi       = self._cfg.figi,
            direction  = close_dir,
            quantity   = order.quantity,
            fill_price = order.fill_price or candles[-1]["close"],
            commission = order.commission,
        ))
        self._risk.on_trade_closed(manage_action.pnl_approx)

    async def _close_partial_async(self, pos, qty: int, reason: str) -> None:
        close_dir = Direction.SHORT if pos.direction == Direction.LONG else Direction.LONG
        self._portfolio.set_pending_order_params({"reason": reason})
        try:
            order = await self._exec.place_order(direction=close_dir, quantity=qty)
        except Exception as e:
            logger.error("Ошибка частичного закрытия: %s", e)
            return
        self._bus.publish(OrderFilled(
            order_id   = order.order_id,
            figi       = self._cfg.figi,
            direction  = close_dir,
            quantity   = order.quantity,
            fill_price = order.fill_price or 0.0,
            commission = order.commission,
        ))
