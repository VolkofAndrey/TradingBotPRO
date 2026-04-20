"""
execution/retry.py — экспоненциальный backoff для gRPC вызовов.

Заменяет простой sleep(1.0 * attempt) на правильную стратегию:
  attempt 1: wait 1s
  attempt 2: wait 2s
  attempt 3: wait 4s
  attempt N: wait min(2^(N-1), MAX_WAIT) + jitter

Jitter (случайная добавка ±30%) предотвращает thundering herd:
когда все боты одновременно переподключаются после сбоя API.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable, TypeVar

logger = logging.getLogger("bot.execution.retry")

T = TypeVar("T")

# Коды ошибок gRPC при которых имеет смысл retry
RETRYABLE_CODES = frozenset({
    "UNAVAILABLE",
    "DEADLINE_EXCEEDED",
    "RESOURCE_EXHAUSTED",   # rate limit
    "INTERNAL",             # временные ошибки сервера
})

MAX_WAIT_SEC   = 30.0   # максимальная пауза между попытками
MAX_RETRIES    = 5      # максимум попыток


def _is_retryable(exc: Exception) -> bool:
    """Проверяет, стоит ли повторять запрос после данного исключения."""
    msg = str(exc).upper()
    return any(code in msg for code in RETRYABLE_CODES)


def _wait_time(attempt: int) -> float:
    """
    Экспоненциальный backoff с jitter.
    attempt=1 → ~1s, attempt=2 → ~2s, attempt=3 → ~4s, ...
    """
    base    = min(2 ** (attempt - 1), MAX_WAIT_SEC)
    jitter  = base * 0.3 * (random.random() * 2 - 1)  # ±30%
    return max(0.5, base + jitter)


async def with_retry(
    coro_fn: Callable,
    *args,
    max_retries: int = MAX_RETRIES,
    operation: str = "API call",
    **kwargs,
):
    """
    Выполнить async-функцию с retry и exponential backoff.

    Использование:
      result = await with_retry(client.orders.post_order, instrument_id=..., ...)
    """
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt >= max_retries:
                logger.error(
                    "%s: финальная ошибка после %d попыток: %s",
                    operation, attempt, exc,
                )
                raise

            wait = _wait_time(attempt)
            logger.warning(
                "%s: ошибка (попытка %d/%d), ждём %.1fs: %s",
                operation, attempt, max_retries, wait, exc,
            )
            await asyncio.sleep(wait)

    raise last_exc  # никогда не достигается, для type checker
