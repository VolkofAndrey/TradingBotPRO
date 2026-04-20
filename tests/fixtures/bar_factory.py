"""
tests/fixtures/bar_factory.py — фабрика тестовых баров.

Используется во всех unit и integration тестах.
Возвращает dict-бары (совместимы с strategy_logic.py) или Bar-объекты (pydantic).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import List, Optional


def make_bar(
    close:   float,
    open_:   Optional[float] = None,
    high:    Optional[float] = None,
    low:     Optional[float] = None,
    volume:  int = 10_000,
    dt:      Optional[datetime] = None,
    complete: bool = True,
) -> dict:
    """Создать один бар в формате dict (как в strategy_logic.py)."""
    dt = dt or datetime(2024, 6, 1, 10, 0, 0)
    c  = close
    return {
        "datetime": dt,
        "date":     dt.date(),
        "time":     dt.time(),
        "open":     open_ if open_ is not None else c,
        "high":     high  if high  is not None else c,
        "low":      low   if low   is not None else c,
        "close":    c,
        "volume":   volume,
        "complete": complete,
    }


def make_bars(
    closes:     List[float],
    start:      Optional[datetime] = None,
    volume:     int = 10_000,
    high_delta: float = 5.0,   # high = close + high_delta
    low_delta:  float = 5.0,   # low  = close - low_delta
) -> List[dict]:
    """
    Создать список баров по ценам закрытия.
    Каждый следующий бар сдвинут на 1 минуту.
    """
    start = start or datetime(2024, 6, 1, 10, 0, 0)
    bars  = []
    for i, c in enumerate(closes):
        dt = start + timedelta(minutes=i)
        bars.append({
            "datetime": dt,
            "date":     dt.date(),
            "time":     dt.time(),
            "open":     c,
            "high":     c + high_delta,
            "low":      c - low_delta,
            "close":    c,
            "volume":   volume,
            "complete": True,
        })
    return bars


def make_sweep_bars(
    level:     float,
    direction: str = "up",   # "up" → sweep вверх (шорт), "down" → sweep вниз (лонг)
    n_before:  int = 10,
    n_after:   int = 5,
    atr:       float = 20.0,
) -> List[dict]:
    """
    Генерирует последовательность баров с паттерном sweep:
      - n_before баров торгуются около уровня
      - 1 бар пробивает уровень (sweep)
      - n_after баров возвращаются за уровень

    direction="up"  → цена пробивает level вверх (потом возвращается вниз) → сигнал SHORT
    direction="down"→ цена пробивает level вниз (потом возвращается вверх) → сигнал LONG
    """
    bars = []
    dt   = datetime(2024, 6, 3, 10, 0, 0)

    if direction == "up":
        # Бары ниже уровня
        for i in range(n_before):
            bars.append(make_bar(
                close=level - atr * 0.3,
                high=level - atr * 0.1,
                low=level - atr * 0.5,
                dt=dt + timedelta(minutes=i),
            ))
        # Sweep-бар: high > level, тело маленькое
        i = n_before
        bars.append(make_bar(
            close=level - 1,       # закрылся ниже уровня (возврат)
            open_=level - 2,
            high=level + atr * 0.6,  # пробил вверх
            low=level - 3,
            dt=dt + timedelta(minutes=i),
        ))
        # Бары после — торгуются ниже уровня (возврат)
        for j in range(n_after):
            bars.append(make_bar(
                close=level - atr * 0.4,
                high=level - atr * 0.1,
                low=level - atr * 0.6,
                dt=dt + timedelta(minutes=n_before + 1 + j),
            ))
    else:  # down
        for i in range(n_before):
            bars.append(make_bar(
                close=level + atr * 0.3,
                high=level + atr * 0.5,
                low=level + atr * 0.1,
                dt=dt + timedelta(minutes=i),
            ))
        i = n_before
        bars.append(make_bar(
            close=level + 1,
            open_=level + 2,
            high=level + 3,
            low=level - atr * 0.6,   # пробил вниз
            dt=dt + timedelta(minutes=i),
        ))
        for j in range(n_after):
            bars.append(make_bar(
                close=level + atr * 0.4,
                high=level + atr * 0.6,
                low=level + atr * 0.1,
                dt=dt + timedelta(minutes=n_before + 1 + j),
            ))

    return bars
