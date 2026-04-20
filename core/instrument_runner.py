"""
core/instrument_runner.py — запуск нескольких инструментов параллельно.

Каждый инструмент получает свой изолированный набор объектов:
  EventBus, Strategy, Portfolio, RiskManager, DataProvider, ExecutionProvider.

Для backtest — запуск последовательный (один за другим), результаты объединяются.
Для live/paper — запуск параллельный через asyncio.gather().

Пример использования в run_live.py:
  runner = MultiInstrumentRunner()
  runner.add("FUTSI0625000", build_live("FUTSI0625000"))
  runner.add("FUTSNG0625000", build_live("FUTSNG0625000"))
  await runner.run_all_async()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("bot.multi_runner")


@dataclass
class InstrumentResult:
    figi:         str
    stats:        dict
    error:        str | None = None


class MultiInstrumentRunner:
    """
    Оркестратор для торговли несколькими инструментами.

    Каждый инструмент полностью изолирован: отдельный EventBus,
    отдельный Portfolio, отдельная Strategy. Они не знают друг о друге.
    """

    def __init__(self) -> None:
        self._containers: list[tuple[str, object]] = []

    def add(self, figi: str, container) -> "MultiInstrumentRunner":
        """Добавить контейнер инструмента (BacktestContainer или LiveContainer)."""
        self._containers.append((figi, container))
        return self

    # ─── Backtest (последовательный) ─────────────────────────────────────────

    def run_all_backtest(self) -> list[InstrumentResult]:
        """
        Запустить бэктест для всех инструментов последовательно.
        Возвращает список результатов.
        """
        results = []
        for figi, container in self._containers:
            logger.info("Backtest: %s", figi)
            try:
                stats = container.engine.run()
                results.append(InstrumentResult(figi=figi, stats=stats))
            except Exception as e:
                logger.exception("Backtest %s упал: %s", figi, e)
                results.append(InstrumentResult(figi=figi, stats={}, error=str(e)))
        return results

    # ─── Live/Paper (параллельный) ────────────────────────────────────────────

    async def run_all_async(self) -> None:
        """
        Запустить live/paper торговлю для всех инструментов параллельно.

        Каждый инструмент работает в своей coroutine.
        Если один падает — остальные продолжают.
        Graceful stop: при Ctrl+C все задачи отменяются корректно.
        """
        if not self._containers:
            logger.warning("MultiInstrumentRunner: нет инструментов для запуска")
            return

        tasks = []
        for figi, container in self._containers:
            task = asyncio.create_task(
                self._run_one_async(figi, container),
                name=f"engine-{figi}",
            )
            tasks.append(task)

        logger.info(
            "Запускаем %d инструментов параллельно: %s",
            len(tasks),
            [figi for figi, _ in self._containers],
        )

        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except asyncio.CancelledError:
            logger.info("MultiInstrumentRunner: получен CancelledError, останавливаем")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_one_async(self, figi: str, container) -> None:
        """Запустить один инструмент, пробрасывая исключения наверх."""
        try:
            async with container.execution:
                await container.engine.run()
        except asyncio.CancelledError:
            raise   # пробрасываем для корректного shutdown
        except Exception as e:
            logger.exception("Инструмент %s упал: %s", figi, e)
            # Не пробрасываем — остальные инструменты должны продолжать работу
