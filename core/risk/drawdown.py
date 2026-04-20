"""
core/risk/drawdown.py — защита от просадки.

DrawdownGuard отслеживает:
  - Дневную просадку (сравнивает баланс с началом дня).
  - Серию последовательных убытков.

Полностью stateless относительно I/O.
check() возвращает (allowed: bool, reason: str).
"""

from __future__ import annotations

from datetime import date

from config.settings import Settings


class DrawdownGuard:
    """
    Дневной риск-лимит.

    Состояние сбрасывается автоматически при смене календарной даты.
    """

    def __init__(self, cfg: Settings, initial_balance: float) -> None:
        self._cfg                 = cfg
        self._day                 = date.today()
        self._day_start_balance   = initial_balance
        self._consecutive_losses  = 0
        self._halted              = False

    def record_trade(self, pnl: float) -> None:
        """Зафиксировать результат закрытой сделки."""
        self._maybe_reset()
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

    def check(self, current_balance: float) -> tuple[bool, str]:
        """
        Проверить лимиты.
        Возвращает (True, "") если торговля разрешена,
        иначе (False, причина).
        """
        self._maybe_reset()

        if self._halted:
            return False, "Торговля остановлена на сегодня"

        # Дневная просадка
        if self._day_start_balance > 0:
            dd = (self._day_start_balance - current_balance) / self._day_start_balance
            if dd >= self._cfg.daily_drawdown_limit:
                self._halted = True
                return False, (
                    f"Дневная просадка {dd:.1%} ≥ лимита "
                    f"{self._cfg.daily_drawdown_limit:.1%}"
                )

        # Серия убытков
        if self._consecutive_losses >= self._cfg.max_consecutive_losses:
            self._halted = True
            return False, (
                f"Серия убытков {self._consecutive_losses} ≥ лимита "
                f"{self._cfg.max_consecutive_losses}"
            )

        return True, ""

    def _maybe_reset(self) -> None:
        today = date.today()
        if today != self._day:
            self._day                = today
            self._consecutive_losses = 0
            self._halted             = False
            # day_start_balance обновляется снаружи через set_day_start

    def set_day_start_balance(self, balance: float) -> None:
        """Зафиксировать баланс на начало нового торгового дня."""
        self._day_start_balance = balance
