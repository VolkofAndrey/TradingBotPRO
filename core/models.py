"""
core/models.py — единый формат данных (pydantic v2).

Все модели иммутабельны (frozen=True) — случайная мутация состояния невозможна.
Исключение: Position — меняется по ходу жизни сделки (take1_hit, current_stop и т.д.).
"""

from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ─────────────────────────────────────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────────────────────────────────────

class Direction(str, Enum):
    LONG  = "long"
    SHORT = "short"


class OrderSide(str, Enum):
    BUY  = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING   = "pending"
    ACCEPTED  = "accepted"
    FILLED    = "filled"
    CANCELLED = "cancelled"
    REJECTED  = "rejected"


class Take2Mode(str, Enum):
    FIXED = "fixed"
    TRAIL = "trail"
    PSAR  = "psar"


class MarketMode(str, Enum):
    STRONG_TREND = "strong_trend"
    WEAK_TREND   = "weak_trend"
    RANGE        = "range"


# ─────────────────────────────────────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────────────────────────────────────

class Bar(BaseModel, frozen=True):
    """
    Минутная свеча. Является основным входным типом для стратегии.
    complete=False означает незакрытую (текущую) свечу.
    """
    datetime_: datetime = Field(alias="datetime")
    date_:     date     = Field(alias="date")
    time_:     time     = Field(alias="time")
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    int
    complete:  bool = True

    model_config = {"populate_by_name": True}

    def to_dict(self) -> dict:
        """Совместимость со старым кодом strategy.py, который работает с dict."""
        return {
            "datetime": self.datetime_,
            "date":     self.date_,
            "time":     self.time_,
            "open":     self.open,
            "high":     self.high,
            "low":      self.low,
            "close":    self.close,
            "volume":   self.volume,
            "complete": self.complete,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

class TradeParams(BaseModel, frozen=True):
    """Параметры конкретной сделки, рассчитанные стратегией."""
    entry:      float
    stop:       float
    take1:      float
    take2:      float
    risk_ticks: float
    direction:  Direction
    rr:         float


class Signal(BaseModel, frozen=True):
    """Сигнал на открытие позиции."""
    action:     str            # "open_long" | "open_short" | "pending_sweep" | "wait" | "no_signal"
    params:     Optional[TradeParams] = None
    contracts:  int            = 0
    sweep:      Optional[dict] = None
    is_strong_trade: bool      = False
    timeout_time: Optional[str] = None
    atr:        float          = 0.0
    atr_fast:   float          = 0.0
    reason:     str            = ""


class ManageAction(BaseModel, frozen=True):
    """Результат manage_position — что делать с открытой позицией."""
    action:      str           # "hold" | "close_stop" | "close_take1" | "close_take2" |
                               # "close_timeout" | "close_session" | "update_stop"
    reason:      str           = ""
    new_stop:    Optional[float] = None
    exit_price:  Optional[float] = None
    pnl_approx:  float         = 0.0
    psar_update: Optional[tuple] = None   # (active, ep, af, val)


# ─────────────────────────────────────────────────────────────────────────────
#  ORDERS
# ─────────────────────────────────────────────────────────────────────────────

class Order(BaseModel):
    """Ордер, отправленный брокеру."""
    order_id:   str
    figi:       str
    side:       OrderSide
    quantity:   int
    status:     OrderStatus = OrderStatus.PENDING
    filled_at:  Optional[datetime] = None
    fill_price: Optional[float]    = None
    commission: float              = 0.0

    def is_done(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


# ─────────────────────────────────────────────────────────────────────────────
#  POSITION
# ─────────────────────────────────────────────────────────────────────────────

class Position(BaseModel):
    """
    Открытая позиция. Мутабельна — состояние меняется при take1, трейлинге и т.д.
    """
    figi:              str
    direction:         Direction
    entry_price:       float
    contracts_total:   int
    contracts_remaining: int
    current_stop:      float
    take1:             float
    take2:             float
    risk_ticks:        float
    opened_at:         datetime
    timeout_time:      time
    is_strong_trade:   bool  = False
    take1_hit:         bool  = False

    # Трейлинг
    trail_stop: Optional[float] = None

    # PSAR-состояние
    psar_active: bool          = False
    psar_ep:     float         = 0.0
    psar_af:     float         = 0.02
    psar_val:    Optional[float] = None

    @model_validator(mode="after")
    def check_stop_direction(self) -> "Position":
        if self.direction == Direction.LONG:
            assert self.current_stop < self.entry_price, (
                f"LONG: stop({self.current_stop}) должен быть < entry({self.entry_price})"
            )
        else:
            assert self.current_stop > self.entry_price, (
                f"SHORT: stop({self.current_stop}) должен быть > entry({self.entry_price})"
            )
        return self


# ─────────────────────────────────────────────────────────────────────────────
#  ACCOUNT
# ─────────────────────────────────────────────────────────────────────────────

class Account(BaseModel, frozen=True):
    balance:   float
    currency:  str = "RUB"


# ─────────────────────────────────────────────────────────────────────────────
#  TRADE (закрытая сделка — для статистики)
# ─────────────────────────────────────────────────────────────────────────────

class Trade(BaseModel, frozen=True):
    figi:          str
    direction:     Direction
    entry_price:   float
    exit_price:    float
    contracts:     int
    pnl:           float
    commission:    float
    opened_at:     datetime
    closed_at:     datetime
    close_reason:  str         # "stop" | "take1" | "take2" | "timeout" | "session" | "psar"

    @property
    def net_pnl(self) -> float:
        return self.pnl - self.commission
