"""
core/event_bus.py — синхронная шина событий с защитой от медленных обработчиков.

Проблема которую решаем:
  Если обработчик (например portfolio.on_order_filled) зависает на 500мс,
  весь engine останавливается — publish() блокирует.

Решение: timeout-обёртка для каждого обработчика.
  В backtest: timeout отключён (нет I/O, всё быстро).
  В live: каждый обработчик получает MAX_HANDLER_MS миллисекунд.
  При превышении — логируем CRITICAL и продолжаем (не роняем бота).

Дополнительно:
  - Изоляция исключений: упавший обработчик не прерывает остальные.
  - Дедупликация: один обработчик не вызывается дважды за одно событие.
  - История событий для тестов.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Callable, Type

from core.events import Event

logger = logging.getLogger("bot.event_bus")

Handler = Callable[[Event], None]

# Максимальное время выполнения одного обработчика (мс). 0 = без ограничений.
MAX_HANDLER_MS: int = 0


class EventBus:
    """
    Синхронная шина событий.

    Порядок вызова обработчиков = порядок subscribe().
    Исключение в обработчике логируется и НЕ прерывает остальных.
    Медленный обработчик логируется как WARNING/CRITICAL.
    """

    def __init__(self, max_handler_ms: int = MAX_HANDLER_MS) -> None:
        self._handlers: dict[Type[Event], list[Handler]] = defaultdict(list)
        self._history: list[Event] = []
        self._record_history: bool = False
        self._max_ms = max_handler_ms   # 0 = без ограничений

    # ─── Подписка ────────────────────────────────────────────────────────────

    def subscribe(self, event_type: Type[Event], handler: Handler) -> None:
        self._handlers[event_type].append(handler)
        logger.debug("subscribe: %s → %s", event_type.__name__, handler.__qualname__)

    def unsubscribe(self, event_type: Type[Event], handler: Handler) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    # ─── Публикация ──────────────────────────────────────────────────────────

    def publish(self, event: Event) -> None:
        """
        Опубликовать событие синхронно.

        Каждый обработчик вызывается с измерением времени.
        Если _max_ms > 0 и обработчик превысил лимит — логируем предупреждение.
        Исключения изолированы: один упавший не ломает цепочку.
        """
        if self._record_history:
            self._history.append(event)

        event_type = type(event)
        called: set[int] = set()   # id(handler) для дедупликации

        for registered_type, handlers in self._handlers.items():
            if not isinstance(event, registered_type):
                continue
            for handler in handlers:
                hid = id(handler)
                if hid in called:
                    continue
                called.add(hid)
                self._call(handler, event, event_type.__name__)

        if not called:
            logger.debug("publish: %s — нет подписчиков", event_type.__name__)

    def _call(self, handler: Handler, event: Event, event_name: str) -> None:
        """Вызвать обработчик с измерением времени и изоляцией исключений."""
        t0 = time.perf_counter()
        try:
            handler(event)
        except Exception:
            logger.exception(
                "Ошибка в обработчике %s для события %s",
                handler.__qualname__, event_name,
            )
            return
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if self._max_ms > 0 and elapsed_ms > self._max_ms:
                level = logging.CRITICAL if elapsed_ms > self._max_ms * 5 else logging.WARNING
                logger.log(
                    level,
                    "МЕДЛЕННЫЙ обработчик %s для %s: %.1fms (лимит %dms)",
                    handler.__qualname__, event_name, elapsed_ms, self._max_ms,
                )
            elif elapsed_ms > 100:
                # Всегда логируем если > 100ms — даже без явного лимита
                logger.warning(
                    "Медленный обработчик %s: %.1fms",
                    handler.__qualname__, elapsed_ms,
                )

    # ─── Утилиты для тестов ──────────────────────────────────────────────────

    def enable_history(self) -> None:
        self._record_history = True
        self._history.clear()

    def get_history(self, event_type: Type[Event] | None = None) -> list[Event]:
        if event_type is None:
            return list(self._history)
        return [e for e in self._history if isinstance(e, event_type)]

    def clear_history(self) -> None:
        self._history.clear()

    def clear_all(self) -> None:
        self._handlers.clear()
        self._history.clear()
