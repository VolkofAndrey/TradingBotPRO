"""
core/risk/manager.py — оркестратор риска.

Единая точка входа: check_entry(signal, balance) → (allowed, reason).
Собирает все проверки в одном месте, не реализует логику сам.

Намеренно тонкий: бизнес-логика живёт в DrawdownGuard, PositionSizer и т.д.
"""

from __future__ import annotations

import logging

from config.settings import Settings
from core.models import Direction, Signal
from core.risk.drawdown import DrawdownGuard
from core.risk.position_sizer import FixedFractionSizer, PositionSizer

logger = logging.getLogger("bot.risk.manager")


class RiskManager:
    """
    Оркестратор всех риск-проверок перед открытием позиции.

    check_entry() вызывается engine'ом после получения SignalEvent.
    Если хотя бы одна проверка не прошла — вход блокируется.
    """

    def __init__(
        self,
        cfg: Settings,
        initial_balance: float,
        sizer: PositionSizer | None = None,
    ) -> None:
        self._cfg      = cfg
        self._sizer    = sizer or FixedFractionSizer()
        self._drawdown = DrawdownGuard(cfg, initial_balance)

    # ─── Основной интерфейс ──────────────────────────────────────────────────

    def check_entry(
        self,
        signal: Signal,
        balance: float,
        go_per_contract: float = 0.0,
    ) -> tuple[bool, str]:
        """
        Проверить все условия для входа.
        Возвращает (allowed: bool, reason: str).

        Порядок проверок (от критичных к менее критичным):
          1. Дневные лимиты (просадка, серия убытков).
          2. Минимальный баланс для открытия хотя бы 1 контракта.
          3. R:R (дополнительная проверка — основная уже в strategy_logic).
        """
        # 1. Дневные лимиты
        allowed, reason = self._drawdown.check(balance)
        if not allowed:
            logger.warning("Вход заблокирован: %s", reason)
            return False, reason

        # 2. Размер позиции > 0
        if signal.params is None:
            return False, "Нет параметров сделки"

        contracts = self._sizer.calculate(
            balance         = balance,
            risk_ticks      = signal.params.risk_ticks,
            go_per_contract = go_per_contract,
            cfg             = self._cfg,
        )
        if contracts <= 0:
            reason = "Размер позиции = 0 (недостаточно баланса)"
            logger.warning("Вход заблокирован: %s", reason)
            return False, reason

        # 3. Минимальный R:R (дополнительная защита)
        if (self._cfg.take2_mode == "fixed" and
                signal.params.rr < self._cfg.min_rr):
            reason = f"R:R {signal.params.rr:.2f} < минимума {self._cfg.min_rr}"
            logger.warning("Вход заблокирован: %s", reason)
            return False, reason

        return True, ""

    def on_trade_closed(self, pnl: float) -> None:
        """Зафиксировать результат сделки (обновляет счётчики просадки)."""
        self._drawdown.record_trade(pnl)

    def set_day_start_balance(self, balance: float) -> None:
        """Вызывается в начале торговой сессии."""
        self._drawdown.set_day_start_balance(balance)

    def calculate_contracts(
        self,
        balance: float,
        risk_ticks: float,
        go_per_contract: float = 0.0,
    ) -> int:
        """Рассчитать размер позиции через текущий sizer."""
        return self._sizer.calculate(
            balance         = balance,
            risk_ticks      = risk_ticks,
            go_per_contract = go_per_contract,
            cfg             = self._cfg,
        )
