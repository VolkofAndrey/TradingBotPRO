"""
run_backtest.py — точка входа для бэктеста.

Использование:
  python run_backtest.py --data data/si_m1_2024.csv --balance 500000

Аргументы:
  --data      путь к CSV или Parquet файлу с M1-свечами (обязательный)
  --balance   начальный баланс в рублях (по умолчанию 500 000)
  --warmup    количество баров для прогрева (по умолчанию 450)
  --slippage  слипидж в тиках (по умолчанию 1)
  --output    файл для сохранения результатов CSV (опционально)
"""

import argparse
import csv
import json
import sys
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(description="Backtest Liquidity Sweep Strategy")
    p.add_argument("--data",     required=True,         help="Путь к CSV/Parquet с M1-свечами")
    p.add_argument("--balance",  type=float, default=500_000.0, help="Начальный баланс (руб)")
    p.add_argument("--warmup",   type=int,   default=450,       help="Баров для прогрева")
    p.add_argument("--slippage", type=int,   default=1,         help="Слипидж в тиках")
    p.add_argument("--output",   default=None,          help="Куда сохранить trades.csv")
    return p.parse_args()


def main():
    args = parse_args()

    data_path = Path(args.data)
    if not data_path.exists():
        print(f"Ошибка: файл не найден: {data_path}", file=sys.stderr)
        sys.exit(1)

    # Сборка через DI
    from di import build_backtest
    container = build_backtest(
        data_path       = data_path,
        initial_balance = args.balance,
        warmup_bars     = args.warmup,
        slippage_ticks  = args.slippage,
    )

    # Запуск
    print(f"\nБэктест запущен | figi={container.cfg.figi} | баланс={args.balance:,.0f} руб.")
    print(f"Данные: {data_path} | прогрев: {args.warmup} баров\n")

    stats = container.engine.run()

    # Вывод результатов
    print("\n" + "=" * 50)
    print("РЕЗУЛЬТАТЫ БЭКТЕСТА")
    print("=" * 50)
    print(f"  Всего сделок:       {stats['total_trades']}")
    print(f"  Прибыльных:         {stats['wins']}")
    print(f"  Убыточных:          {stats['losses']}")
    print(f"  Win rate:           {stats['winrate']:.1%}")
    print(f"  Чистый PnL:         {stats['net_pnl']:+,.0f} руб.")
    print(f"  Итоговый баланс:    {stats['balance']:,.0f} руб.")
    print(f"  Доходность:         {(stats['balance'] - args.balance) / args.balance:+.1%}")
    print(f"  Обработано баров:   {stats.get('bars_processed', '?')}")
    print("=" * 50)

    # Сохранение сделок
    if args.output:
        _save_trades(container.portfolio.trades, args.output)
        print(f"\nСделки сохранены: {args.output}")

    return 0


def _save_trades(trades, output_path: str):
    if not trades:
        return
    fields = ["opened_at", "closed_at", "direction", "entry_price",
              "exit_price", "contracts", "pnl", "commission", "close_reason"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            w.writerow({
                "opened_at":   t.opened_at,
                "closed_at":   t.closed_at,
                "direction":   t.direction.value,
                "entry_price": t.entry_price,
                "exit_price":  t.exit_price,
                "contracts":   t.contracts,
                "pnl":         round(t.pnl, 2),
                "commission":  round(t.commission, 2),
                "close_reason": t.close_reason,
            })


if __name__ == "__main__":
    sys.exit(main())
