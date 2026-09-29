#!/usr/bin/env python3
"""
Торговые сигналы по криптовалюте с рекомендациями: когда покупать и когда выходить.

Как работает стратегия (только по закрытым свечам):
  ПОКУПКА, если:
    - EMA 9 пересекла EMA 21 снизу вверх ИЛИ RSI вышел из перепроданности (<30)
    - и цена выше EMA 200 (фильтр тренда: против тренда не покупаем)
  Сразу считаются уровни по ATR (средний размах свечи):
    - стоп-лосс = вход − 1.5 × ATR
    - цель      = вход + 3 × ATR   (риск/прибыль 1:2)
  ВЫХОД (бот сам следит за сделкой и пишет):
    - цена дошла до стоп-лосса или до цели
    - или разворот: EMA 9 пересекла EMA 21 вниз / RSI вышел из перекупленности (>70)
  Всплеск объёма отмечается в сообщении как подтверждение.
  Если сделки нет, а тренд вниз и появился сигнал разворота —
  придёт предупреждение «если держишь монету, подумай о выходе».

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

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

STATE_FILE = Path(__file__).with_name("signals_state.json")
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
    now_ms = time.time() * 1000
    return [
        {"time": k[0], "high": float(k[2]), "low": float(k[3]),
         "close": float(k[4]), "volume": float(k[5])}
        for k in r.json() if k[6] < now_ms  # только закрытые свечи
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
    now = time.time()
    return [
        {"time": k[0] * 1000, "high": float(k[2]), "low": float(k[1]),
         "close": float(k[4]), "volume": float(k[5])}
        for t, k in sorted(rows.items()) if t + gran <= now  # только закрытые
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


def analyze(candles):
    """Индикаторы и события на последней закрытой свече."""
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    vols = [c["volume"] for c in candles]
    i = len(closes) - 1

    ef, es, et = ema(closes, EMA_FAST), ema(closes, EMA_SLOW), ema(closes, TREND_EMA)
    r = rsi(closes, RSI_PERIOD)
    a = atr(highs, lows, closes, ATR_PERIOD)

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


def margin_result(entry, exit_price):
    """Результат в % от маржи с учётом плеча и комиссий за вход и выход."""
    return (pct(entry, exit_price) / 100 * LEVERAGE - 2 * FEE * LEVERAGE) * 100


def liquidation_price(entry):
    """Примерная цена ликвидации лонга при изолированной марже."""
    return entry * (1 - 1 / LEVERAGE + MMR)


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


# ---------- Логика стратегии ----------
def check_symbol(symbol, state, min_volume=0):
    candles = FETCHERS[EXCHANGE](symbol, INTERVAL)
    if len(candles) < EMA_SLOW + RSI_PERIOD + 2:
        raise ValueError(f"мало свечей ({len(candles)}), проверь название пары")
    key = f"{symbol}:{INTERVAL}"
    positions = state.setdefault("positions", {})
    if min_volume and key not in positions and daily_volume_usd(candles) < min_volume:
        return "skip"
    last_seen = state.setdefault("last_candle", {})

    a = analyze(candles)
    prev_time = last_seen.get(key)
    if prev_time == a["time"]:
        log.info("%s: новой свечи нет", symbol)
        return
    last_seen[key] = a["time"]

    title = f"<b>{symbol} · {INTERVAL}</b>"
    rsi_txt = f"{a['rsi']:.1f}" if a["rsi"] is not None else "—"
    trend_up = a["trend"] is None or a["price"] > a["trend"]
    buy_ev = a["events"] & {"EMA_UP", "RSI_UP"}
    sell_ev = a["events"] & {"EMA_DOWN", "RSI_DOWN"}

    pos = positions.get(key)
    if pos:
        # Проверяем все свечи, закрывшиеся после входа и прошлой проверки
        since = max(pos["time"], prev_time or 0)
        reason, exit_price = None, None
        for c in candles:
            if c["time"] <= since:
                continue
            if c["low"] <= pos["stop"]:  # если за свечу задело и стоп, и цель — считаем стоп
                reason, exit_price = "🛑 Сработал стоп-лосс", pos["stop"]
                break
            if c["high"] >= pos["target"]:
                reason, exit_price = "🎯 Цель достигнута", pos["target"]
                break
        if not reason and sell_ev:
            reason = "📉 Разворот: " + ", ".join(EVENT_TEXT[e] for e in sorted(sell_ev))
            exit_price = a["price"]

        if reason:
            result = pct(pos["entry"], exit_price)
            lev_line = ""
            if LEVERAGE > 1:
                lev_line = f"\nС плечом x{LEVERAGE:g}: {margin_result(pos['entry'], exit_price):+.1f}% к марже (с комиссиями)"
            notify(
                f"🔴 {title} — <b>ПРОДАВАТЬ / закрыть сделку</b>\n\n"
                f"Причина: {reason}\n"
                f"Вход: {fmt(pos['entry'])} → выход: {fmt(exit_price)}\n"
                f"Цена: {result:+.2f}%" + lev_line
            )
            del positions[key]
            log.info("%s: выход (%s)", symbol, reason)
        else:
            log.info("%s: сделка открыта, держим (цена %s, стоп %s, цель %s)",
                     symbol, fmt(a["price"]), fmt(pos["stop"]), fmt(pos["target"]))
        return

    if buy_ev and trend_up and a["atr"]:
        entry = a["price"]
        stop = entry - STOP_ATR * a["atr"]
        target = entry + TAKE_ATR * a["atr"]

        lev_block = ""
        if LEVERAGE > 1:
            liq = liquidation_price(entry)
            if (entry - stop) > MAX_STOP_OF_LIQ * (entry - liq):
                log.info("%s: сигнал пропущен — стоп %s слишком близко к ликвидации %s при x%g",
                         symbol, fmt(stop), fmt(liq), LEVERAGE)
                return
            lev_block = (
                f"\n<b>Плечо x{LEVERAGE:g}</b> (изолированная маржа)\n"
                f"Ликвидация ≈ {fmt(liq)} ({pct(entry, liq):+.1f}%)\n"
                f"На стопе: {margin_result(entry, stop):+.0f}% маржи · "
                f"на цели: {margin_result(entry, target):+.0f}% маржи\n"
            )
            if DEPOSIT > 0:
                loss_per_margin = -margin_result(entry, stop) / 100
                margin = DEPOSIT * RISK_PCT / 100 / loss_per_margin
                lev_block += (
                    f"Маржа на сделку: ≈ {margin:,.0f}$ ({margin / DEPOSIT * 100:.1f}% депозита), "
                    f"объём позиции ≈ {margin * LEVERAGE:,.0f}$ — "
                    f"при стопе потеряешь {RISK_PCT:g}% депозита\n"
                )
            lev_block += "Поставь стоп-лосс и тейк-профит ордерами на бирже сразу после входа.\n"

        positions[key] = {"entry": entry, "stop": stop, "target": target, "time": a["time"]}

        why = [EVENT_TEXT[e] for e in sorted(buy_ev)]
        if a["trend"] is not None:
            why.append(f"цена выше EMA {TREND_EMA} — тренд вверх")
        if "VOLUME" in a["events"]:
            why.append(f"объём x{a['vol_ratio']:.1f} к среднему — подтверждение")
        notify(
            f"🟢 {title} — <b>сигнал на ПОКУПКУ</b>\n\n"
            f"Вход: {fmt(entry)}\n"
            f"Стоп-лосс: {fmt(stop)} ({pct(entry, stop):+.1f}%)\n"
            f"Цель: {fmt(target)} ({pct(entry, target):+.1f}%)\n"
            f"Риск/прибыль: 1:{TAKE_ATR / STOP_ATR:g} · RSI: {rsi_txt}\n"
            + lev_block + "\n"
            f"Почему: " + "; ".join(why) + "\n\n"
            "Когда выходить — пришлю отдельное сообщение (стоп, цель или разворот).\n"
            "Рискуй не больше 1–2% депозита на сделку. Это сигнал стратегии, не финансовый совет."
        )
        log.info("%s: покупка по %s", symbol, fmt(entry))
    elif buy_ev:
        log.info("%s: сигнал на покупку пропущен — цена ниже EMA %d (тренд вниз)", symbol, TREND_EMA)
    elif sell_ev and not trend_up:
        notify(
            f"⚠️ {title} — <b>сигнал на продажу</b>\n\n"
            + "; ".join(EVENT_TEXT[e] for e in sorted(sell_ev))
            + f", цена ниже EMA {TREND_EMA} — тренд вниз.\n"
            f"Цена: {fmt(a['price'])} · RSI: {rsi_txt}\n\n"
            "Если держишь эту монету — стоит подумать о выходе. Покупать сейчас не стоит."
        )
        log.info("%s: предупреждение о продаже", symbol)
    else:
        log.info("%s: сигналов нет", symbol)


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
    errors = skipped = 0
    for symbol in symbols:
        try:
            if check_symbol(symbol, state, min_volume) == "skip":
                skipped += 1
        except Exception as e:
            errors += 1
            log.error("%s: ошибка — %s", symbol, e)
        if all_mode:
            time.sleep(0.2)  # не упираться в лимит запросов биржи
    if skipped:
        log.info("Пропущено мелких монет (оборот < %s$ в сутки): %d", f"{MIN_VOLUME_USD:,.0f}", skipped)
    save_state(state)
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
