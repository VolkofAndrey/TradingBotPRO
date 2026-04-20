"""
run_live.py — реальная торговля, один или несколько инструментов.

Один инструмент (FIGI из конфига):
  python run_live.py --confirm

Несколько инструментов:
  python run_live.py --confirm --figi FUTSI0625000 --figi FUTSNG0625000

ВНИМАНИЕ: торгует реальными деньгами.
"""

import argparse
import asyncio
import signal
import sys


def parse_args():
    p = argparse.ArgumentParser(description="Live trading — T-Инвестиции (РЕАЛЬНЫЕ ДЕНЬГИ)")
    p.add_argument("--figi", action="append", default=[],
                   help="FIGI инструмента (можно указать несколько раз)")
    p.add_argument("--balance-hint", type=float, default=0.0)
    p.add_argument("--confirm", action="store_true",
                   help="Подтвердить запуск реальной торговли (обязательный флаг)")
    return p.parse_args()


async def main():
    args = parse_args()

    if not args.confirm:
        print("⚠️  ВНИМАНИЕ: вы запускаете реальную торговлю.")
        print("Добавьте флаг --confirm для подтверждения.")
        sys.exit(1)

    from di import build_live, build_live_for
    from core.instrument_runner import MultiInstrumentRunner

    runner = MultiInstrumentRunner()

    if args.figi:
        # Несколько явно указанных инструментов
        for figi in args.figi:
            container = build_live_for(figi, args.balance_hint)
            cfg = container.cfg
            if not cfg.tinkoff_token.get_secret_value():
                print("Ошибка: BOT_TINKOFF_TOKEN не задан", file=sys.stderr)
                sys.exit(1)
            runner.add(figi, container)
            print(f"  + {figi}")
    else:
        # Один инструмент из конфига
        container = build_live(args.balance_hint)
        cfg = container.cfg
        if not cfg.tinkoff_token.get_secret_value():
            print("Ошибка: BOT_TINKOFF_TOKEN не задан", file=sys.stderr)
            sys.exit(1)
        runner.add(cfg.figi, container)
        print(f"  + {cfg.figi}")

    print(f"\nLIVE trading | Нажмите Ctrl+C для graceful stop\n")

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _shutdown():
        print("\nОстановка...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    run_task  = asyncio.create_task(runner.run_all_async())
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
