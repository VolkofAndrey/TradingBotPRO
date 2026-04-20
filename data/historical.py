"""
data/historical.py — провайдер исторических данных для бэктеста.

Источники (определяются расширением файла):
  .csv     — стандартный CSV с заголовком
  .parquet — Apache Parquet (быстрее, меньше места)

Формат CSV/Parquet (имена колонок гибкие, маппинг через COLUMN_ALIASES):
  datetime / time / ts / timestamp  → datetime
  open / o                          → open
  high / h                          → high
  low / l                           → low
  close / c                         → close
  volume / vol / v                  → volume

Синхронный. Никакого async, никаких сетевых вызовов.
iter_bars() отдаёт скользящее окно: на каждом шаге бэктест видит
ровно столько свечей, сколько было бы доступно в реальном времени.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from config.settings import Settings
from data.base import DataProvider

logger = logging.getLogger("bot.data.historical")

# Гибкий маппинг названий колонок
COLUMN_ALIASES: dict[str, list[str]] = {
    "datetime": ["datetime", "time", "ts", "timestamp", "date_time", "candle_time"],
    "open":     ["open", "o", "open_price"],
    "high":     ["high", "h", "high_price"],
    "low":      ["low",  "l", "low_price"],
    "close":    ["close","c", "close_price"],
    "volume":   ["volume","vol","v","qty"],
}


def _resolve_columns(header: list[str]) -> dict[str, str]:
    """Строит маппинг {canonical_name: actual_column_name}."""
    header_lower = {h.lower(): h for h in header}
    mapping: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in header_lower:
                mapping[canonical] = header_lower[alias]
                break
        if canonical not in mapping:
            raise ValueError(
                f"Колонка '{canonical}' не найдена в заголовке. "
                f"Доступные: {list(header_lower.keys())}. "
                f"Допустимые псевдонимы: {aliases}"
            )
    return mapping


def _parse_datetime(s: str) -> datetime:
    """Парсит datetime из строки, пробует несколько форматов."""
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
    ]
    s = s.strip()
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # ISO fallback
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"Не удалось распознать дату: {s!r}")


def _is_finam(fieldnames: list[str]) -> bool:
    """Определяет формат Finam по наличию полей <DATE>, <TIME>, <VOL>."""
    names = [f.strip("<>").upper() for f in fieldnames]
    return "DATE" in names and "TIME" in names and "VOL" in names


def _load_finam_csv(path: Path) -> list[dict]:
    """
    Загружает CSV в формате Finam (разделитель ';'):
      <TICKER>;<PER>;<DATE>;<TIME>;<OPEN>;<HIGH>;<LOW>;<CLOSE>;<VOL>
      Si;1;20260105;090000;80260;80475;80200;80424;4526
    """
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            try:
                date_s = row["<DATE>"].strip()          # 20260105
                time_s = row["<TIME>"].strip().zfill(6) # 090000
                dt = datetime.strptime(f"{date_s}{time_s}", "%Y%m%d%H%M%S")
                rows.append({
                    "datetime": dt,
                    "date":     dt.date(),
                    "time":     dt.time(),
                    "open":     float(row["<OPEN>"]),
                    "high":     float(row["<HIGH>"]),
                    "low":      float(row["<LOW>"]),
                    "close":    float(row["<CLOSE>"]),
                    "volume":   int(float(row["<VOL>"])),
                    "complete": True,
                })
            except (ValueError, KeyError) as e:
                logger.debug("Пропускаем строку Finam (%s): %s", e, row)
    return rows


def _load_csv(path: Path) -> list[dict]:
    # Определяем формат по первой строке
    with open(path, encoding="utf-8", newline="") as f:
        first_line = f.readline()
    delimiter = ";" if ";" in first_line else ","
    with open(path, encoding="utf-8", newline="") as f:
        probe = csv.DictReader(f, delimiter=delimiter)
        fieldnames = probe.fieldnames or []
    if _is_finam(fieldnames):
        logger.info("Определён формат Finam")
        return _load_finam_csv(path)
    # Стандартный CSV с автоопределением колонок
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"Пустой CSV: {path}")
        mapping = _resolve_columns(list(reader.fieldnames))
        for row in reader:
            try:
                dt = _parse_datetime(row[mapping["datetime"]])
                rows.append({
                    "datetime": dt,
                    "date":     dt.date(),
                    "time":     dt.time(),
                    "open":     float(row[mapping["open"]]),
                    "high":     float(row[mapping["high"]]),
                    "low":      float(row[mapping["low"]]),
                    "close":    float(row[mapping["close"]]),
                    "volume":   int(float(row[mapping["volume"]])),
                    "complete": True,
                })
            except (ValueError, KeyError) as e:
                logger.debug("Пропускаем строку CSV (%s): %s", e, row)
    return rows


def _load_parquet(path: Path) -> list[dict]:
    try:
        import pandas as pd
    except ImportError:
        raise ImportError(
            "pandas не установлен. Для Parquet: pip install pandas pyarrow"
        )
    df = pd.read_parquet(path)
    df.columns = [c.lower() for c in df.columns]

    # Находим колонку datetime
    dt_col = None
    for alias in COLUMN_ALIASES["datetime"]:
        if alias in df.columns:
            dt_col = alias
            break
    if dt_col is None:
        raise ValueError(f"Колонка datetime не найдена в parquet: {list(df.columns)}")

    rows = []
    for _, row in df.iterrows():
        dt = pd.Timestamp(row[dt_col]).to_pydatetime()
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        rows.append({
            "datetime": dt,
            "date":     dt.date(),
            "time":     dt.time(),
            "open":     float(row.get("open",  row.get("o", 0))),
            "high":     float(row.get("high",  row.get("h", 0))),
            "low":      float(row.get("low",   row.get("l", 0))),
            "close":    float(row.get("close", row.get("c", 0))),
            "volume":   int(float(row.get("volume", row.get("vol", row.get("v", 0))))),
            "complete": True,
        })
    return rows


class HistoricalDataProvider(DataProvider):
    """
    Провайдер для бэктеста. Загружает CSV или Parquet, итерирует бары.

    iter_bars() — генератор скользящего окна:
      на каждом шаге yield list[dict] с барами от начала до i-го включительно.
      Это точно воспроизводит то, что стратегия видела бы в реальном времени.

    warmup_bars — сколько баров нужно накопить перед первым yield
      (стратегии нужно ATR_PERIOD + запас для уровней, обычно 400+).
    """

    def __init__(self, cfg: Settings, data_path: str | Path, warmup_bars: int = 450) -> None:
        self._figi       = cfg.figi
        self._path       = Path(data_path)
        self._warmup     = warmup_bars
        self._bars: list[dict] = []
        self._loaded     = False

    def get_figi(self) -> str:
        return self._figi

    def _load(self) -> None:
        if self._loaded:
            return
        path = self._path
        logger.info("Загружаем исторические данные: %s", path)
        if not path.exists():
            raise FileNotFoundError(f"Файл данных не найден: {path}")

        suffix = path.suffix.lower()
        if suffix == ".csv":
            self._bars = _load_csv(path)
        elif suffix in (".parquet", ".pq"):
            self._bars = _load_parquet(path)
        else:
            raise ValueError(f"Неизвестный формат файла: {suffix}. Используйте .csv или .parquet")

        # Сортируем по времени — на случай если файл не отсортирован
        self._bars.sort(key=lambda b: b["datetime"])
        self._loaded = True
        logger.info(
            "Загружено %d баров: %s → %s",
            len(self._bars),
            self._bars[0]["datetime"] if self._bars else "?",
            self._bars[-1]["datetime"] if self._bars else "?",
        )

    def iter_bars(self) -> Iterator[list[dict]]:
        """
        Синхронный генератор скользящего окна.

        Первый yield происходит после накопления warmup_bars баров.
        Каждый следующий yield — после каждого нового бара.
        """
        self._load()
        bars = self._bars

        if len(bars) < self._warmup:
            logger.warning(
                "Баров меньше warmup (%d < %d) — бэктест может быть неточным",
                len(bars), self._warmup,
            )

        window: list[dict] = []
        for bar in bars:
            window.append(bar)
            if len(window) >= self._warmup:
                yield window[:]  # копия — стратегия не может изменить окно

    def get_warmup_bars(self, n: int) -> list[dict]:
        """Последние n загруженных баров (для прогрева live-стратегии)."""
        self._load()
        return self._bars[-n:]

    @property
    def total_bars(self) -> int:
        self._load()
        return len(self._bars)
