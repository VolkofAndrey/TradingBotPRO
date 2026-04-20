"""
core/portfolio.py — управление позициями, PnL и дневной статистикой.

Слушает события OrderFilled, PositionUpdated, PositionClosed.
Публикует PositionOpened, PositionClosed, RiskEvent (при превышении лимитов).

Не знает об API, не знает об async — чистая синхронная логика.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from config.settings import Settings
from core.event_bus import EventBus
from core.events import (
    ManageAction,
    OrderFilled,
    PositionClosed,
    PositionOpened,
    PositionUpdated,
    RiskEvent,
)
from core.models import Direction, Position, Trade

logger = logging.getLogger("bot.portfolio")


class Portfolio:
    """
    Текущее состояние торгового счёта с точки зрения бота.

    Хранит:
    - Открытую позицию (max 1 одновременно).
    - Историю закрытых сделок за сессию.
    - Дневную статистику (просадка, серия убытков).
    - Текущий баланс (обновляется из execution-провайдера через set_balance).
    """

    def __init__(self, bus: EventBus, cfg: Settings, initial_balance: float = 0.0) -> None:
        self._bus = bus
        self._cfg = cfg

        self._balance:  float = initial_balance
        self._position: Optional[Position] = None
        self._trades:   list[Trade] = []

        # Дневные лимиты
        self._day:               date  = date.today()
        self._day_start_balance: float = initial_balance
        self._consecutive_losses: int  = 0
        self._trading_halted:    bool  = False

        # Хранит параметры ожидающего ордера (до OrderFilled)
        self._pending_order_params: Optional[dict] = None

        bus.subscribe(OrderFilled, self.on_order_filled)
        logger.info("Portfolio инициализирован. Баланс: %.0f руб.", initial_balance)

    # ─── Публичный интерфейс ─────────────────────────────────────────────────

    def set_balance(self, balance: float) -> None:
        """Обновить баланс из execution-провайдера (вызывается извне)."""
        self._balance = balance

    def set_pending_order_params(self, params: dict | None) -> None:
        """
        Сохранить параметры ордера до его исполнения.
        Вызывается engine'ом перед отправкой ордера.
        None — сброс при ошибке (ордер не исполнился).
        """
        self._pending_order_params = params

    def get_available_balance(self) -> float:
        return self._balance

    @property
    def position(self) -> Optional[Position]:
        return self._position

    @property
    def has_position(self) -> bool:
        return self._position is not None

    @property
    def is_halted(self) -> bool:
        return self._trading_halted

    @property
    def trades(self) -> list[Trade]:
        return list(self._trades)

    # ─── Обработчик OrderFilled ──────────────────────────────────────────────

    def on_order_filled(self, event: OrderFilled) -> None:
        """
        Ордер исполнен → создать/закрыть позицию.
        Отличаем открывающий ордер от закрывающего по наличию позиции.
        """
        self._maybe_reset_day()

        if self._trading_halted:
            logger.warning("on_order_filled: торговля остановлена, игнорируем")
            return

        if self._position is None:
            self._open_position(event)
        else:
            self._close_position(event)

    # ─── Управление позицией (вызывается engine'ом каждую минуту) ────────────

    def apply_manage_action(self, action: ManageAction) -> None:
        """
        Применить результат manage_position() к портфелю.
        Публикует PositionUpdated с деталями изменения.
        """
        if self._position is None:
            return

        pos = self._position
        act = action.action

        if act == "update_stop":
            if action.new_stop is not None:
                old_pos = pos.model_copy()
                pos.current_stop = action.new_stop

                # PSAR-обновление
                if action.psar_update:
                    active, ep, af, val = action.psar_update
                    pos.psar_active = active
                    pos.psar_ep = ep
                    pos.psar_af = af
                    pos.psar_val = val

                # Трейлинг-стоп
                if "trail" in action.reason.lower():
                    pos.trail_stop = action.new_stop

                self._bus.publish(PositionUpdated(
                    figi=pos.figi,
                    action=action,
                    position_before=old_pos,
                ))

        elif act == "close_take1":
            # Половина позиции закрыта — переводим стоп в безубыток
            old_pos = pos.model_copy()
            pos.take1_hit = True
            pos.contracts_remaining = max(1, pos.contracts_total // 2)
            pos.current_stop = pos.entry_price  # безубыток
            self._bus.publish(PositionUpdated(
                figi=pos.figi,
                action=action,
                position_before=old_pos,
            ))
            logger.info(
                "Take1 достигнут. Стоп → безубыток (%.2f). Остаток: %d конт.",
                pos.entry_price, pos.contracts_remaining,
            )

    # ─── Проверка дневных лимитов ─────────────────────────────────────────────

    def check_daily_limits(self) -> bool:
        """
        Вернуть True если торговля разрешена.
        Публикует RiskEvent и останавливает торговлю при превышении лимитов.
        """
        self._maybe_reset_day()

        if self._trading_halted:
            return False

        # Просадка
        if self._day_start_balance > 0:
            drawdown = (self._day_start_balance - self._balance) / self._day_start_balance
            if drawdown >= self._cfg.daily_drawdown_limit:
                self._halt(f"Дневная просадка {drawdown:.1%} >= лимита {self._cfg.daily_drawdown_limit:.1%}")
                return False

        # Серия убытков
        if self._consecutive_losses >= self._cfg.max_consecutive_losses:
            self._halt(f"Серия убытков {self._consecutive_losses} >= лимита {self._cfg.max_consecutive_losses}")
            return False

        return True

    # ─── Статистика ──────────────────────────────────────────────────────────

    def session_stats(self) -> dict:
        total = len(self._trades)
        wins  = sum(1 for t in self._trades if t.net_pnl > 0)
        pnl   = sum(t.net_pnl for t in self._trades)
        return {
            "total_trades":  total,
            "wins":          wins,
            "losses":        total - wins,
            "winrate":       wins / total if total else 0.0,
            "net_pnl":       pnl,
            "balance":       self._balance,
        }

    # ─── Приватные методы ────────────────────────────────────────────────────

    def _open_position(self, event: OrderFilled) -> None:
        p = self._pending_order_params
        if p is None:
            logger.error("OrderFilled без pending_order_params — позиция не создана")
            return

        from datetime import time as dtime
        timeout_str = p.get("timeout_time", "21:00")
        try:
            timeout_t = dtime.fromisoformat(timeout_str)
        except Exception:
            timeout_t = dtime(21, 0)

        pos = Position(
            figi=event.figi,
            direction=event.direction,
            entry_price=event.fill_price,
            contracts_total=event.quantity,
            contracts_remaining=event.quantity,
            current_stop=p["stop"],
            take1=p["take1"],
            take2=p["take2"],
            risk_ticks=p["risk_ticks"],
            opened_at=event.ts,
            timeout_time=timeout_t,
            is_strong_trade=p.get("is_strong_trade", False),
        )
        self._position = pos
        self._pending_order_params = None
        logger.info(
            "Позиция открыта: %s %d конт. по %.2f | стоп=%.2f | take1=%.2f",
            event.direction.value, event.quantity, event.fill_price,
            p["stop"], p["take1"],
        )
        self._bus.publish(PositionOpened(figi=event.figi, position=pos))

    def _close_position(self, event: OrderFilled) -> None:
        pos = self._position
        if pos is None:
            return

        qty = event.quantity
        if pos.direction == Direction.LONG:
            pnl = (event.fill_price - pos.entry_price) * qty * self._cfg.tick_value
        else:
            pnl = (pos.entry_price - event.fill_price) * qty * self._cfg.tick_value

        net_pnl = pnl - event.commission

        trade = Trade(
            figi=pos.figi,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=event.fill_price,
            contracts=qty,
            pnl=pnl,
            commission=event.commission,
            opened_at=pos.opened_at,
            closed_at=event.ts,
            close_reason=self._pending_order_params.get("reason", "unknown") if self._pending_order_params else "unknown",
        )
        self._trades.append(trade)

        # Обновляем серию убытков
        if net_pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        logger.info(
            "Позиция закрыта: %s по %.2f | PnL=%.0f руб. (комм. %.0f) | серия убытков: %d",
            pos.direction.value, event.fill_price, pnl, event.commission, self._consecutive_losses,
        )

        self._position = None
        self._pending_order_params = None

        self._bus.publish(PositionClosed(
            figi=pos.figi,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=event.fill_price,
            contracts=qty,
            pnl=pnl,
            commission=event.commission,
            close_reason=trade.close_reason,
        ))

    def _halt(self, reason: str) -> None:
        self._trading_halted = True
        logger.warning("Торговля ОСТАНОВЛЕНА: %s", reason)
        self._bus.publish(RiskEvent(reason=reason, is_fatal=True))

    def _maybe_reset_day(self) -> None:
        today = date.today()
        if today != self._day:
            logger.info("Новый торговый день: %s. Сброс дневных лимитов.", today)
            self._day = today
            self._day_start_balance = self._balance
            self._consecutive_losses = 0
            self._trading_halted = False
