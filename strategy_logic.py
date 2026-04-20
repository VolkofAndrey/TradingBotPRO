"""
=============================================================
  strategy.py — логика стратегии Liquidity Sweep v3.6
=============================================================

Чистая функциональная логика — без I/O, без API-вызовов.
Принимает список свечей (list[dict]) и возвращает сигналы.
Полностью тестируется изолированно.

Candle dict:
  {datetime, date, time, open, high, low, close, volume, complete}
=============================================================
"""

import math
import logging
from datetime import datetime, timedelta
from typing import Optional
from config.settings import Settings


logger = logging.getLogger("bot.strategy")


# ─────────────────────────────────────────────
#  ТЕХНИЧЕСКИЕ ИНДИКАТОРЫ (без numpy/pandas)
# ─────────────────────────────────────────────

def compute_atr(candles: list[dict], period: int) -> float:
    """ATR(period) по последним свечам. Возвращает последнее значение."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h  = candles[i]["high"]
        l  = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    # Простое скользящее среднее последних `period` значений
    return sum(trs[-period:]) / period


def compute_volume_ma(candles: list[dict], period: int) -> float:
    if len(candles) < period:
        return 0.0
    vols = [c["volume"] for c in candles[-period:]]
    return sum(vols) / period


def compute_ma(candles: list[dict], period: int) -> float:
    """MA(period) по close."""
    if len(candles) < period:
        return 0.0
    return sum(c["close"] for c in candles[-period:]) / period


def compute_adx_di(candles: list[dict], period: int) -> tuple[float, float, float]:
    """
    Возвращает (ADX, DI+, DI-) по последним свечам.
    Упрощённый вариант (Wilder smoothing → SMA для достаточного числа баров).
    """
    if len(candles) < period * 2 + 1:
        return 0.0, 50.0, 50.0

    trs, dmp, dmm = [], [], []
    for i in range(1, len(candles)):
        h  = candles[i]["high"]
        l  = candles[i]["low"]
        pc = candles[i - 1]["close"]
        ph = candles[i - 1]["high"]
        pl = candles[i - 1]["low"]

        tr = max(h - l, abs(h - pc), abs(l - pc))
        up_move   = h  - ph
        down_move = pl - l
        dm_plus  = up_move   if up_move > down_move and up_move > 0   else 0
        dm_minus = down_move if down_move > up_move  and down_move > 0 else 0

        trs.append(tr)
        dmp.append(dm_plus)
        dmm.append(dm_minus)

    # Берём последние period значений
    tr_s  = sum(trs[-period:])
    dmp_s = sum(dmp[-period:])
    dmm_s = sum(dmm[-period:])

    if tr_s == 0:
        return 0.0, 50.0, 50.0

    di_plus  = 100 * dmp_s / tr_s
    di_minus = 100 * dmm_s / tr_s
    di_diff  = abs(di_plus - di_minus)
    di_sum   = di_plus + di_minus
    dx       = 100 * di_diff / di_sum if di_sum > 0 else 0.0

    # Упрощённый ADX = среднее DX за period предыдущих шагов
    # (полноценный Wilder потребует накопления, используем SMA как аппроксимацию)
    dxs = []
    step = max(1, len(trs) // period)
    for j in range(len(trs) - period, len(trs)):
        if j < 1:
            continue
        tr_j  = sum(trs[max(0, j - period):j]) or 1
        dmp_j = sum(dmp[max(0, j - period):j])
        dmm_j = sum(dmm[max(0, j - period):j])
        dip = 100 * dmp_j / tr_j
        dim = 100 * dmm_j / tr_j
        ds  = dip + dim
        dxs.append(100 * abs(dip - dim) / ds if ds > 0 else 0)

    adx = sum(dxs) / len(dxs) if dxs else dx
    return adx, di_plus, di_minus


def compute_psar(candles: list[dict], af_start: float, af_step: float, af_max: float,
                 state_ep: float, state_af: float, state_val: float, state_active: bool,
                 direction: str) -> tuple[float, float, float]:
    """
    Обновляет Parabolic SAR на одном новом баре.
    Возвращает (psar_val, ep, af).
    """
    bar = candles[-1]
    h   = bar["high"]
    l   = bar["low"]

    if not state_active or state_val is None:
        # Инициализация
        if direction == "short":
            ep  = h
            val = l - (h - l)
            af  = af_start
        else:
            ep  = l
            val = h + (h - l)
            af  = af_start
        return val, ep, af

    val = state_val
    ep  = state_ep
    af  = state_af

    if direction == "short":
        # SAR для шорта (выше рынка)
        val = val + af * (ep - val)
        if h < ep:
            ep = h
            af = min(af + af_step, af_max)
        # Правило: SAR не может быть ниже двух предыдущих High
        if len(candles) >= 3:
            val = max(val, candles[-2]["high"], candles[-3]["high"])
    else:
        # SAR для лонга (ниже рынка)
        val = val + af * (ep - val)
        if l > ep:
            ep = l
            af = min(af + af_step, af_max)
        if len(candles) >= 3:
            val = min(val, candles[-2]["low"], candles[-3]["low"])

    return val, ep, af


# ─────────────────────────────────────────────
#  УРОВНИ ЛИКВИДНОСТИ
# ─────────────────────────────────────────────

def _find_equal_levels(prices: list[float], tolerance: float) -> list[float]:
    result = []
    sorted_p = sorted(prices)
    i = 0
    while i < len(sorted_p):
        cluster = [sorted_p[i]]
        j = i + 1
        while j < len(sorted_p) and abs(sorted_p[j] - sorted_p[i]) <= tolerance:
            cluster.append(sorted_p[j])
            j += 1
        if len(cluster) >= 2:
            result.append(sum(cluster) / len(cluster))
        i = j
    return result


def find_liquidity_levels(candles: list[dict], cfg: Settings) -> list[dict]:
    """
    Находит уровни ликвидности по истории свечей.
    Возвращает список {"price": float, "priority": int, "type": str}.
    """
    lookback = cfg.level_lookback_bars
    window   = candles[-lookback:] if len(candles) > lookback else candles

    if len(window) < 10:
        return []

    levels   = []
    tick     = cfg.tick_size
    tol      = 3 * tick
    cur_date = candles[-1]["date"]

    # Вчерашние High/Low (высший приоритет)
    from datetime import timedelta
    for d_offset in range(1, 8):
        prev_date = cur_date - timedelta(days=d_offset)
        prev_bars = [c for c in window if c["date"] == prev_date]
        if prev_bars:
            levels.append({"price": max(c["high"]  for c in prev_bars), "priority": 1, "type": "prev_high"})
            levels.append({"price": min(c["low"]   for c in prev_bars), "priority": 1, "type": "prev_low"})
            break

    # Дневные High/Low (только если прошло ≥30 баров)
    today_bars = [c for c in window if c["date"] == cur_date]
    if len(today_bars) >= 30:
        levels.append({"price": max(c["high"] for c in today_bars), "priority": 2, "type": "day_high"})
        levels.append({"price": min(c["low"]  for c in today_bars), "priority": 2, "type": "day_low"})

    # Equal Highs / Equal Lows
    highs = [c["high"] for c in window]
    lows  = [c["low"]  for c in window]
    for lvl in _find_equal_levels(highs, tol):
        levels.append({"price": lvl, "priority": 2, "type": "equal_high"})
    for lvl in _find_equal_levels(lows, tol):
        levels.append({"price": lvl, "priority": 2, "type": "equal_low"})

    return levels


# ─────────────────────────────────────────────
#  H1 ADX / ТРЕНД-ФИЛЬТР
# ─────────────────────────────────────────────

def aggregate_h1(candles: list[dict]) -> list[dict]:
    """Агрегирует M1 → H1 бары."""
    h1_map = {}
    for c in candles:
        key = c["datetime"].replace(minute=0, second=0, microsecond=0)
        if key not in h1_map:
            h1_map[key] = {"datetime": key, "open": c["open"], "high": c["high"],
                           "low": c["low"], "close": c["close"], "volume": c["volume"]}
        else:
            h = h1_map[key]
            h["high"]   = max(h["high"],  c["high"])
            h["low"]    = min(h["low"],   c["low"])
            h["close"]  = c["close"]
            h["volume"] += c["volume"]
    return sorted(h1_map.values(), key=lambda x: x["datetime"])


def get_trend_context(candles: list[dict], cfg: Settings) -> tuple[float, str]:
    """
    Возвращает (ADX, trend_dir) по H1 барам.
    trend_dir: "bull" если DI+ > DI-, иначе "bear".
    """
    h1 = aggregate_h1(candles)
    if len(h1) < cfg.adx_period * 2:
        return 0.0, "bull"
    adx, di_plus, di_minus = compute_adx_di(h1, cfg.adx_period)
    trend_dir = "bull" if di_plus > di_minus else "bear"
    return adx, trend_dir


# ─────────────────────────────────────────────
#  ДЕТЕКТОР SWEEP
# ─────────────────────────────────────────────

def detect_sweep(candles: list[dict], level: dict, atr: float, cfg: Settings) -> Optional[dict]:
    """
    Проверяет последнюю закрытую свечу на признаки sweep.
    Возвращает sweep-dict или None.
    """
    if len(candles) < 6:
        return None

    bar         = candles[-1]
    level_price = level["price"]
    tick        = cfg.tick_size
    proximity   = cfg.level_proximity_ticks * tick

    prev_closes = [c["close"] for c in candles[-6:-1]]

    # ── Sweep вверх → шорт ──────────────────────────────────
    high_breakout    = bar["high"]  > level_price + proximity
    came_from_below  = sum(1 for c in prev_closes if c < level_price + proximity) >= 2
    open_below_level = bar["open"]  < level_price + proximity * 2

    if high_breakout and came_from_below and open_below_level:
        body    = abs(bar["close"] - bar["open"])
        min_body = cfg.sweep_body_atr_min * atr
        if min_body > 0 and body < min_body:
            return None
        return {
            "direction":      "short",
            "sweep_extreme":  bar["high"],
            "level_price":    level_price,
            "level_type":     level["type"],
            "level_priority": level["priority"],
            "bar_time":       bar["datetime"],
        }

    # ── Sweep вниз → лонг ───────────────────────────────────
    low_breakout    = bar["low"]  < level_price - proximity
    came_from_above = sum(1 for c in prev_closes if c > level_price - proximity) >= 2
    open_above_level = bar["open"] > level_price - proximity * 2

    if low_breakout and came_from_above and open_above_level:
        body     = abs(bar["close"] - bar["open"])
        min_body = cfg.sweep_body_atr_min * atr
        if min_body > 0 and body < min_body:
            return None
        return {
            "direction":      "long",
            "sweep_extreme":  bar["low"],
            "level_price":    level_price,
            "level_type":     level["type"],
            "level_priority": level["priority"],
            "bar_time":       bar["datetime"],
        }

    return None


# ─────────────────────────────────────────────
#  ДЕТЕКТОР ВОЗВРАТА
# ─────────────────────────────────────────────

def detect_return(candles: list[dict], sweep: dict, cfg: Settings) -> Optional[dict]:
    """
    Проверяет: произошёл ли возврат за уровень после sweep.
    Смотрит только на последние RETURN_MAX_CANDLES свечей.
    """
    level    = sweep["level_price"]
    min_ret  = cfg.return_min_ticks * cfg.tick_size
    max_bars = cfg.return_max_candles

    # Ищем возврат в последних max_bars свечах, начиная со свечи после sweep
    recent = candles[-max_bars:]

    for c in recent:
        if sweep["direction"] == "short":
            if c["close"] < level - min_ret:
                return {"return_close": c["close"], "return_time": c["datetime"]}
        else:
            if c["close"] > level + min_ret:
                return {"return_close": c["close"], "return_time": c["datetime"]}

    return None


# ─────────────────────────────────────────────
#  РАСЧЁТ ПАРАМЕТРОВ СДЕЛКИ
# ─────────────────────────────────────────────

def calculate_trade_params(sweep: dict, atr: float, cfg: Settings) -> Optional[dict]:
    """
    Рассчитывает вход, стоп, тейки, R:R.
    Возвращает параметры или None если условия не выполнены.

    Геометрия:
      SHORT-sweep (цена снесла уровень вверх, возврат вниз):
        entry      = level - 2 ticks          (чуть ниже уровня, ждём возврата под него)
        stop       = sweep_extreme + buffer    (выше максимума свипа)
        take1/take2 = entry - R * N            (вниз — в сторону прибыли)

      LONG-sweep (цена снесла уровень вниз, возврат вверх):
        entry      = level + 2 ticks          (чуть выше уровня, ждём возврата над ним)
        stop       = sweep_extreme - buffer    (ниже минимума свипа)
        take1/take2 = entry + R * N            (вверх — в сторону прибыли)

    Инварианты:
      SHORT: stop > entry  (стоп выше входа)  ✓
      LONG:  stop < entry  (стоп ниже входа)  ✓
    """
    tick      = cfg.tick_size
    direction = sweep["direction"]
    level     = sweep["level_price"]

    if direction == "short":
        # Вход чуть ниже уровня, стоп выше sweep_extreme
        entry      = level - 2 * tick
        stop       = sweep["sweep_extreme"] + cfg.stop_buffer_ticks * tick
        if stop <= entry:        # стоп должен быть ВЫШЕ входа для шорта
            logger.debug(f"calculate_trade_params [short]: stop({stop}) <= entry({entry}), пропускаем")
            return None
        risk_ticks = stop - entry

    else:  # long
        # Вход чуть выше уровня, стоп ниже sweep_extreme
        entry      = level + 2 * tick
        stop       = sweep["sweep_extreme"] - cfg.stop_buffer_ticks * tick
        if stop >= entry:        # стоп должен быть НИЖЕ входа для лонга
            logger.debug(f"calculate_trade_params [long]: stop({stop}) >= entry({entry}), пропускаем")
            return None
        risk_ticks = entry - stop

    if risk_ticks <= 0:
        return None

    # Проверка: стоп не больше STOP_MAX_ATR_MULT × ATR
    if atr > 0 and risk_ticks > cfg.stop_max_atr_mult * atr:
        logger.debug(f"calculate_trade_params: risk_ticks({risk_ticks:.1f}) > {cfg.stop_max_atr_mult}×ATR({atr:.1f})")
        return None

    # Тейки в сторону прибыли
    if direction == "short":
        take1 = entry - risk_ticks * cfg.take1_r_mult   # ниже для шорта ✓
        take2 = entry - atr * cfg.take2_atr_mult        # ниже для шорта ✓
    else:
        take1 = entry + risk_ticks * cfg.take1_r_mult   # выше для лонга ✓
        take2 = entry + atr * cfg.take2_atr_mult        # выше для лонга ✓

    rr = abs(take2 - entry) / risk_ticks if cfg.take2_mode != "trail" else 999.0
    if cfg.take2_mode not in ("trail", "psar") and rr < cfg.min_rr:
        return None

    # Финальная санитарная проверка направлений
    if direction == "long":
        assert stop  < entry, f"LONG: stop({stop}) должен быть < entry({entry})"
        assert take1 > entry, f"LONG: take1({take1}) должен быть > entry({entry})"
    else:
        assert stop  > entry, f"SHORT: stop({stop}) должен быть > entry({entry})"
        assert take1 < entry, f"SHORT: take1({take1}) должен быть < entry({entry})"

    return {
        "entry":      entry,
        "stop":       stop,
        "take1":      take1,
        "take2":      take2,
        "risk_ticks": risk_ticks,
        "direction":  direction,
        "rr":         rr,
    }


# ─────────────────────────────────────────────
#  РАЗМЕР ПОЗИЦИИ
# ─────────────────────────────────────────────

def calculate_position_size(available_balance: float, risk_ticks: float,
                             go_per_contract: float = 0.0,
                             cfg: Settings = None) -> int:
    """
    Рассчитывает количество контрактов.

    Параметры:
      available_balance — реальный свободный остаток из API (не виртуальный capital)
      risk_ticks        — риск сделки в пунктах (стоп - вход)
      go_per_contract   — актуальное ГО из API (0 = использовать cfg.go_per_contract)

    Два независимых ограничителя:
      contracts_risk   = floor(balance * RISK_PER_TRADE / risk_ticks)  — риск-сайзинг
      contracts_margin = floor(balance * MAX_MARGIN_USAGE / go)        — лимит ГО

    Итог: min(contracts_risk, contracts_margin, MAX_CONTRACTS)

    Важно: это расчёт «желаемого» размера. Финальное clamp под реальный баланс
    делается в execution.open_position() непосредственно перед ордером.
    """
    if cfg is None:
        from config.settings import get_settings
        cfg = get_settings()
    if risk_ticks <= 0 or available_balance <= 0:
        return 0

    # ── Риск-сайзинг ──────────────────────────────────────
    risk_rub        = available_balance * cfg.risk_per_trade
    contracts_risk  = int(risk_rub / (risk_ticks * cfg.tick_value))
    contracts_risk  = max(1, contracts_risk)

    # ── Лимит по ГО ───────────────────────────────────────
    go = go_per_contract if go_per_contract > 0 else cfg.go_per_contract
    if cfg.max_margin_usage > 0 and go > 0:
        margin_limit    = available_balance * cfg.max_margin_usage
        contracts_margin = int(margin_limit / go)
        contracts_risk  = min(contracts_risk, max(1, contracts_margin))

    # ── Абсолютный потолок ────────────────────────────────
    if cfg.max_contracts:
        contracts_risk = min(contracts_risk, cfg.max_contracts)

    return contracts_risk


# ─────────────────────────────────────────────
#  ГЛАВНАЯ ТОЧКА ВХОДА — генерация сигнала
# ─────────────────────────────────────────────

def generate_signal(candles: list[dict], available_balance: float,
                    active_sweep: Optional[dict] = None,
                    go_per_contract: float = 0.0,
                    cfg: Settings = None) -> dict:
    """
    Анализирует текущий набор свечей и генерирует сигнал.

    available_balance — реальный свободный остаток из API (НЕ виртуальный capital)
    go_per_contract   — актуальное ГО из API (0 = fallback на config)

    Возвращает:
      {
        "action": "open_long" | "open_short" | "wait" | "no_signal" | "pending_sweep",
        "params": {...}    # параметры сделки (если action != wait/no_signal)
        "reason": str      # причина для логов
      }
    """
    if cfg is None:
        from config.settings import get_settings
        cfg = get_settings()
    if len(candles) < cfg.atr_period + 5:
        return {"action": "no_signal", "reason": "недостаточно свечей"}

    # Только закрытые свечи для анализа
    closed = [c for c in candles if c.get("complete", True)]
    if len(closed) < cfg.atr_period + 5:
        return {"action": "no_signal", "reason": "недостаточно закрытых свечей"}

    last_bar = closed[-1]

    # ── Торговое окно ─────────────────────────────────────
    now_time = last_bar["time"]
    in_session = False
    if cfg.session_a_start_t <= now_time <= cfg.session_a_end_t:
        in_session = True
    if (cfg.session_b_start_t and cfg.session_b_end_t and
            cfg.session_b_start_t <= now_time <= cfg.session_b_end_t):
        in_session = True

    if not in_session:
        return {"action": "wait", "reason": f"вне торгового окна ({now_time})"}

    # ── Индикаторы ────────────────────────────────────────
    atr     = compute_atr(closed, cfg.atr_period)
    atr_f   = compute_atr(closed, cfg.atr_fast_period)
    vol_ma  = compute_volume_ma(closed, cfg.volume_ma_period)
    ma10    = compute_ma(closed, 10)

    if atr <= 0:
        return {"action": "no_signal", "reason": "ATR = 0"}

    # ── Фильтр перегрева ──────────────────────────────────
    if cfg.overextend_atr_mult > 0:
        dist = abs(last_bar["close"] - ma10)
        if dist > cfg.overextend_atr_mult * atr:
            return {"action": "wait", "reason": "цена перегрета (далеко от MA10)"}

    # ── Объём ─────────────────────────────────────────────
    if vol_ma > 0 and last_bar["volume"] < vol_ma * cfg.volume_multiplier:
        return {"action": "wait", "reason": "объём ниже нормы"}

    # ── ADX / тренд ───────────────────────────────────────
    adx, trend_dir = get_trend_context(closed, cfg)

    if adx >= cfg.adx_trend_min:
        market_mode = "strong_trend"
    elif adx >= cfg.adx_weak_min:
        market_mode = "weak_trend"
    else:
        market_mode = "range"

    # ── Уровни ────────────────────────────────────────────
    levels = find_liquidity_levels(closed, cfg)
    if not levels:
        return {"action": "no_signal", "reason": "нет уровней ликвидности"}

    # ── Поиск sweep (если ещё не нашли) ──────────────────
    if active_sweep is None:
        found_sweep = None
        for level in levels:
            # Фильтр по режиму рынка
            if market_mode == "strong_trend":
                # Только уровни с приоритетом 1 (prev_)
                if level["priority"] > 1:
                    continue

            sweep = detect_sweep(closed, level, atr, cfg)
            if sweep:
                # Фильтр по направлению тренда
                if cfg.with_trend_only:
                    if trend_dir == "bull" and sweep["direction"] == "short":
                        continue
                    if trend_dir == "bear" and sweep["direction"] == "long":
                        continue
                found_sweep = sweep
                break

        if not found_sweep:
            return {"action": "no_signal", "reason": "нет sweep"}

        # Возвращаем "pending_sweep" — нужно ждать возврата
        return {
            "action": "pending_sweep",
            "sweep":  found_sweep,
            "reason": f"sweep {found_sweep['direction']} на {found_sweep['level_price']}"
        }

    # ── Проверка возврата (sweep уже есть) ───────────────
    ret = detect_return(closed, active_sweep, cfg)
    if not ret:
        return {"action": "wait", "reason": "ожидаем возврат после sweep"}

    # ── Параметры сделки ──────────────────────────────────
    params = calculate_trade_params(active_sweep, atr, cfg)
    if not params:
        return {"action": "no_signal", "reason": "параметры сделки не прошли валидацию"}

    # ── Размер позиции ────────────────────────────────────
    contracts = calculate_position_size(available_balance, params["risk_ticks"], go_per_contract, cfg)
    if contracts <= 0:
        return {"action": "no_signal", "reason": "нулевой размер позиции"}

    # ── Это сильная сделка? ───────────────────────────────
    is_strong = (
        cfg.with_trend_only and
        ((trend_dir == "bull" and params["direction"] == "long") or
         (trend_dir == "bear" and params["direction"] == "short"))
    )

    timeout_time = (last_bar["datetime"] + timedelta(minutes=cfg.position_timeout_min)
                    ).strftime("%H:%M")

    action = "open_long" if params["direction"] == "long" else "open_short"
    return {
        "action":         action,
        "params":         params,
        "contracts":      contracts,
        "sweep":          active_sweep,
        "is_strong_trade": is_strong,
        "timeout_time":   timeout_time,
        "atr":            atr,
        "atr_fast":       atr_f,
        "reason":         f"{action} {contracts} конт. вход={params['entry']} стоп={params['stop']}",
    }


# ─────────────────────────────────────────────
#  УПРАВЛЕНИЕ ОТКРЫТОЙ ПОЗИЦИЕЙ
# ─────────────────────────────────────────────

def manage_position(candles: list[dict], position, cfg: Settings = None) -> dict:
    """
    Анализирует открытую позицию и возвращает действие.

    Возвращает:
      {"action": "hold" | "close_stop" | "close_take1" | "close_take2" |
                 "close_timeout" | "close_session" | "update_stop",
       "reason": str,
       "new_stop": float (если update_stop),
       "pnl_approx": float (приблизительный PnL)}
    """
    if cfg is None:
        from config.settings import get_settings
        cfg = get_settings()
    if not candles:
        return {"action": "hold", "reason": "нет данных"}

    last  = candles[-1]
    price = last["close"]
    now_t = last["time"]
    p     = position

    # ── Принудительное закрытие сессии ───────────────────
    close_time = cfg.strong_trade_close_time_t if (
        p.is_strong_trade and cfg.strong_trade_session_end
    ) else cfg.hard_close_time_t

    if now_t >= close_time:
        pnl = _calc_pnl(p, price, cfg)
        return {"action": "close_session", "reason": f"конец сессии ({close_time})",
                "pnl_approx": pnl}

    # ── Проверка стопа ────────────────────────────────────
    # SHORT: стоп ВЫШЕ входа → цена касается high
    # LONG:  стоп НИЖЕ входа → цена касается low
    if p.direction == "short":
        hit_stop = last["high"] >= p.current_stop
    else:
        hit_stop = last["low"] <= p.current_stop

    if hit_stop:
        pnl = _calc_pnl(p, p.current_stop, cfg)
        return {"action": "close_stop", "reason": "стоп",
                "exit_price": p.current_stop, "pnl_approx": pnl}

    # ── Проверка Take1 (если ещё не взят) ─────────────────
    # SHORT: take1 НИЖЕ входа → цена касается low
    # LONG:  take1 ВЫШЕ входа → цена касается high
    if not p.take1_hit:
        if p.direction == "short":
            hit_t1 = last["low"] <= p.take1
        else:
            hit_t1 = last["high"] >= p.take1

        if hit_t1:
            pnl_part = _calc_pnl_partial(p, p.take1)
            return {"action": "close_take1", "reason": "take1", "pnl_approx": pnl_part}

    # ── Take2 / трейлинг / PSAR (после Take1) ─────────────
    if p.take1_hit:
        # Таймаут позиции
        if now_t >= p.timeout_time:
            pnl = _calc_pnl(p, price, cfg)
            return {"action": "close_timeout", "reason": "таймаут",
                    "pnl_approx": pnl}

        if cfg.take2_mode == "fixed":
            if p.direction == "short":
                hit_t2 = last["low"] <= p.take2
            else:
                hit_t2 = last["high"] >= p.take2
            if hit_t2:
                pnl = _calc_pnl(p, p.take2, cfg)
                return {"action": "close_take2", "reason": "take2 fixed", "pnl_approx": pnl}

        elif cfg.take2_mode == "trail":
            atr_f = compute_atr(candles, cfg.atr_fast_period)
            if p.direction == "short":
                trail = price + atr_f
                if p.trail_stop is None or trail < p.trail_stop:
                    new_stop = trail
                    return {"action": "update_stop", "reason": "trail stop обновлён",
                            "new_stop": new_stop}
                if last["high"] >= p.trail_stop:
                    pnl = _calc_pnl(p, p.trail_stop, cfg)
                    return {"action": "close_stop", "reason": "trail stop",
                            "exit_price": p.trail_stop, "pnl_approx": pnl}
            else:
                trail = price - atr_f
                if p.trail_stop is None or trail > p.trail_stop:
                    return {"action": "update_stop", "reason": "trail stop обновлён",
                            "new_stop": trail}
                if last["low"] <= p.trail_stop:
                    pnl = _calc_pnl(p, p.trail_stop, cfg)
                    return {"action": "close_stop", "reason": "trail stop",
                            "exit_price": p.trail_stop, "pnl_approx": pnl}

        elif cfg.take2_mode == "psar":
            # Проверяем активацию PSAR
            dist_from_entry = abs(price - p.entry_price) / (p.entry_price - p.stop + 0.001)
            psar_should_activate = (
                p.psar_active or
                cfg.psar_activation_r <= 0 or
                dist_from_entry >= cfg.psar_activation_r
            )

            if psar_should_activate:
                psar_val, ep, af = compute_psar(
                    candles,
                    cfg.psar_af_start, cfg.psar_af_step, cfg.psar_af_max,
                    p.psar_ep, p.psar_af, p.psar_val, p.psar_active,
                    p.direction
                )
                # Проверяем триггер PSAR
                if p.direction == "short" and last["high"] >= psar_val:
                    pnl = _calc_pnl(p, psar_val, cfg)
                    return {"action": "close_stop", "reason": "psar",
                            "exit_price": psar_val, "pnl_approx": pnl,
                            "psar_update": (True, ep, af, psar_val)}
                if p.direction == "long" and last["low"] <= psar_val:
                    pnl = _calc_pnl(p, psar_val, cfg)
                    return {"action": "close_stop", "reason": "psar",
                            "exit_price": psar_val, "pnl_approx": pnl,
                            "psar_update": (True, ep, af, psar_val)}
                return {"action": "update_stop", "reason": "psar обновлён",
                        "new_stop": psar_val,
                        "psar_update": (True, ep, af, psar_val)}

    return {"action": "hold", "reason": "в позиции"}


def _calc_pnl(position, exit_price: float, cfg: Settings = None) -> float:
    """
    Примерный PnL для оставшихся контрактов.
    Используется только для логов и approx-оценок — не для записи в CSV.
    Реальный PnL берётся из executed_price ордера в main.py.
    """
    n    = position.contracts_remaining
    if cfg is None:
        from config.settings import get_settings
        cfg = get_settings()
    tick = cfg.tick_value
    if position.direction == "short":
        pnl = (position.entry_price - exit_price) * n * tick
    else:
        pnl = (exit_price - position.entry_price) * n * tick

    # Sanity check: при стопе PnL должен быть <= 0, при тейке >= 0
    # Если знак нарушен — это сигнал об инвертированных параметрах
    if exit_price == position.current_stop:
        if position.direction == "long" and pnl > 0.1:
            logger.warning(
                f"_calc_pnl [LONG stop]: pnl={pnl:+.0f} > 0 — "
                f"стоп({position.current_stop}) выше входа({position.entry_price})?"
            )
        elif position.direction == "short" and pnl > 0.1:
            logger.warning(
                f"_calc_pnl [SHORT stop]: pnl={pnl:+.0f} > 0 — "
                f"стоп({position.current_stop}) ниже входа({position.entry_price})?"
            )
    return pnl


def _calc_pnl_partial(position, exit_price: float, cfg: Settings = None) -> float:
    """
    Примерный PnL для половины позиции (Take1).
    Только для логов — реальный PnL считается в main.py из executed_price.
    """
    if cfg is None:
        from config.settings import get_settings
        cfg = get_settings()
    half = max(1, position.contracts_total // 2)
    tick = cfg.tick_value
    if position.direction == "short":
        pnl = (position.entry_price - exit_price) * half * tick
    else:
        pnl = (exit_price - position.entry_price) * half * tick

    if position.direction == "long" and exit_price < position.entry_price:
        logger.warning(
            f"_calc_pnl_partial [LONG take1]: take1({exit_price}) < entry({position.entry_price}) "
            f"— инвертированный параметр!"
        )
    elif position.direction == "short" and exit_price > position.entry_price:
        logger.warning(
            f"_calc_pnl_partial [SHORT take1]: take1({exit_price}) > entry({position.entry_price}) "
            f"— инвертированный параметр!"
        )
    return pnl
