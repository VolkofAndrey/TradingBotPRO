"""
execution/paper.py — исполнение через sandbox T-Инвестиций (paper trading).

Использует SandboxClient / sandbox-методы API.
Логика идентична TinkoffExecutionProvider, но:
  - Все ордера идут в sandbox-счёт.
  - Баланс — виртуальный (пополняется через sandbox_pay_in).
  - Можно запускать параллельно с реальным счётом без риска.

API sandbox:
  client.sandbox.post_sandbox_order(...)       — разместить ордер
  client.sandbox.get_sandbox_positions(...)    — позиции
  client.sandbox.sandbox_pay_in(...)           — пополнить баланс
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Optional

from config.settings import Settings
from core.models import Direction, Order, OrderSide, OrderStatus
from execution.base import ExecutionProvider
from execution.retry import with_retry

logger = logging.getLogger("bot.execution.paper")


class PaperExecutionProvider(ExecutionProvider):
    """
    Асинхронный провайдер для paper trading через sandbox T-Инвестиций.

    При старте автоматически пополняет sandbox-счёт до нужного баланса,
    если текущий баланс ниже min_balance.
    """

    def __init__(self, cfg: Settings, initial_balance: float = 1_000_000.0) -> None:
        self._cfg             = cfg
        self._figi            = cfg.figi
        self._token           = cfg.tinkoff_token
        self._account_id      = cfg.tinkoff_account_id
        self._initial_balance = initial_balance
        self._client          = None
        self._client_ctx      = None

    # ─── Context manager ─────────────────────────────────────────────────────

    async def __aenter__(self):
        from tinkoff.invest.sandbox.client import AsyncSandboxClient
        self._client_ctx = AsyncSandboxClient(self._token)
        self._client = await self._client_ctx.__aenter__()
        await self._ensure_balance()
        logger.info(
            "Sandbox-клиент открыт | account_id=%s | баланс=%.0f",
            self._account_id, self._initial_balance,
        )
        return self

    async def __aexit__(self, *args):
        if self._client_ctx:
            await self._client_ctx.__aexit__(*args)
        logger.info("Sandbox-клиент закрыт")

    async def _ensure_balance(self) -> None:
        """Проверяем баланс и при необходимости пополняем."""
        from tinkoff.invest import MoneyValue
        from tinkoff.invest.utils import decimal_to_quotation, quotation_to_decimal

        try:
            current = await self.get_balance()
            if current < self._initial_balance * 0.5:
                top_up = self._initial_balance - current
                money  = decimal_to_quotation(Decimal(str(top_up)))
                await self._client.sandbox.sandbox_pay_in(
                    account_id=self._account_id,
                    amount=MoneyValue(units=money.units, nano=money.nano, currency="rub"),
                )
                logger.info("Sandbox пополнен на %.0f руб.", top_up)
        except Exception as e:
            logger.warning("_ensure_balance: %s", e)

    # ─── Интерфейс ExecutionProvider ─────────────────────────────────────────

    def get_figi(self) -> str:
        return self._figi

    def get_account_id(self) -> str:
        return self._account_id

    async def place_order(
        self,
        direction: Direction,
        quantity: int,
    ) -> Order:
        from tinkoff.invest import OrderDirection, OrderType
        from tinkoff.invest.utils import quotation_to_decimal

        tinkoff_dir = (
            OrderDirection.ORDER_DIRECTION_BUY
            if direction == Direction.LONG
            else OrderDirection.ORDER_DIRECTION_SELL
        )
        order_id = str(uuid.uuid4())

        if quantity <= 0:
            raise ValueError(f"place_order: quantity={quantity} должно быть > 0")

        response = await with_retry(
            self._client.sandbox.post_sandbox_order,
            instrument_id = self._figi,
            quantity      = quantity,
            direction     = tinkoff_dir,
            account_id    = self._account_id,
            order_type    = OrderType.ORDER_TYPE_MARKET,
            order_id      = order_id,
            operation     = f"paper_order({self._figi} {direction.value} {quantity})",
        )

        fill_price = 0.0
        commission = 0.0
        try:
            ep   = response.executed_order_price
            fill_price = float(quotation_to_decimal(ep)) if ep else 0.0
            comm = response.initial_commission
            commission = float(quotation_to_decimal(comm)) if comm else 0.0
        except Exception:
            pass

        order = Order(
            order_id   = response.order_id,
            figi       = self._figi,
            side       = OrderSide.BUY if direction == Direction.LONG else OrderSide.SELL,
            quantity   = quantity,
            status     = OrderStatus.FILLED if fill_price > 0 else OrderStatus.ACCEPTED,
            fill_price = fill_price or None,
            commission = commission,
        )
        logger.info(
            "[PAPER] Ордер: %s %d конт. | fill=%.2f",
            direction.value, quantity, fill_price,
        )
        return order

    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self._client.sandbox.cancel_sandbox_order(
                account_id=self._account_id,
                order_id=order_id,
            )
            return True
        except Exception as e:
            logger.error("paper cancel_order: %s", e)
            return False

    async def get_balance(self) -> float:
        from tinkoff.invest.utils import quotation_to_decimal

        positions = await self._client.sandbox.get_sandbox_positions(
            account_id=self._account_id
        )
        rub = 0.0
        for money in positions.money:
            if money.currency.lower() in ("rub", "ruble", "руб"):
                rub += float(quotation_to_decimal(money))
        return rub

    async def get_go_per_contract(self) -> float:
        """Для sandbox берём из конфига — маржа не меняется динамически."""
        return self._cfg.go_per_contract

    async def get_order_status(self, order_id: str) -> Optional[Order]:
        from tinkoff.invest.utils import quotation_to_decimal

        try:
            resp = await self._client.sandbox.get_sandbox_order_state(
                account_id=self._account_id,
                order_id=order_id,
            )
            fill_price = 0.0
            ep = resp.average_position_price
            if ep:
                fill_price = float(quotation_to_decimal(ep))
            return Order(
                order_id   = order_id,
                figi       = self._figi,
                side       = OrderSide.BUY,
                quantity   = resp.lots_requested,
                status     = OrderStatus.FILLED if resp.lots_executed == resp.lots_requested
                             else OrderStatus.ACCEPTED,
                fill_price = fill_price or None,
            )
        except Exception as e:
            logger.error("paper get_order_status: %s", e)
            return None
