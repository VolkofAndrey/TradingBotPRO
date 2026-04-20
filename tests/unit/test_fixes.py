"""
tests/unit/test_fixes.py — тесты для всех четырёх исправлений.
"""

import sys, os, asyncio, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest
from unittest.mock import AsyncMock, MagicMock


# ─── Проблема 1а: strategy_logic принимает явный cfg ─────────────────────────

class TestExplicitCfg:
    def test_generate_signal_accepts_cfg(self):
        from config.settings import get_settings
        import strategy_logic as sl
        from tests.fixtures.bar_factory import make_bars
        cfg  = get_settings("backtest")
        bars = make_bars([100.0] * 50)
        result = sl.generate_signal(bars, 500_000, cfg=cfg)
        assert "action" in result

    def test_generate_signal_fallback_no_cfg(self):
        """Без cfg — фолбэк на get_settings(), не падаем."""
        import strategy_logic as sl
        from tests.fixtures.bar_factory import make_bars
        bars = make_bars([100.0] * 50)
        result = sl.generate_signal(bars, 500_000)
        assert "action" in result

    def test_no_import_config_shim_needed(self):
        """config/__init__.py больше не содержит шима."""
        import config
        # Старый шим давал атрибуты вроде config.TICK_SIZE через __getattr__
        # Теперь это должно падать с AttributeError — шима нет
        with pytest.raises(AttributeError):
            _ = config.TICK_SIZE


# ─── Проблема 1б: EventBus логирует медленные обработчики ────────────────────

