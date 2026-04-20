"""
run_paper.py — точка входа для paper trading (sandbox T-Инвестиций).

Использование:
  python run_paper.py --balance 1000000

Требует в .env:
  BOT_TINKOFF_TOKEN=...
  BOT_TINKOFF_ACCOUNT_ID=...  (sandbox account ID)
"""

import argparse
import asyncio
import signal
import sys


def parse_args():
    p = argparse.ArgumentParser(description="Paper trading — sandbox T-Инвестиций")
    p.add_argument("--balance", type=float, default=1_000_000.0,
                   help="Виртуальный баланс для sandbox (руб)")
    return p.parse_args()


async def main():
    args = parse_args()

    from di import build_paper
    container = build_paper(initial_balance=args.balance)

    cfg = container.cfg
    if not cfg.tinkoff_token.get_secret_value():
        print("Ошибка: BOT_TINKOFF_TOKEN не задан в .env", file=sys.stderr)
        sys.exit(1)
    if not cfg.tinkoff_account_id:
        print("Ошибка: BOT_TINKOFF_ACCOUNT_ID не задан в .env", file=sys.stderr)
        sys.exit(1)

    print(f"\nPaper trading | figi={cfg.figi} | sandbox баланс={args.balance:,.0f} руб.")
    print("Нажмите Ctrl+C для остановки\n")

    # Graceful shutdown по Ctrl+C
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _shutdown():
        print("\nОстановка по сигналу...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    async with container.execution:
        engine_task = asyncio.create_task(container.engine.run())
        stop_task   = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {engine_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

    # Итоги
    stats = container.portfolio.session_stats()
    print("\n--- Итоги сессии ---")
    print(f"Сделок: {stats['total_trades']} | Win rate: {stats['winrate']:.1%} | PnL: {stats['net_pnl']:+,.0f} руб.")


if __name__ == "__main__":
    asyncio.run(main())
