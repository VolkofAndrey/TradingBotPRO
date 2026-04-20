"""
data/base.py — абстрактный интерфейс провайдера рыночных данных.

Стратегия и engine работают только с этим ABC.
Конкретные реализации (historical, live) подставляются через DI.

Ключевой принцип:
  - historical.py — синхронный, возвращает данные из CSV/Parquet.
  - live.py        — асинхронный, читает WebSocket стрим T-Инвестиций.
  Оба реализуют один и тот же интерфейс.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import AsyncIterator, Iterator, List

from core.models import Bar


class DataProvider(ABC):
    """
    Абстрактный провайдер рыночных данных.

    Два режима работы:
      sync  — iter_bars()       для backtest (нет I/O, чистая итерация)
      async — aiter_bars()      для live/paper (WebSocket, REST polling)

    Конкретный класс реализует тот метод, который ему нужен.
    По умолчанию оба бросают NotImplementedError — чтобы было явно,
    если вызвать неподходящий метод.
    """

    @abstractmethod
    def get_figi(self) -> str:
        """FIGI инструмента, который обслуживает этот провайдер."""

    # ─── Синхронный интерфейс (backtest) ─────────────────────────────────────

    def iter_bars(self) -> Iterator[list[dict]]:
        """
        Синхронный генератор: на каждой итерации отдаёт актуальный
        список dict-свечей (окно до текущего момента включительно).

        Используется только в BacktestEngine.
        """
        raise NotImplementedError(
            f"{type(self).__name__} не поддерживает синхронный iter_bars(). "
            "Используйте HistoricalDataProvider."
        )

    def get_warmup_bars(self, n: int) -> list[dict]:
        """
        Вернуть n последних закрытых свечей для прогрева индикаторов.
        Используется live/paper провайдером при старте.
        """
        raise NotImplementedError(
            f"{type(self).__name__} не реализует get_warmup_bars(). "
        )

    # ─── Асинхронный интерфейс (live / paper) ────────────────────────────────

    async def aiter_bars(self) -> AsyncIterator[list[dict]]:
        """
        Асинхронный генератор: отдаёт снапшот свечей при каждой
        новой закрытой M1-свече из стрима.

        Используется только в LiveEngine.
        """
        raise NotImplementedError(
            f"{type(self).__name__} не поддерживает async aiter_bars(). "
            "Используйте LiveDataProvider."
        )
        # чтобы Python распознал метод как async generator:
        yield  # type: ignore[misc]
