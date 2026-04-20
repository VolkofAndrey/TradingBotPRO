"""
core/risk/position_sizer.py — расчёт размера позиции.

Реализованы два метода:
  FixedFractionSizer  — фиксированный процент от баланса на риск (используется в стратегии).
  VolatilitySizer     — размер через ATR (альтернатива, не используется по умолчанию).

Чистые функции / stateless классы — легко тестировать изолированно.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

from config.settings import Settings


class PositionSizer(ABC):
    @abstractmethod
    def calculate(
        self,
        balance: float,
        risk_ticks: float,
        go_per_contract: float,
        cfg: Settings,
    ) -> int:
        """Вернуть количество контрактов (>= 0)."""


class FixedFractionSizer(PositionSizer):
    """
    Стандартный риск-сайзинг стратегии:
      risk_rub       = balance × RISK_PER_TRADE          (1% от баланса)
      contracts_risk = risk_rub / (risk_ticks × TICK_VALUE)
      contracts_go   = balance × MAX_MARGIN_USAGE / go   (лимит по ГО)
      result         = min(contracts_risk, contracts_go, MAX_CONTRACTS)

    Это точная копия логики calculate_position_size() из strategy_logic.py,
    вынесенная в отдельный тестируемый класс.
    """

    def calculate(
        self,
        balance: float,
        risk_ticks: float,
        go_per_contract: float,
        cfg: Settings,
    ) -> int:
        if risk_ticks <= 0 or balance <= 0:
            return 0

        # Риск-сайзинг
        risk_rub       = balance * cfg.risk_per_trade
        contracts_risk = int(risk_rub / (risk_ticks * cfg.tick_value))
        contracts_risk = max(1, contracts_risk)

        # Лимит по ГО
        go = go_per_contract if go_per_contract > 0 else cfg.go_per_contract
        if cfg.max_margin_usage > 0 and go > 0:
            margin_limit     = balance * cfg.max_margin_usage
            contracts_margin = int(margin_limit / go)
            contracts_risk   = min(contracts_risk, max(1, contracts_margin))

        # Абсолютный потолок
        if cfg.max_contracts:
            contracts_risk = min(contracts_risk, cfg.max_contracts)

        return contracts_risk


class VolatilitySizer(PositionSizer):
    """
    Альтернативный сайзинг через ATR:
      risk_rub  = balance × RISK_PER_TRADE
      contracts = risk_rub / (atr_multiplier × ATR × TICK_VALUE)

    Используется если хочется, чтобы риск масштабировался с волатильностью.
    Передаётся atr через risk_ticks (интерпретируем как ATR-единицы).
    """

    def __init__(self, atr_multiplier: float = 1.5) -> None:
        self.atr_multiplier = atr_multiplier

    def calculate(
        self,
        balance: float,
        risk_ticks: float,   # здесь: ATR в тиках
        go_per_contract: float,
        cfg: Settings,
    ) -> int:
        if risk_ticks <= 0 or balance <= 0:
            return 0
        atr_risk = risk_ticks * self.atr_multiplier
        risk_rub  = balance * cfg.risk_per_trade
        contracts = int(risk_rub / (atr_risk * cfg.tick_value))
        contracts = max(1, contracts)
        if cfg.max_contracts:
            contracts = min(contracts, cfg.max_contracts)
        return contracts
