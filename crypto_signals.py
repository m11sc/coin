#!/usr/bin/env python3
"""
Уведомления о торговых сигналах по криптовалюте.

Берёт свечи с биржи (публичный API, ключ не нужен), считает индикаторы
и шлёт сигналы в Telegram. Если Telegram не настроен — печатает в консоль.

Сигналы (только по закрытым свечам):
  - EMA 9 пересекла EMA 21 (вверх / вниз)
  - RSI вышел из зоны перепроданности (<30) или перекупленности (>70)
  - Всплеск объёма (объём свечи > среднего за 20 свечей в N раз)

Два режима:
  - на своём компьютере: работает в цикле, проверяет каждые CHECK_EVERY секунд
  - в GitHub Actions (RUN_ONCE=1): одна проверка и выход, запуск по расписанию

Биржи (EXCHANGE):
  - binance  — пары вида BTCUSDT (по умолчанию для своего компьютера)
  - coinbase — пары вида BTC-USD (для GitHub Actions: Binance блокирует серверы в США)

Запуск на своём компьютере:
  pip install requests
  export TG_TOKEN="123:ABC"        # токен бота от @BotFather
  export TG_CHAT_ID="123456789"    # твой chat id
  python crypto_signals.py

Это не финансовый совет: сигналы — просто индикаторы, решения принимай сам.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

# ---------- Настройки (можно менять здесь или через переменные окружения) ----------
EXCHANGE = (os.getenv("EXCHANGE") or "binance").lower()
DEFAULT_SYMBOLS = {
    "binance": "BTCUSDT,ETHUSDT,SOLUSDT",
    "coinbase": "BTC-USD,ETH-USD,SOL-USD",
}
INTERVAL = os.getenv("INTERVAL") or "1h"             # binance: 1m,5m,15m,1h,4h,1d; coinbase: 1m,5m,15m,1h,6h,1d
CHECK_EVERY = int(os.getenv("CHECK_EVERY") or 60)     # пауза между проверками, сек (для своего компьютера)
RUN_ONCE = os.getenv("RUN_ONCE") == "1"               # одна проверка и выход (для GitHub Actions)

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_LOW = 30
RSI_HIGH = 70
VOL_WINDOW = 20
VOL_MULT = 2.5

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

STATE_FILE = Path(__file__).with_name("signals_state.json")
# ------------------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("signals")

COINBASE_GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}


def fetch_binance(symbol, interval):
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": 200},
        timeout=10,
    )
    r.raise_for_status()
    now_ms = time.time() * 1000
    # k[6] — время закрытия свечи; берём только закрытые
    return [
        {"time": k[0], "close": float(k[4]), "volume": float(k[5])}
        for k in r.json() if k[6] < now_ms
    ]


def fetch_coinbase(symbol, interval):
    gran = COINBASE_GRANULARITY[interval]
    r = requests.get(
        f"https://api.exchange.coinbase.com/products/{symbol}/candles",
        params={"granularity": gran},
        headers={"User-Agent": "crypto-signals"},
        timeout=10,
    )
    r.raise_for_status()
    now = time.time()
    rows = sorted(r.json(), key=lambda k: k[0])  # Coinbase отдаёт от новых к старым
    # формат: [time, low, high, open, close, volume]; берём только закрытые
    return [
        {"time": k[0] * 1000, "close": float(k[4]), "volume": float(k[5])}
        for k in rows if k[0] + gran <= now
    ]


FETCHERS = {"binance": fetch_binance, "coinbase": fetch_coinbase}


def ema(values, period):
    k = 2 / (period + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(values, period=14):
    """RSI по Уайлдеру. Для первых свечей — None."""
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


def find_signals(candles):
    """Проверяет последнюю закрытую свечу. Возвращает список (код, текст)."""
    closes = [c["close"] for c in candles]
    vols = [c["volume"] for c in candles]
    i = len(closes) - 1
    signals = []

    ef, es = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    if ef[i - 1] <= es[i - 1] and ef[i] > es[i]:
        signals.append(("EMA_UP", f"🟢 EMA {EMA_FAST} пересекла EMA {EMA_SLOW} снизу вверх"))
    if ef[i - 1] >= es[i - 1] and ef[i] < es[i]:
        signals.append(("EMA_DOWN", f"🔴 EMA {EMA_FAST} пересекла EMA {EMA_SLOW} сверху вниз"))

    r = rsi(closes, RSI_PERIOD)
    if r[i - 1] is not None and r[i] is not None:
        if r[i - 1] < RSI_LOW <= r[i]:
            signals.append(("RSI_UP", f"🟢 RSI вышел из перепроданности ({r[i]:.1f})"))
        if r[i - 1] > RSI_HIGH >= r[i]:
            signals.append(("RSI_DOWN", f"🔴 RSI вышел из перекупленности ({r[i]:.1f})"))

    if i >= VOL_WINDOW:
        avg_vol = sum(vols[i - VOL_WINDOW:i]) / VOL_WINDOW
        if avg_vol > 0 and vols[i] > avg_vol * VOL_MULT:
            signals.append(("VOLUME", f"⚡ Всплеск объёма: x{vols[i] / avg_vol:.1f} к среднему"))

    return signals, closes[i], r[i]


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


def check_symbol(symbol, state):
    candles = FETCHERS[EXCHANGE](symbol, INTERVAL)
    if len(candles) < EMA_SLOW + RSI_PERIOD:
        raise ValueError(f"мало свечей ({len(candles)}), проверь название пары")
    signals, price, rsi_now = find_signals(candles)
    candle_time = candles[-1]["time"]

    new = []
    for code, text in signals:
        key = f"{symbol}:{INTERVAL}:{code}"
        if state.get(key) != candle_time:  # не повторяем сигнал по той же свече
            state[key] = candle_time
            new.append(text)

    if new:
        rsi_txt = f"{rsi_now:.1f}" if rsi_now is not None else "—"
        msg = (
            f"<b>{symbol}</b> · {INTERVAL}\n"
            f"Цена: {price:g} · RSI: {rsi_txt}\n\n" + "\n".join(new)
        )
        notify(msg)
        log.info("%s: %d сигнал(а)", symbol, len(new))
    else:
        log.info("%s: сигналов нет", symbol)


def run_check(symbols, state):
    errors = 0
    for symbol in symbols:
        try:
            check_symbol(symbol, state)
        except Exception as e:
            errors += 1
            log.error("%s: ошибка — %s", symbol, e)
    save_state(state)
    return errors


def main():
    if EXCHANGE not in FETCHERS:
        sys.exit(f"Неизвестная биржа EXCHANGE={EXCHANGE}. Доступно: binance, coinbase")
    if EXCHANGE == "coinbase" and INTERVAL not in COINBASE_GRANULARITY:
        sys.exit(f"Coinbase не поддерживает таймфрейм {INTERVAL}. Доступно: {', '.join(COINBASE_GRANULARITY)}")

    symbols = [s.strip().upper() for s in (os.getenv("SYMBOLS") or DEFAULT_SYMBOLS[EXCHANGE]).split(",") if s.strip()]
    log.info("Биржа %s, слежу за %s, таймфрейм %s", EXCHANGE, ", ".join(symbols), INTERVAL)
    if not (TG_TOKEN and TG_CHAT_ID):
        log.warning("TG_TOKEN / TG_CHAT_ID не заданы — сигналы будут в консоли")

    state = load_state()
    if RUN_ONCE:
        errors = run_check(symbols, state)
        sys.exit(1 if errors == len(symbols) else 0)  # красный запуск, если не удалось ни одной паре

    while True:
        run_check(symbols, state)
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
