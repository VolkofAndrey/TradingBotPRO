"""
execution/backtest.py — симуляция исполнения ордеров для бэктеста.

Принципы:
  - Полностью синхронный. Никакого async, никаких сетевых вызовов.
  - Рыночный ордер исполняется мгновенно по close последней свечи.
  - Комиссия рассчитывается как процент от суммы сделки (COMMISSION_RATE).
  - Баланс уменьшается на ГО при открытии, восстанавливается при закрытии.
  - Слипидж опционален (SLIPPAGE_TICKS добавляется к цене против позиции).

Используется исключительно в BacktestEngine — стратегия не знает о его существовании.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from config.settings import Settings
from core.models import Direction, Order, OrderSide, OrderStatus
from execution.base import ExecutionProvider

logger = logging.getLogger("bot.execution.backtest")

# Комиссия T-Инвестиций для фьючерсов (примерное значение, уточни в тарифах)
COMMISSION_RATE = 0.0004   # 0.04% от суммы сделки


class BacktestExecutionProvider(ExecutionProvider):
    """
    Симулятор исполнения ордеров для бэктеста.

    Хранит виртуальный баланс и список исполненных ордеров.
    place_order_sync() — мгновенное исполнение по переданной цене.
    """

    def __init__(
        self,
        cfg: Settings,
        initial_balance: float,
        slippage_ticks: int = 1,
    ) -> None:
        self._cfg             = cfg
        self._figi            = cfg.figi
        self._balance         = initial_balance
        self._initial_balance = initial_balance
        self._slippage_ticks  = slippage_ticks
        self._orders: list[Order] = []
        self._open_margin: float  = 0.0   # замороженное ГО

    def get_figi(self) -> str:
        return self._figi

    def get_account_id(self) -> str:
        return "backtest-account"

    # ─── Синхронный интерфейс ────────────────────────────────────────────────

    def place_order_sync(
        self,
        direction: Direction,
        quantity: int,
        price: float,
    ) -> Order:
        """
        Мгновенное исполнение рыночного ордера.

        Для покупки (LONG) — цена чуть выше (слипидж против позиции).
        Для продажи (SHORT) — цена чуть ниже.
        Комиссия = COMMISSION_RATE × цена × quantity × tick_value.
        """
        if quantity <= 0:
            raise ValueError(f"place_order_sync: quantity={quantity} должно быть > 0")
        if price <= 0:
            raise ValueError(f"place_order_sync: price={price} должно быть > 0")

        tick = self._cfg.tick_size
        slip = self._slippage_ticks * tick

        fill_price = price + slip if direction == Direction.LONG else price - slip

        # Комиссия в рублях
        # Для фьючерсов: комиссия от суммы = price * lot_size * contracts
        # Упрощённо через tick_value: комиссия = fill_price * contracts * COMMISSION_RATE
        commission = fill_price * quantity * COMMISSION_RATE

        order_id = f"bt-{uuid.uuid4().hex[:8]}"
        side = OrderSide.BUY if direction == Direction.LONG else OrderSide.SELL

        # Обновляем баланс
        go = self._cfg.go_per_contract * quantity
        if direction == Direction.LONG:
            # Открываем: замораживаем ГО
            self._open_margin += go
            self._balance     -= commission
        else:
            # Закрываем шорт или открываем шорт — логика одинаковая
            self._open_margin = max(0.0, self._open_margin - go)
            self._balance    -= commission

        order = Order(
            order_id   = order_id,
            figi       = self._figi,
            side       = side,
            quantity   = quantity,
            status     = OrderStatus.FILLED,
            filled_at  = datetime.utcnow(),
            fill_price = fill_price,
            commission = commission,
        )
        self._orders.append(order)
        logger.debug(
            "Backtest ордер: %s %d конт. по %.2f | комм=%.2f | баланс=%.0f",
            direction.value, quantity, fill_price, commission, self._balance,
        )
        return order

    def get_balance_sync(self) -> float:
        return self._balance

    def get_go_per_contract_sync(self) -> float:
        return self._cfg.go_per_contract

    # ─── Управление балансом ─────────────────────────────────────────────────

    def credit_pnl(self, pnl: float) -> None:
        """
        Зачислить PnL на баланс (вызывается engine при закрытии позиции).
        В реальном брокере это происходит автоматически — здесь симулируем.
        """
        self._balance += pnl
        self._open_margin = 0.0

    # ─── Статистика ──────────────────────────────────────────────────────────

    @property
    def orders(self) -> list[Order]:
        return list(self._orders)

    @property
    def final_balance(self) -> float:
        return self._balance

    @property
    def total_return(self) -> float:
        return (self._balance - self._initial_balance) / self._initial_balance
