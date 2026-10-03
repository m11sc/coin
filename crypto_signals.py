#!/usr/bin/env python3
"""
Торговые сигналы по криптовалюте с рекомендациями: когда покупать и когда выходить.

Как работает стратегия (сигналы — по закрытым свечам):
  ЛОНГ (покупка), если цена выше EMA 200 (тренд вверх) и:
    - EMA 9 пересекла EMA 21 снизу вверх ИЛИ RSI вышел из перепроданности (<30)
  ШОРТ (продажа), если цена ниже EMA 200 (тренд вниз) и:
    - EMA 9 пересекла EMA 21 сверху вниз ИЛИ RSI вышел из перекупленности (>70)
  Уровни по ATR (средний размах свечи):
    - стоп-лосс на 1.5 × ATR против сделки, цель на 3 × ATR по сделке (риск/прибыль 1:2)
  ВЫХОД (бот сам следит за сделкой и пишет):
    - стоп или цель — проверяются при каждом запуске по текущей цене, не дожидаясь закрытия свечи
    - разворот (сигнал в обратную сторону) — по закрытой свече
  Всплеск объёма отмечается в сообщении как подтверждение.

Плечо (LEVERAGE, например 20):
  - в сигнале: цена ликвидации, результат к марже на стопе и цели (с комиссиями)
  - если задан DEPOSIT: сколько маржи ставить, чтобы стоп стоил RISK_PCT % депозита
  - сигналы, где стоп слишком близко к ликвидации, пропускаются

Режимы:
  - на своём компьютере: цикл, проверка каждые CHECK_EVERY секунд
  - в GitHub Actions (RUN_ONCE=1): одна проверка и выход

Биржи (EXCHANGE): binance (пары BTCUSDT) или coinbase (пары BTC-USD).

Это сигналы механической стратегии, а не финансовый совет.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------- Настройки ----------
EXCHANGE = (os.getenv("EXCHANGE") or "binance").lower()
DEFAULT_SYMBOLS = {
    "binance": "BTCUSDT,ETHUSDT,SOLUSDT",
    "coinbase": "BTC-USD,ETH-USD,SOL-USD",
}
INTERVAL = os.getenv("INTERVAL") or "1h"             # binance: 1m,5m,15m,1h,4h,1d; coinbase: 1m,5m,15m,1h,6h,1d
CHECK_EVERY = int(os.getenv("CHECK_EVERY") or 60)
RUN_ONCE = os.getenv("RUN_ONCE") == "1"
# SYMBOLS=ALL — все пары к доллару, у которых оборот за сутки не меньше MIN_VOLUME_USD
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD") or 5_000_000)
STABLECOINS = {"USDT", "USDC", "DAI", "PYUSD", "FDUSD", "TUSD", "USDP", "GUSD", "EURC", "USDS", "USD1", "RLUSD"}

EMA_FAST = 9
EMA_SLOW = 21
TREND_EMA = 200     # фильтр тренда
RSI_PERIOD = 14
RSI_LOW = 30
RSI_HIGH = 70
VOL_WINDOW = 20
VOL_MULT = 2.5
ATR_PERIOD = 14
STOP_ATR = 1.5      # стоп-лосс = вход − STOP_ATR × ATR
TAKE_ATR = 3.0      # цель      = вход + TAKE_ATR × ATR

# Торговля с плечом (фьючерсы). LEVERAGE=1 — обычная покупка на споте.
LEVERAGE = float(os.getenv("LEVERAGE") or 1)
DEPOSIT = float(os.getenv("DEPOSIT") or 0)           # депозит в $, чтобы бот считал размер маржи (0 — не считать)
RISK_PCT = float(os.getenv("RISK_PCT") or 1)         # сколько % депозита готов потерять, если сработает стоп
FEE = 0.0005        # комиссия биржи за вход или выход (taker 0.05%), от объёма позиции
MMR = 0.005         # поддерживающая маржа (0.5%) — нужна для расчёта цены ликвидации
MAX_STOP_OF_LIQ = 0.7  # стоп должен быть не дальше 70% пути до ликвидации, иначе сигнал пропускаем
ALLOW_SHORT = (os.getenv("ALLOW_SHORT") or "1") == "1"  # шорт-сигналы при тренде вниз (0 — только лонги)

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

STATE_FILE = Path(__file__).with_name("signals_state.json")
# История сделок для статистики (Mini App в Telegram читает этот файл с GitHub Pages)
TRADES_FILE = Path(os.getenv("TRADES_FILE") or Path(__file__).parent / "docs" / "data" / "trades.json")
MAX_CLOSED = 2000   # сколько закрытых сделок хранить в истории
# --------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("signals")

COINBASE_GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}


# ---------- Данные с бирж ----------
def fetch_binance(symbol, interval):
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": 500},
        timeout=10,
    )
    r.raise_for_status()
    return [
        {"time": k[0], "close_time": k[6], "high": float(k[2]), "low": float(k[3]),
         "close": float(k[4]), "volume": float(k[5])}
        for k in r.json()
    ]


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def fetch_coinbase(symbol, interval, pages=2):
    """Coinbase отдаёт до 300 свечей за запрос, берём 2 страницы (нужно для EMA 200)."""
    gran = COINBASE_GRANULARITY[interval]
    end = int(time.time()) // gran * gran + gran
    rows = {}
    for _ in range(pages):
        start = end - 299 * gran
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/candles",
            params={"granularity": gran, "start": _iso(start), "end": _iso(end)},
            headers={"User-Agent": "crypto-signals"},
            timeout=10,
        )
        r.raise_for_status()
        for k in r.json():  # [time, low, high, open, close, volume]
            rows[k[0]] = k
        end = start
    return [
        {"time": k[0] * 1000, "close_time": (k[0] + gran) * 1000, "high": float(k[2]),
         "low": float(k[1]), "close": float(k[4]), "volume": float(k[5])}
        for t, k in sorted(rows.items())
    ]


FETCHERS = {"binance": fetch_binance, "coinbase": fetch_coinbase}


def all_symbols():
    """Все пары к доллару на бирже (без стейблкоинов). Для Binance сразу фильтр по обороту."""
    if EXCHANGE == "coinbase":
        r = requests.get("https://api.exchange.coinbase.com/products",
                         headers={"User-Agent": "crypto-signals"}, timeout=15)
        r.raise_for_status()
        return sorted(
            p["id"] for p in r.json()
            if p.get("quote_currency") == "USD" and p.get("status") == "online"
            and not p.get("trading_disabled") and p.get("base_currency") not in STABLECOINS
        )
    r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
    r.raise_for_status()
    rows = [
        t for t in r.json()
        if t["symbol"].endswith("USDT") and t["symbol"][:-4] not in STABLECOINS
        and float(t["quoteVolume"]) >= MIN_VOLUME_USD
    ]
    return [t["symbol"] for t in sorted(rows, key=lambda t: -float(t["quoteVolume"]))]


def daily_volume_usd(candles):
    """Примерный оборот за последние сутки в $ по свечам."""
    step_ms = candles[-1]["time"] - candles[-2]["time"]
    n = max(1, round(86_400_000 / step_ms))
    return sum(c["close"] * c["volume"] for c in candles[-n:])


# ---------- Индикаторы ----------
def ema(values, period):
    """EMA; первое значение — простое среднее первых period свечей, до него None."""
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values, period=14):
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def calc(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

    out[period] = calc(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = calc(avg_g, avg_l)
    return out


def atr(highs, lows, closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    tr = [highs[i] - lows[i] if i == 0 else
          max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
          for i in range(len(closes))]
    prev = sum(tr[1:period + 1]) / period
    out[period] = prev
    for i in range(period + 1, len(closes)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def indicators(candles):
    """Все индикаторы сразу для всего списка свечей (значение на свече i зависит только от свечей до i)."""
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    return {
        "closes": closes,
        "vols": [c["volume"] for c in candles],
        "ef": ema(closes, EMA_FAST), "es": ema(closes, EMA_SLOW), "et": ema(closes, TREND_EMA),
        "rsi": rsi(closes, RSI_PERIOD), "atr": atr(highs, lows, closes, ATR_PERIOD),
    }


def analyze(candles, i=None, ind=None):
    """Индикаторы и события на свече i (по умолчанию — последняя закрытая).
    ind можно посчитать заранее через indicators() — так делает бэктест."""
    ind = ind or indicators(candles)
    closes, vols = ind["closes"], ind["vols"]
    i = len(closes) - 1 if i is None else i

    ef, es, et, r, a = ind["ef"], ind["es"], ind["et"], ind["rsi"], ind["atr"]

    events = set()
    if None not in (ef[i - 1], es[i - 1], ef[i], es[i]):
        if ef[i - 1] <= es[i - 1] and ef[i] > es[i]:
            events.add("EMA_UP")
        if ef[i - 1] >= es[i - 1] and ef[i] < es[i]:
            events.add("EMA_DOWN")
    if r[i - 1] is not None and r[i] is not None:
        if r[i - 1] < RSI_LOW <= r[i]:
            events.add("RSI_UP")
        if r[i - 1] > RSI_HIGH >= r[i]:
            events.add("RSI_DOWN")

    vol_ratio = 0.0
    if i >= VOL_WINDOW:
        avg_vol = sum(vols[i - VOL_WINDOW:i]) / VOL_WINDOW
        vol_ratio = vols[i] / avg_vol if avg_vol > 0 else 0.0
        if vol_ratio > VOL_MULT:
            events.add("VOLUME")

    return {"time": candles[i]["time"], "price": closes[i], "rsi": r[i],
            "trend": et[i], "atr": a[i], "events": events, "vol_ratio": vol_ratio}


EVENT_TEXT = {
    "EMA_UP": f"EMA {EMA_FAST} пересекла EMA {EMA_SLOW} вверх",
    "EMA_DOWN": f"EMA {EMA_FAST} пересекла EMA {EMA_SLOW} вниз",
    "RSI_UP": "RSI вышел из перепроданности",
    "RSI_DOWN": "RSI вышел из перекупленности",
}


# ---------- Уведомления и состояние ----------
def fmt(x):
    return f"{x:.6g}"


def pct(a, b):
    return (b / a - 1) * 100


def margin_result(entry, exit_price, side="long"):
    """Результат в % от маржи с учётом плеча, направления сделки и комиссий за вход и выход."""
    move = pct(entry, exit_price) / 100
    if side == "short":
        move = -move
    return (move * LEVERAGE - 2 * FEE * LEVERAGE) * 100


def liquidation_price(entry, side="long"):
    """Примерная цена ликвидации при изолированной марже."""
    if side == "short":
        return entry * (1 + 1 / LEVERAGE - MMR)
    return entry * (1 - 1 / LEVERAGE + MMR)


SIDE_NAME = {"long": "ЛОНГ", "short": "ШОРТ"}


def notify(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        print("\n" + text + "\n")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        ).raise_for_status()
    except requests.RequestException as e:
        log.error("Не удалось отправить в Telegram: %s", e)
        print(text)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


# ---------- История сделок (для статистики) ----------
REASON_CODE = {"🛑": "stop", "🎯": "target", "↩️": "reverse"}


def trade_record(key, pos, exit_price, reason, close_time):
    """Одна закрытая сделка в том виде, в каком её показывает Mini App."""
    symbol, interval = key.split(":")
    side = pos.get("side", "long")
    move = pct(pos["entry"], exit_price) * (-1 if side == "short" else 1)
    return {
        "symbol": symbol, "interval": interval, "side": side,
        "entry": pos["entry"], "exit": exit_price, "stop": pos["stop"], "target": pos["target"],
        "open_time": pos["time"], "close_time": int(close_time),
        "result": next((code for icon, code in REASON_CODE.items() if reason.startswith(icon)), "other"),
        "move_pct": round(move, 3),
        "margin_pct": round(margin_result(pos["entry"], exit_price, side), 2),
    }


def settings_info():
    return {"exchange": EXCHANGE, "interval": INTERVAL, "leverage": LEVERAGE,
            "stop_atr": STOP_ATR, "take_atr": TAKE_ATR}


def load_trades():
    try:
        data = json.loads(TRADES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("open", [])
    data.setdefault("closed", [])
    return data


def save_trades(trades, state):
    """Пишем файл, только если что-то поменялось — иначе в репозитории будет коммит каждые 5 минут."""
    trades["open"] = [
        {"symbol": k.split(":")[0], "interval": k.split(":")[1], **p}
        for k, p in sorted(state.get("positions", {}).items())
    ]
    trades["closed"] = trades["closed"][-MAX_CLOSED:]
    trades["settings"] = settings_info()
    old = load_trades()
    old.pop("updated", None)
    if old == {k: v for k, v in trades.items() if k != "updated"} and TRADES_FILE.exists():
        return
    trades["updated"] = int(time.time() * 1000)
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRADES_FILE.write_text(json.dumps(trades, ensure_ascii=False, indent=1))
    log.info("Статистика обновлена: открыто %d, закрыто всего %d", len(trades["open"]), len(trades["closed"]))


def restore_positions(state, trades):
    """Если кэш GitHub Actions потерялся, берём открытые сделки из файла статистики."""
    positions = state.setdefault("positions", {})
    if positions or not trades["open"]:
        return
    for p in trades["open"]:
        p = dict(p)
        key = f"{p.pop('symbol')}:{p.pop('interval')}"
        positions[key] = p
    log.info("Открытые сделки восстановлены из файла статистики: %d", len(positions))


# ---------- Логика стратегии ----------
def close_message(title, pos, reason, exit_price):
    side = pos.get("side", "long")
    lev_line = ""
    if LEVERAGE > 1:
        lev_line = (f"\nС плечом x{LEVERAGE:g}: "
                    f"{margin_result(pos['entry'], exit_price, side):+.1f}% к марже (с комиссиями)")
    move = pct(pos["entry"], exit_price) * (-1 if side == "short" else 1)
    icon = "✅" if move > 0 else "❌"
    return (
        f"{icon} {title} — <b>ЗАКРЫТЬ {SIDE_NAME[side]}</b>\n\n"
        f"Причина: {reason}\n"
        f"Вход: {fmt(pos['entry'])} → выход: {fmt(exit_price)}\n"
        f"Результат по цене: {move:+.2f}%" + lev_line
    )


def check_levels(pos, candles_after_entry):
    """Дошла ли цена до стопа или цели. Если за одну свечу задело оба — считаем стоп."""
    side = pos.get("side", "long")
    for c in candles_after_entry:
        if side == "long":
            if c["low"] <= pos["stop"]:
                return "🛑 Сработал стоп-лосс", pos["stop"]
            if c["high"] >= pos["target"]:
                return "🎯 Цель достигнута", pos["target"]
        else:
            if c["high"] >= pos["stop"]:
                return "🛑 Сработал стоп-лосс", pos["stop"]
            if c["low"] <= pos["target"]:
                return "🎯 Цель достигнута", pos["target"]
    return None, None


def open_message(title, side, entry, stop, target, a, events):
    rsi_txt = f"{a['rsi']:.1f}" if a["rsi"] is not None else "—"
    lev_block = ""
    if LEVERAGE > 1:
        liq = liquidation_price(entry, side)
        lev_block = (
            f"\n<b>Плечо x{LEVERAGE:g}</b> (изолированная маржа)\n"
            f"Ликвидация ≈ {fmt(liq)} ({pct(entry, liq):+.1f}%)\n"
            f"На стопе: {margin_result(entry, stop, side):+.0f}% маржи · "
            f"на цели: {margin_result(entry, target, side):+.0f}% маржи\n"
        )
        if DEPOSIT > 0:
            loss_per_margin = -margin_result(entry, stop, side) / 100
            margin = DEPOSIT * RISK_PCT / 100 / loss_per_margin
            lev_block += (
                f"Маржа на сделку: ≈ {margin:,.0f}$ ({margin / DEPOSIT * 100:.1f}% депозита), "
                f"объём позиции ≈ {margin * LEVERAGE:,.0f}$ — "
                f"при стопе потеряешь {RISK_PCT:g}% депозита\n"
            )
        lev_block += "Поставь стоп-лосс и тейк-профит ордерами на бирже сразу после входа.\n"

    why = [EVENT_TEXT[e] for e in sorted(events)]
    if a["trend"] is not None:
        why.append(f"цена {'выше' if side == 'long' else 'ниже'} EMA {TREND_EMA} — "
                   f"тренд {'вверх' if side == 'long' else 'вниз'}")
    if "VOLUME" in a["events"]:
        why.append(f"объём x{a['vol_ratio']:.1f} к среднему — подтверждение")

    head = ("🟢 {t} — <b>сигнал в ЛОНГ (покупка)</b>" if side == "long"
            else "🔴 {t} — <b>сигнал в ШОРТ (продажа)</b>").format(t=title)
    short_note = "Шорт открывается только на фьючерсах или марже.\n" if side == "short" else ""
    return (
        f"{head}\n\n"
        f"Вход: {fmt(entry)}\n"
        f"Стоп-лосс: {fmt(stop)} ({pct(entry, stop):+.1f}%)\n"
        f"Цель: {fmt(target)} ({pct(entry, target):+.1f}%)\n"
        f"Риск/прибыль: 1:{TAKE_ATR / STOP_ATR:g} · RSI: {rsi_txt}\n"
        + lev_block + "\n"
        "Почему: " + "; ".join(why) + "\n\n"
        + short_note +
        "Когда выходить — пришлю отдельное сообщение (стоп, цель или разворот).\n"
        "Рискуй не больше 1–2% депозита на сделку. Это сигнал стратегии, не финансовый совет."
    )


def _directional_events(a):
    return a["events"] & {"EMA_UP", "RSI_UP"}, a["events"] & {"EMA_DOWN", "RSI_DOWN"}


def reversal_events(pos, a):
    """События на свече, которые идут против открытой сделки (повод выйти)."""
    up_ev, down_ev = _directional_events(a)
    return down_ev if pos.get("side", "long") == "long" else up_ev


def entry_signal(a):
    """Решение о входе по свече. Возвращает (сигнал, None) или (None, почему нет).
    Одна функция и для бота, и для бэктеста — чтобы статистика считалась по той же логике."""
    trend_up = a["trend"] is None or a["price"] > a["trend"]
    trend_down = a["trend"] is not None and a["price"] < a["trend"]
    up_ev, down_ev = _directional_events(a)

    side, events = None, None
    if up_ev and trend_up:
        side, events = "long", up_ev
    elif down_ev and trend_down and ALLOW_SHORT:
        side, events = "short", down_ev
    if not side or not a["atr"]:
        return None, ("сигнал против тренда — пропущен" if (up_ev or down_ev) else "сигналов нет")

    entry = a["price"]
    sign = 1 if side == "long" else -1
    stop = entry - sign * STOP_ATR * a["atr"]
    target = entry + sign * TAKE_ATR * a["atr"]
    if LEVERAGE > 1:
        liq = liquidation_price(entry, side)
        if abs(entry - stop) > MAX_STOP_OF_LIQ * abs(entry - liq):
            return None, (f"{SIDE_NAME[side]} пропущен — стоп {fmt(stop)} слишком близко "
                          f"к ликвидации {fmt(liq)} при x{LEVERAGE:g}")
    return {"side": side, "events": events, "entry": entry, "stop": stop, "target": target}, None


def check_symbol(symbol, state, min_volume=0, trades=None):
    all_candles = FETCHERS[EXCHANGE](symbol, INTERVAL)
    now_ms = time.time() * 1000
    candles = [c for c in all_candles if c["close_time"] <= now_ms]     # закрытые свечи
    live = [c for c in all_candles if c["close_time"] > now_ms][-1:]    # текущая, ещё не закрытая
    if len(candles) < EMA_SLOW + RSI_PERIOD + 2:
        raise ValueError(f"мало свечей ({len(candles)}), проверь название пары")

    key = f"{symbol}:{INTERVAL}"
    positions = state.setdefault("positions", {})
    last_seen = state.setdefault("last_candle", {})
    if min_volume and key not in positions and daily_volume_usd(candles) < min_volume:
        return "skip"
    title = f"<b>{symbol} · {INTERVAL}</b>"

    # 1) Открытая сделка: стоп и цель проверяем при каждом запуске, включая текущую свечу
    pos = positions.get(key)
    if pos:
        after = [c for c in candles + live if c["time"] > pos["time"]]
        reason, exit_price = check_levels(pos, after)
        if reason:
            notify(close_message(title, pos, reason, exit_price))
            if trades is not None:
                trades["closed"].append(trade_record(key, pos, exit_price, reason, now_ms))
            del positions[key]
            log.info("%s: выход (%s)", symbol, reason)
            last_seen[key] = candles[-1]["time"]  # на этой же свече новую сделку не открываем
            return

    # 2) Сигналы — только по новой закрытой свече
    a = analyze(candles)
    if last_seen.get(key) == a["time"]:
        if pos:
            log.info("%s: %s открыт, держим (цена %s, стоп %s, цель %s)", symbol,
                     SIDE_NAME[pos.get("side", "long")], fmt((live or candles)[-1]["close"]),
                     fmt(pos["stop"]), fmt(pos["target"]))
        else:
            log.info("%s: новой свечи нет", symbol)
        return
    last_seen[key] = a["time"]

    # Разворот против открытой сделки — выход
    if pos:
        side = pos.get("side", "long")
        against = reversal_events(pos, a)
        if against:
            reason = "↩️ Разворот: " + ", ".join(EVENT_TEXT[e] for e in sorted(against))
            notify(close_message(title, pos, reason, a["price"]))
            if trades is not None:
                trades["closed"].append(trade_record(key, pos, a["price"], reason, candles[-1]["close_time"]))
            del positions[key]
            log.info("%s: выход по развороту", symbol)
        else:
            log.info("%s: %s открыт, держим", symbol, SIDE_NAME[side])
        return

    # Новая сделка
    sig, skip = entry_signal(a)
    if not sig:
        log.info("%s: %s", symbol, skip)
        return
    positions[key] = {"side": sig["side"], "entry": sig["entry"], "stop": sig["stop"],
                      "target": sig["target"], "time": a["time"]}
    notify(open_message(title, sig["side"], sig["entry"], sig["stop"], sig["target"], a, sig["events"]))
    log.info("%s: открыт %s по %s", symbol, SIDE_NAME[sig["side"]], fmt(sig["entry"]))


def resolve_symbols():
    raw = os.getenv("SYMBOLS") or DEFAULT_SYMBOLS[EXCHANGE]
    if raw.strip().upper() == "ALL":
        return all_symbols(), True
    return [s.strip().upper() for s in raw.split(",") if s.strip()], False


def run_check(state):
    symbols, all_mode = resolve_symbols()
    # у Coinbase объём в списке пар не отдаётся, поэтому фильтруем по свечам
    min_volume = MIN_VOLUME_USD if all_mode and EXCHANGE == "coinbase" else 0
    log.info("Биржа %s, пар к проверке: %d, таймфрейм %s, плечо x%g",
             EXCHANGE, len(symbols), INTERVAL, LEVERAGE)
    trades = load_trades()
    restore_positions(state, trades)
    errors = skipped = 0
    for symbol in symbols:
        try:
            if check_symbol(symbol, state, min_volume, trades) == "skip":
                skipped += 1
        except Exception as e:
            errors += 1
            log.error("%s: ошибка — %s", symbol, e)
        if all_mode:
            time.sleep(0.2)  # не упираться в лимит запросов биржи
    if skipped:
        log.info("Пропущено мелких монет (оборот < %s$ в сутки): %d", f"{MIN_VOLUME_USD:,.0f}", skipped)
    save_state(state)
    try:
        save_trades(trades, state)
    except OSError as e:
        log.error("Не удалось сохранить статистику: %s", e)
    return errors, len(symbols)


def main():
    if EXCHANGE not in FETCHERS:
        sys.exit(f"Неизвестная биржа EXCHANGE={EXCHANGE}. Доступно: binance, coinbase")
    if EXCHANGE == "coinbase" and INTERVAL not in COINBASE_GRANULARITY:
        sys.exit(f"Coinbase не поддерживает таймфрейм {INTERVAL}. Доступно: {', '.join(COINBASE_GRANULARITY)}")
    if not (TG_TOKEN and TG_CHAT_ID):
        log.warning("TG_TOKEN / TG_CHAT_ID не заданы — сигналы будут в консоли")

    state = load_state()
    if RUN_ONCE:
        errors, total = run_check(state)
        sys.exit(1 if total and errors == total else 0)  # красный запуск, если не удалось ни одной паре

    while True:
        try:
            run_check(state)
        except Exception as e:
            log.error("Ошибка проверки: %s", e)
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
