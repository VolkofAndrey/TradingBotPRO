"""
config/settings.py — загрузка и валидация конфигурации.

Порядок приоритетов (от низкого к высокому):
  1. config/base.yaml
  2. config/{mode}.yaml  (backtest / paper / live)
  3. Переменные окружения  (BOT_FIGI=..., BOT_RISK_PER_TRADE=...)
  4. .env файл

Использование:
  from config.settings import get_settings
  cfg = get_settings("live")
"""

from __future__ import annotations

import os
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_CONFIG_DIR = Path(__file__).parent


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _merge_configs(mode: str) -> dict:
    base    = _load_yaml(_CONFIG_DIR / "base.yaml")
    overlay = _load_yaml(_CONFIG_DIR / f"{mode}.yaml")
    return {**base, **overlay}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Инструмент ──────────────────────────────────────────────────────────
    figi:   str = "FUTSI0325000"
    ticker: str = "Si-3.25"

    # ─── Тик ────────────────────────────────────────────────────────────────
    tick_size:       float = 1.0
    tick_value:      float = 10.0
    go_per_contract: float = 5000.0

    # ─── Риск ────────────────────────────────────────────────────────────────
    risk_per_trade:    float = 0.01
    max_margin_usage:  float = 0.5
    max_contracts:     int   = 10

    # ─── ATR ────────────────────────────────────────────────────────────────
    atr_period:      int = 20
    atr_fast_period: int = 10

    # ─── Объём ──────────────────────────────────────────────────────────────
    volume_ma_period:    int   = 20
    volume_multiplier:   float = 1.3

    # ─── Перегрев ────────────────────────────────────────────────────────────
    overextend_atr_mult: float = 2.5

    # ─── ADX ────────────────────────────────────────────────────────────────
    adx_period:     int   = 14
    adx_trend_min:  float = 35.0
    adx_weak_min:   float = 20.0
    with_trend_only: bool = True

    # ─── Уровни ────────────────────────────────────────────────────────────
    level_lookback_bars:  int   = 390
    level_proximity_ticks: int  = 2
    level_min_touches:    int   = 2

    # ─── Sweep ──────────────────────────────────────────────────────────────
    sweep_body_atr_min:  float = 0.0
    sweep_prev_candles:  int   = 5

    # ─── Return ─────────────────────────────────────────────────────────────
    return_min_ticks:   int = 2
    return_max_candles: int = 5

    # ─── Сделка ─────────────────────────────────────────────────────────────
    stop_buffer_ticks:  int   = 5
    stop_max_atr_mult:  float = 2.5
    take1_r_mult:       float = 1.0
    take2_atr_mult:     float = 1.5
    min_rr:             float = 1.5
    take2_mode:         str   = "psar"

    # ─── PSAR ───────────────────────────────────────────────────────────────
    psar_af_start:      float = 0.02
    psar_af_step:       float = 0.02
    psar_af_max:        float = 0.20
    psar_activation_r:  float = 2.5

    # ─── Сессии ─────────────────────────────────────────────────────────────
    session_a_start: str = "09:00"
    session_a_end:   str = "18:30"
    session_b_start: Optional[str] = "18:30"
    session_b_end:   Optional[str] = "21:00"
    hard_close_time: str = "21:00"
    strong_trade_close_time:  str  = "21:00"
    strong_trade_session_end: bool = True

    # ─── Таймаут ────────────────────────────────────────────────────────────
    position_timeout_min: int = 45

    # ─── Дневные лимиты ──────────────────────────────────────────────────────
    daily_drawdown_limit:    float = 0.02
    max_consecutive_losses:  int   = 2

    # ─── Логирование ────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_file:  str = "logs/bot.log"

    # ─── Tinkoff API ────────────────────────────────────────────────────────
    tinkoff_token: SecretStr = Field(default=SecretStr(""), description="Токен T-Инвестиций из .env")
    tinkoff_account_id: str = Field(default="", description="ID торгового счёта")

    # ─── Производные свойства (не из YAML) ───────────────────────────────────
    @property
    def session_a_start_t(self) -> time:
        return time.fromisoformat(self.session_a_start)

    @property
    def session_a_end_t(self) -> time:
        return time.fromisoformat(self.session_a_end)

    @property
    def session_b_start_t(self) -> Optional[time]:
        return time.fromisoformat(self.session_b_start) if self.session_b_start else None

    @property
    def session_b_end_t(self) -> Optional[time]:
        return time.fromisoformat(self.session_b_end) if self.session_b_end else None

    @property
    def hard_close_time_t(self) -> time:
        return time.fromisoformat(self.hard_close_time)

    @property
    def strong_trade_close_time_t(self) -> time:
        return time.fromisoformat(self.strong_trade_close_time)

    @field_validator("take2_mode")
    @classmethod
    def validate_take2_mode(cls, v: str) -> str:
        allowed = {"fixed", "trail", "psar"}
        if v not in allowed:
            raise ValueError(f"take2_mode должен быть одним из {allowed}, получено: {v!r}")
        return v

    @field_validator("risk_per_trade")
    @classmethod
    def validate_risk(cls, v: float) -> float:
        if not 0 < v <= 0.05:
            raise ValueError(f"risk_per_trade={v} вне диапазона (0, 0.05]")
        return v


Settings.model_rebuild()


@lru_cache(maxsize=4)
def get_settings(mode: str = "live") -> Settings:
    """
    Фабрика настроек с кэшем. Вызывать один раз на старте.

    mode: "backtest" | "paper" | "live"
    """
    merged = _merge_configs(mode)
    return Settings(**merged)
