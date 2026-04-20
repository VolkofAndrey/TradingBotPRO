"""
data/live.py — провайдер живых данных через T-Инвестиции.

Два источника данных на старте:
  1. REST get_all_candles() — прогрев (последние N M1-баров истории).
  2. WebSocket MarketDataStream — подписка на закрытые M1-свечи в реальном времени.

При каждой новой закрытой свече aiter_bars() отдаёт актуальное скользящее окно.

Импорты:
  from tinkoff.invest import AsyncClient, CandleInstrument, ...
  (пакет tinkoff-investments, новый SDK)

quotation_to_decimal — конвертация Quotation {units, nano} → Decimal → float.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import AsyncIterator

from config.settings import Settings
from data.base import DataProvider

logger = logging.getLogger("bot.data.live")

# Максимальный размер окна свечей в памяти
CANDLE_WINDOW = 800

# Пауза между попытками переподключения стрима
RECONNECT_DELAY_SEC = 5


def _quotation_to_float(q) -> float:
    """Конвертирует tinkoff Quotation {units: int, nano: int} → float."""
    from tinkoff.invest.utils import quotation_to_decimal
    return float(quotation_to_decimal(q))


def _candle_to_dict(c, complete: bool = True) -> dict:
    """
    Конвертирует HistoricCandle или Candle (из стрима) в dict-формат стратегии.
    Поля time у них называются одинаково — .time (datetime с timezone).
    """
    dt = c.time
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz=None).replace(tzinfo=None)
    return {
        "datetime": dt,
        "date":     dt.date(),
        "time":     dt.time(),
        "open":     _quotation_to_float(c.open),
        "high":     _quotation_to_float(c.high),
        "low":      _quotation_to_float(c.low),
        "close":    _quotation_to_float(c.close),
        "volume":   int(c.volume),
        "complete": complete,
    }


class LiveDataProvider(DataProvider):
    """
    Асинхронный провайдер данных через T-Инвестиции.

    Использование:
      provider = LiveDataProvider(cfg)
      async for candles_snapshot in provider.aiter_bars():
          bus.publish(BarEvent(figi=cfg.figi, candles_snapshot=candles_snapshot))
    """

    def __init__(self, cfg: Settings) -> None:
        self._cfg    = cfg
        self._figi   = cfg.figi
        self._token  = cfg.tinkoff_token.get_secret_value()
        self._window: deque[dict] = deque(maxlen=CANDLE_WINDOW)

    def get_figi(self) -> str:
        return self._figi

    # ─── Прогрев ─────────────────────────────────────────────────────────────

    async def _load_warmup(self, client, n: int = 450) -> None:
        """
        Загружает последние n M1-баров через REST API для прогрева индикаторов.
        Вызывается один раз при старте, до подписки на стрим.
        """
        from tinkoff.invest import CandleInterval
        from tinkoff.invest.utils import now as tinow

        logger.info("Прогрев: загружаем %d баров M1 для figi=%s", n, self._figi)
        count = 0
        async for candle in client.get_all_candles(
            instrument_id=self._figi,
            from_=tinow() - timedelta(minutes=n + 60),  # запас на клиринг
            to=tinow(),
            interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
        ):
            if candle.is_complete:
                self._window.append(_candle_to_dict(candle, complete=True))
                count += 1

        logger.info("Прогрев завершён: %d баров загружено", count)

    def get_warmup_bars(self, n: int) -> list[dict]:
        """Синхронный доступ к уже загруженному окну (после прогрева)."""
        return list(self._window)[-n:]

    # ─── Основной async-генератор ─────────────────────────────────────────────

    async def aiter_bars(self) -> AsyncIterator[list[dict]]:
        """
        Асинхронный генератор закрытых M1-свечей.

        Алгоритм:
          1. Прогрев через REST.
          2. Подписка на WebSocket стрим (waiting_close=True — только закрытые свечи).
          3. При каждой новой свече добавляем в окно, yield snapshot.
          4. При обрыве соединения — переподключение через RECONNECT_DELAY_SEC.
        """
        from tinkoff.invest import (
            AsyncClient,
            CandleInstrument,
            SubscriptionInterval,
        )

        while True:  # внешний цикл для переподключения
            try:
                async with AsyncClient(self._token) as client:
                    # Прогрев историческими данными
                    await self._load_warmup(client)

                    # Подписка на стрим закрытых свечей
                    stream = client.create_market_data_stream()
                    stream.candles.waiting_close().subscribe([
                        CandleInstrument(
                            instrument_id=self._figi,
                            interval=SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
                        )
                    ])

                    logger.info("Подключён к стриму M1-свечей figi=%s", self._figi)

                    async for market_data in stream:
                        candle = market_data.candle
                        if candle is None:
                            continue

                        bar = _candle_to_dict(candle, complete=True)

                        # Дедупликация: не добавляем если уже есть бар с тем же временем
                        if (self._window and
                                self._window[-1]["datetime"] == bar["datetime"]):
                            continue

                        self._window.append(bar)
                        logger.debug(
                            "Новый бар: %s  O=%.2f H=%.2f L=%.2f C=%.2f V=%d",
                            bar["datetime"], bar["open"], bar["high"],
                            bar["low"], bar["close"], bar["volume"],
                        )
                        yield list(self._window)

            except Exception as e:
                logger.error(
                    "Ошибка стрима (%s: %s). Переподключение через %ds...",
                    type(e).__name__, e, RECONNECT_DELAY_SEC,
                )
                await asyncio.sleep(RECONNECT_DELAY_SEC)