class TestEventBusTiming:
    def test_fast_handler_no_warning(self, caplog):
        from core.event_bus import EventBus
        from core.events import BarEvent
        import logging

        bus = EventBus(max_handler_ms=500)
        bus.subscribe(BarEvent, lambda e: None)   # мгновенный

        with caplog.at_level(logging.WARNING, logger="bot.event_bus"):
            bus.publish(BarEvent(figi="TEST"))

        slow_warnings = [r for r in caplog.records if "МЕДЛЕННЫЙ" in r.message]
        assert len(slow_warnings) == 0

    def test_slow_handler_logged(self, caplog):
        from core.event_bus import EventBus
        from core.events import BarEvent
        import logging

        bus = EventBus(max_handler_ms=10)  # лимит 10мс

        def slow_handler(e):
            time.sleep(0.05)  # 50мс — в 5× раз медленнее лимита

        bus.subscribe(BarEvent, slow_handler)

        with caplog.at_level(logging.WARNING, logger="bot.event_bus"):
            bus.publish(BarEvent(figi="TEST"))

        # Должно быть предупреждение о медленном обработчике
        slow_logs = [r for r in caplog.records
                     if "медленн" in r.message.lower() or "МЕДЛЕННЫЙ" in r.message]
        assert len(slow_logs) > 0

    def test_exception_in_handler_does_not_stop_others(self):
        from core.event_bus import EventBus
        from core.events import BarEvent

        bus     = EventBus()
        results = []

        bus.subscribe(BarEvent, lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.subscribe(BarEvent, lambda e: results.append("ok"))

        bus.publish(BarEvent(figi="TEST"))
        assert results == ["ok"]


# ─── Проблема 2: MultiInstrumentRunner ───────────────────────────────────────

class TestMultiInstrumentRunner:
    def test_backtest_sequential(self):
        from core.instrument_runner import MultiInstrumentRunner, InstrumentResult

        class FakeContainer:
            class engine:
                @staticmethod
                def run():
                    return {"total_trades": 5, "net_pnl": 1000.0,
                            "wins": 3, "losses": 2, "winrate": 0.6, "balance": 501000.0}

        runner = MultiInstrumentRunner()
        runner.add("FIGI1", FakeContainer())
        runner.add("FIGI2", FakeContainer())

        results = runner.run_all_backtest()
        assert len(results) == 2
        assert all(r.error is None for r in results)
        assert results[0].figi == "FIGI1"
        assert results[1].stats["total_trades"] == 5

    def test_backtest_error_isolation(self):
        from core.instrument_runner import MultiInstrumentRunner

        class GoodContainer:
            class engine:
                @staticmethod
                def run():
                    return {"total_trades": 1, "net_pnl": 0, "wins": 0,
                            "losses": 0, "winrate": 0, "balance": 0}

        class BrokenContainer:
            class engine:
                @staticmethod
                def run():
                    raise RuntimeError("симулируем краш")

        runner = MultiInstrumentRunner()
        runner.add("GOOD", GoodContainer())
        runner.add("BAD",  BrokenContainer())

        results = runner.run_all_backtest()
        assert len(results) == 2
        good = next(r for r in results if r.figi == "GOOD")
        bad  = next(r for r in results if r.figi == "BAD")
        assert good.error is None
        assert bad.error is not None


# ─── Проблема 3: order_poller ─────────────────────────────────────────────────

class TestOrderPoller:
    def _make_exec(self, statuses):
        """Создаёт мок ExecutionProvider с заданной последовательностью статусов."""
        from core.models import Order, OrderSide, OrderStatus

        mock = AsyncMock()
        mock.cancel_order = AsyncMock(return_value=True)

        call_count = [0]
        orders = []
        for status, price in statuses:
            o = Order(
                order_id="ord1", figi="TEST",
                side=OrderSide.BUY, quantity=2,
                status=status, fill_price=price if price else None,
            )
            orders.append(o)

        async def get_status(order_id):
            idx = min(call_count[0], len(orders) - 1)
            call_count[0] += 1
            return orders[idx]

        mock.get_order_status = get_status
        return mock

    @pytest.mark.asyncio
    async def test_immediate_fill(self):
        from execution.order_poller import wait_for_fill
        fill = await wait_for_fill("ord1", 2, 95000.0, None)
        assert fill.fill_price == 95000.0
        assert fill.quantity == 2

    @pytest.mark.asyncio
    async def test_delayed_fill(self):
        from execution.order_poller import wait_for_fill
        from core.models import OrderStatus

        exec_prov = self._make_exec([
            (OrderStatus.ACCEPTED, None),
            (OrderStatus.ACCEPTED, None),
            (OrderStatus.FILLED,   95000.0),
        ])

        fill = await wait_for_fill(
            "ord1", 2, 0.0, exec_prov,
            timeout_sec=5.0, interval_sec=0.01,
        )
        assert fill.fill_price == 95000.0

    @pytest.mark.asyncio
    async def test_timeout_cancels_order(self):
        from execution.order_poller import wait_for_fill, OrderNotFilledError
        from core.models import OrderStatus

        exec_prov = self._make_exec([
            (OrderStatus.ACCEPTED, None),
        ] * 100)

        with pytest.raises(OrderNotFilledError):
            await wait_for_fill(
                "ord1", 2, 0.0, exec_prov,
                timeout_sec=0.05, interval_sec=0.01,
            )
        exec_prov.cancel_order.assert_called_once_with("ord1")

    @pytest.mark.asyncio
    async def test_fill_price_zero_raises(self):
        from execution.order_poller import wait_for_fill, OrderNotFilledError
        from core.models import OrderStatus

        exec_prov = self._make_exec([
            (OrderStatus.FILLED, 0.0),   # FILLED но цена 0 — ошибка API
        ])

        with pytest.raises(OrderNotFilledError, match="fill_price=0"):
            await wait_for_fill(
                "ord1", 2, 0.0, exec_prov,
                timeout_sec=1.0, interval_sec=0.01,
            )


# ─── Проблема 4: SecretStr + валидация ───────────────────────────────────────

class TestSecurity:
    def test_token_is_secret_str(self):
        from config.settings import get_settings
        from pydantic import SecretStr
        cfg = get_settings("live")
        assert isinstance(cfg.tinkoff_token, SecretStr)

    def test_token_not_exposed_in_repr(self):
        from config.settings import Settings
        from pydantic import SecretStr
        s = Settings(tinkoff_token=SecretStr("supersecret123"))
        repr_str = repr(s)
        assert "supersecret123" not in repr_str
        assert "**" in repr_str or "secret" in repr_str.lower()

    def test_backtest_validates_zero_quantity(self):
        from config.settings import get_settings
        from execution.backtest import BacktestExecutionProvider
        from core.models import Direction
        cfg = get_settings("backtest")
        ep  = BacktestExecutionProvider(cfg, 500_000)
        with pytest.raises(ValueError, match="quantity"):
            ep.place_order_sync(Direction.LONG, 0, 95000.0)

    def test_backtest_validates_zero_price(self):
        from config.settings import get_settings
        from execution.backtest import BacktestExecutionProvider
        from core.models import Direction
        cfg = get_settings("backtest")
        ep  = BacktestExecutionProvider(cfg, 500_000)
        with pytest.raises(ValueError, match="price"):
            ep.place_order_sync(Direction.LONG, 1, 0.0)

    def test_retry_exponential_backoff(self):
        from execution.retry import _wait_time
        w1 = _wait_time(1)
        w2 = _wait_time(2)
        w3 = _wait_time(3)
        # Каждый следующий шаг должен быть больше предыдущего (с учётом jitter)
        # Проверяем базовые значения без jitter
        assert 0.5 <= w1 <= 2.0
        assert 1.0 <= w2 <= 4.0
        assert 2.5 <= w3 <= 7.0

    def test_retry_non_retryable_not_retried(self):
        import asyncio
        from execution.retry import with_retry

        call_count = [0]

        async def always_fails():
            call_count[0] += 1
            raise ValueError("не-retryable ошибка")

        with pytest.raises(ValueError):
            asyncio.get_event_loop().run_until_complete(
                with_retry(always_fails, max_retries=3)
            )
        # Должен был попробовать только 1 раз (ValueError не retryable)
        assert call_count[0] == 1
