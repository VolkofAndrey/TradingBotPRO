"""
execution/order_poller.py — надёжное ожидание исполнения ордера.

Решает три сценария:
  1. Ордер исполнен сразу (FILLED) — возвращаем fill_price.
  2. Ордер принят (ACCEPTED) — ждём до POLL_TIMEOUT_SEC, опрашивая каждые POLL_INTERVAL_SEC.
  3. Ордер не исполнился за таймаут — отменяем и бросаем OrderNotFilledError.

Также проверяем частичное исполнение: если filled < requested — логируем WARNING
и используем фактическое quantity (не теряем позицию тихо).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger("bot.execution.poller")

POLL_INTERVAL_SEC = 0.5   # опрос каждые 500мс
POLL_TIMEOUT_SEC  = 10.0  # ждём максимум 10 секунд


class OrderNotFilledError(Exception):
    """Ордер не исполнился за отведённое время и был отменён."""


@dataclass
class FillResult:
    order_id:   str
    fill_price: float
    quantity:   int       # фактически исполненное количество
    commission: float


async def wait_for_fill(
    order_id:        str,
    requested_qty:   int,
    initial_price:   float,   # цена из первоначального ответа (может быть 0)
    execution,                # ExecutionProvider
    timeout_sec:     float = POLL_TIMEOUT_SEC,
    interval_sec:    float = POLL_INTERVAL_SEC,
) -> FillResult:
    """
    Ждать исполнения ордера.

    Алгоритм:
      1. Если initial_price > 0 — ордер уже исполнен, возвращаем сразу.
      2. Иначе — опрашиваем get_order_status() каждые interval_sec.
      3. При FILLED — возвращаем FillResult.
      4. При таймауте — вызываем cancel_order() и бросаем OrderNotFilledError.
    """
    # Случай 1: уже исполнен
    if initial_price > 0:
        logger.debug("Ордер %s исполнен сразу: price=%.2f", order_id, initial_price)
        return FillResult(
            order_id   = order_id,
            fill_price = initial_price,
            quantity   = requested_qty,
            commission = 0.0,
        )

    # Случай 2: ждём исполнения
    logger.info(
        "Ордер %s принят, ждём исполнения (таймаут=%.0fs)...",
        order_id, timeout_sec,
    )
    elapsed = 0.0
    while elapsed < timeout_sec:
        await asyncio.sleep(interval_sec)
        elapsed += interval_sec

        order = await execution.get_order_status(order_id)
        if order is None:
            logger.warning("get_order_status(%s) вернул None", order_id)
            continue

        from core.models import OrderStatus
        if order.status == OrderStatus.FILLED:
            fill_price = order.fill_price or 0.0
            if fill_price <= 0:
                logger.critical(
                    "Ордер %s FILLED но fill_price=0! "
                    "Это ошибка API или частичное исполнение. "
                    "Позиция НЕ будет открыта.",
                    order_id,
                )
                raise OrderNotFilledError(
                    f"Ордер {order_id} FILLED с fill_price=0"
                )

            if order.quantity < requested_qty:
                logger.warning(
                    "Частичное исполнение ордера %s: запрошено=%d, исполнено=%d",
                    order_id, requested_qty, order.quantity,
                )

            logger.info(
                "Ордер %s исполнен: price=%.2f qty=%d (%.1fs)",
                order_id, fill_price, order.quantity, elapsed,
            )
            return FillResult(
                order_id   = order_id,
                fill_price = fill_price,
                quantity   = order.quantity,
                commission = order.commission,
            )

        elif order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            raise OrderNotFilledError(
                f"Ордер {order_id} отменён/отклонён: {order.status}"
            )

    # Случай 3: таймаут — отменяем
    logger.error(
        "Ордер %s не исполнился за %.0fs — отменяем",
        order_id, timeout_sec,
    )
    cancelled = await execution.cancel_order(order_id)
    if cancelled:
        logger.info("Ордер %s успешно отменён", order_id)
    else:
        logger.error("Не удалось отменить ордер %s!", order_id)

    raise OrderNotFilledError(
        f"Ордер {order_id} не исполнился за {timeout_sec}s и был отменён"
    )
