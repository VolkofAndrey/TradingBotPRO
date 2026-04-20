"""
execution/base.py — абстрактный интерфейс исполнения ордеров.

Стратегия и engine работают только с этим ABC.
Конкретные реализации (backtest, paper, tinkoff) подставляются через DI.

Синхронный вариант (backtest):
  place_order_sync(), get_position_sync(), get_balance_sync()

Асинхронный вариант (paper / live):
  async place_order(), async cancel_order(), async get_balance()
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from core.models import Direction, Order, OrderStatus


class ExecutionProvider(ABC):
    """
    Абстрактный провайдер исполнения ордеров.

    Как и DataProvider, имеет два режима:
      sync  — backtest (нет I/O, мгновенное исполнение по close)
      async — paper / live (реальный или sandbox Tinkoff API)
    """

    @abstractmethod
    def get_figi(self) -> str:
        """FIGI инструмента."""

    @abstractmethod
    def get_account_id(self) -> str:
        """ID торгового счёта."""

    # ─── Синхронный интерфейс (backtest) ─────────────────────────────────────

    def place_order_sync(
        self,
        direction: Direction,
        quantity: int,
        price: float,
    ) -> Order:
        """
        Разместить рыночный ордер синхронно (мгновенное исполнение по цене price).
        Используется только BacktestExecutionProvider.
        """
        raise NotImplementedError(
            f"{type(self).__name__} не поддерживает синхронный place_order_sync()."
        )

    def get_balance_sync(self) -> float:
        """Вернуть текущий доступный баланс (синхронно)."""
        raise NotImplementedError(
            f"{type(self).__name__} не поддерживает синхронный get_balance_sync()."
        )

    def get_go_per_contract_sync(self) -> float:
        """Вернуть актуальное ГО на контракт (синхронно). Fallback — из конфига."""
        raise NotImplementedError

    # ─── Асинхронный интерфейс (paper / live) ────────────────────────────────

    async def place_order(
        self,
        direction: Direction,
        quantity: int,
    ) -> Order:
        """Разместить рыночный ордер асинхронно."""
        raise NotImplementedError(
            f"{type(self).__name__} не поддерживает async place_order()."
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Отменить ордер. Возвращает True если отменён успешно."""
        raise NotImplementedError

    async def get_balance(self) -> float:
        """Вернуть текущий доступный баланс (асинхронно)."""
        raise NotImplementedError

    async def get_go_per_contract(self) -> float:
        """Вернуть актуальное ГО на контракт (асинхронно)."""
        raise NotImplementedError

    async def get_order_status(self, order_id: str) -> Optional[Order]:
        """Запросить статус ордера по ID."""
        raise NotImplementedError
