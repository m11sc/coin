#!/usr/bin/env python3
"""
Бэктест: прогоняет ту же стратегию, что и crypto_signals.py, по прошлым свечам
и сохраняет все сделки в docs/data/backtest.json — их показывает Mini App на вкладке «История».

Настройки те же переменные окружения, что у бота (EXCHANGE, SYMBOLS, INTERVAL, LEVERAGE, ALLOW_SHORT ...),
плюс BACKTEST_PAGES — сколько страниц свечей скачать (Coinbase: 300 свечей на страницу, Binance: 1000).

Логика входа и выхода берётся прямо из crypto_signals.py (entry_signal, reversal_events, check_levels),
поэтому результат совпадает с тем, что бот сделал бы в реальности на этих свечах.
"""

import json
import logging
import os
import time
from pathlib import Path

import requests

import crypto_signals as cs

PAGES = int(os.getenv("BACKTEST_PAGES") or 6)
OUT_FILE = Path(os.getenv("BACKTEST_FILE") or Path(__file__).parent / "docs" / "data" / "backtest.json")
log = logging.getLogger("backtest")

BINANCE_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def fetch_history(symbol):
    if cs.EXCHANGE == "coinbase":
        return cs.fetch_coinbase(symbol, cs.INTERVAL, pages=PAGES)
    rows, end = {}, None
    for _ in range(PAGES):
        params = {"symbol": symbol, "interval": cs.INTERVAL, "limit": 1000}
        if end:
            params["endTime"] = end
        r = requests.get("https://api.binance.com/api/v3/klines", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for k in data:
            rows[k[0]] = {"time": k[0], "close_time": k[6], "high": float(k[2]), "low": float(k[3]),
                          "close": float(k[4]), "volume": float(k[5])}
        end = data[0][0] - 1
    return [rows[t] for t in sorted(rows)]


def simulate(symbol, candles):
    """Сделки по одной паре. Порядок проверок — как у бота при каждом закрытии свечи."""
    key = f"{symbol}:{cs.INTERVAL}"
    ind = cs.indicators(candles)
    trades, pos = [], None
    for i in range(cs.TREND_EMA, len(candles)):
        c = candles[i]
        if pos:
            reason, price = cs.check_levels(pos, [c])
            if reason:
                trades.append(cs.trade_record(key, pos, price, reason, c["close_time"]))
                pos = None
                continue  # на свече выхода новую сделку бот не открывает
        a = cs.analyze(candles, i, ind)
        if pos:
            against = cs.reversal_events(pos, a)
            if against:
                trades.append(cs.trade_record(key, pos, a["price"], "↩️ Разворот", c["close_time"]))
                pos = None
            continue
        sig, _ = cs.entry_signal(a)
        if sig:
            pos = {"side": sig["side"], "entry": sig["entry"], "stop": sig["stop"],
                   "target": sig["target"], "time": a["time"]}
    return trades


def main():
    symbols, all_mode = cs.resolve_symbols()
    min_volume = cs.MIN_VOLUME_USD if all_mode and cs.EXCHANGE == "coinbase" else 0
    log.info("Бэктест: биржа %s, пар %d, таймфрейм %s, страниц свечей %d",
             cs.EXCHANGE, len(symbols), cs.INTERVAL, PAGES)
    closed, first, last = [], None, None
    for symbol in symbols:
        try:
            now_ms = time.time() * 1000
            candles = [c for c in fetch_history(symbol) if c["close_time"] <= now_ms]
            if len(candles) <= cs.TREND_EMA + 2:
                log.info("%s: мало истории (%d свечей) — пропущено", symbol, len(candles))
                continue
            if min_volume and cs.daily_volume_usd(candles) < min_volume:
                continue
            t = simulate(symbol, candles)
            closed += t
            start = candles[cs.TREND_EMA]["time"]
            first = start if first is None else min(first, start)
            last = candles[-1]["close_time"] if last is None else max(last, candles[-1]["close_time"])
            log.info("%s: сделок %d", symbol, len(t))
        except Exception as e:
            log.error("%s: ошибка — %s", symbol, e)
        if all_mode:
            time.sleep(0.2)
    closed.sort(key=lambda t: t["close_time"])
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "settings": cs.settings_info(), "from": first, "to": last,
        "updated": int(time.time() * 1000), "open": [], "closed": closed,
    }, ensure_ascii=False, indent=1))
    log.info("Готово: %d сделок → %s", len(closed), OUT_FILE)


if __name__ == "__main__":
    main()
