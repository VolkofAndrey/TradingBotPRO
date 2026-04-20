"""
core/events.py — все события системы.

Используем dataclass с field(default=...) для всех полей после ts,
потому что базовый Event имеет ts с default_factory — Python требует,
чтобы все поля в подклассах тоже имели дефолты.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List

from core.models import Direction, ManageAction, Position, Signal


# ─────────────────────────────────────────────────────────────────────────────
#  BASE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Event:
    ts: datetime = field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BarEvent(Event):
    figi:             str  = field(default="")
    candles_snapshot: list = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
#  STRATEGY SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignalEvent(Event):
    figi:   str    = field(default="")
    signal: Signal = field(default=None)   # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
#  ORDERS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrderEvent(Event):
    figi:            str       = field(default="")
    direction:       Direction = field(default=Direction.LONG)
    quantity:        int       = field(default=0)
    stop:            float     = field(default=0.0)
    take1:           float     = field(default=0.0)
    take2:           float     = field(default=0.0)
    risk_ticks:      float     = field(default=0.0)
    is_strong_trade: bool      = field(default=False)
    timeout_time:    str       = field(default="21:00")


@dataclass(frozen=True)
class OrderAccepted(Event):
    order_id: str = field(default="")
    figi:     str = field(default="")


@dataclass(frozen=True)
class OrderFilled(Event):
    order_id:   str       = field(default="")
    figi:       str       = field(default="")
    direction:  Direction = field(default=Direction.LONG)
    quantity:   int       = field(default=0)
    fill_price: float     = field(default=0.0)
    commission: float     = field(default=0.0)


@dataclass(frozen=True)
class OrderRejected(Event):
    order_id: str = field(default="")
    figi:     str = field(default="")
    reason:   str = field(default="")


@dataclass(frozen=True)
class OrderCancelled(Event):
    order_id: str = field(default="")
    figi:     str = field(default="")
    reason:   str = field(default="")


# ─────────────────────────────────────────────────────────────────────────────
#  POSITION LIFECYCLE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PositionOpened(Event):
    figi:     str      = field(default="")
    position: Position = field(default=None)  # type: ignore[assignment]


@dataclass(frozen=True)
class PositionUpdated(Event):
    figi:            str          = field(default="")
    action:          ManageAction = field(default=None)   # type: ignore[assignment]
    position_before: Position     = field(default=None)  # type: ignore[assignment]


@dataclass(frozen=True)
class PositionClosed(Event):
    figi:         str       = field(default="")
    direction:    Direction = field(default=Direction.LONG)
    entry_price:  float     = field(default=0.0)
    exit_price:   float     = field(default=0.0)
    contracts:    int       = field(default=0)
    pnl:          float     = field(default=0.0)
    commission:   float     = field(default=0.0)
    close_reason: str       = field(default="")


# ─────────────────────────────────────────────────────────────────────────────
#  RISK
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RiskEvent(Event):
    reason:   str  = field(default="")
    is_fatal: bool = field(default=False)


# ─────────────────────────────────────────────────────────────────────────────
#  SESSION
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionEvent(Event):
    kind: str = field(default="")
    info: str = field(default="")
