"""
execution/tinkoff.py — исполнение ордеров через реальный счёт T-Инвестиций.

Использует tinkoff-investments SDK:
  from tinkoff.invest import AsyncClient, OrderDirection, OrderType, ...

Все методы асинхронные. Используется в LiveEngine.

Особенности:
  - Рыночные ордера (ORDER_TYPE_MARKET).
  - quotation_to_decimal / decimal_to_quotation для конвертации цен.
  - get_balance() → свободные средства из get_positions().
  - get_go_per_contract() → initial_margin из get_instrument_by().
  - Retry при временных ошибках gRPC (UNAVAILABLE, DEADLINE_EXCEEDED).
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

logger = logging.getLogger("bot.execution.tinkoff")



class TinkoffExecutionProvider(ExecutionProvider):
    """
    Асинхронный провайдер исполнения для реального счёта T-Инвестиций.

    Создаётся один раз и живёт всё время работы LiveEngine.
    AsyncClient открывается в __aenter__ и закрывается в __aexit__.
    """

    def __init__(self, cfg: Settings) -> None:
        self._cfg        = cfg
        self._figi       = cfg.figi
        self._token      = cfg.tinkoff_token.get_secret_value()
        self._account_id = cfg.tinkoff_account_id
        self._client     = None   # устанавливается в __aenter__

    # ─── Context manager ─────────────────────────────────────────────────────

    async def __aenter__(self):
        from tinkoff.invest import AsyncClient
        self._client_ctx = AsyncClient(self._token)
        self._client = await self._client_ctx.__aenter__()
        logger.info("Tinkoff AsyncClient открыт (live счёт)")
        return self

    async def __aexit__(self, *args):
        if self._client_ctx:
            await self._client_ctx.__aexit__(*args)
        logger.info("Tinkoff AsyncClient закрыт")

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
        """
        Выставить рыночный ордер.
        Возвращает Order с fill_price = средняя цена исполнения (если доступна сразу).
        """
        from tinkoff.invest import (
            OrderDirection,
            OrderType,
        )
        from tinkoff.invest.utils import quotation_to_decimal

        tinkoff_dir = (
            OrderDirection.ORDER_DIRECTION_BUY
            if direction == Direction.LONG
            else OrderDirection.ORDER_DIRECTION_SELL
        )

        order_id = str(uuid.uuid4())

        # Валидация перед отправкой
        if quantity <= 0:
            raise ValueError(f"place_order: quantity={quantity} должно быть > 0")

        response = await with_retry(
            self._client.orders.post_order,
            instrument_id = self._figi,
            quantity      = quantity,
            direction     = tinkoff_dir,
            account_id    = self._account_id,
            order_type    = OrderType.ORDER_TYPE_MARKET,
            order_id      = order_id,
            operation     = f"place_order({self._figi} {direction.value} {quantity})",
        )

        # Извлекаем цену исполнения (может быть 0 если ордер ещё не исполнен)
        fill_price = 0.0
        commission = 0.0
        try:
            ep = response.executed_order_price
            fill_price = float(quotation_to_decimal(ep)) if ep else 0.0
            comm = response.initial_commission
            commission = float(quotation_to_decimal(comm)) if comm else 0.0
        except Exception:
            pass

        status = OrderStatus.FILLED if fill_price > 0 else OrderStatus.ACCEPTED

        order = Order(
            order_id   = response.order_id,
            figi       = self._figi,
            side       = OrderSide.BUY if direction == Direction.LONG else OrderSide.SELL,
            quantity   = quantity,
            status     = status,
            fill_price = fill_price or None,
            commission = commission,
        )
        logger.info(
            "Ордер размещён: %s %d конт. | order_id=%s | fill=%.2f",
            direction.value, quantity, order.order_id, fill_price,
        )
        return order

    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self._client.orders.cancel_order(
                account_id=self._account_id,
                order_id=order_id,
            )
            return True
        except Exception as e:
            logger.error("cancel_order(%s) failed: %s", order_id, e)
            return False

    async def get_balance(self) -> float:
        """
        Свободные средства = сумма money позиций в рублях.
        """
        from tinkoff.invest.utils import quotation_to_decimal

        positions = await self._client.operations.get_positions(
            account_id=self._account_id
        )
        rub = 0.0
        for money in positions.money:
            if money.currency.lower() in ("rub", "ruble", "руб"):
                rub += float(quotation_to_decimal(money))
        return rub

    async def get_go_per_contract(self) -> float:
        """
        Начальная маржа (ГО) на 1 контракт из карточки инструмента.
        Fallback → конфиг.
        """
        from tinkoff.invest import InstrumentIdType
        from tinkoff.invest.utils import quotation_to_decimal

        try:
            resp = await self._client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
                id=self._figi,
            )
            margin = resp.instrument.initial_margin_on_buy
            if margin and (margin.units or margin.nano):
                return float(quotation_to_decimal(margin))
        except Exception as e:
            logger.warning("get_go_per_contract fallback: %s", e)

        return self._cfg.go_per_contract

    async def get_order_status(self, order_id: str) -> Optional[Order]:
        from tinkoff.invest.utils import quotation_to_decimal

        try:
            resp = await self._client.orders.get_order_state(
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
                side       = OrderSide.BUY,  # упрощение
                quantity   = resp.lots_requested,
                status     = OrderStatus.FILLED if resp.lots_executed == resp.lots_requested
                             else OrderStatus.ACCEPTED,
                fill_price = fill_price or None,
            )
        except Exception as e:
            logger.error("get_order_status(%s): %s", order_id, e)
            return None
