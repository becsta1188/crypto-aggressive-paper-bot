"""Aggressive multi-crypto PAPER experiment using public Binance spot candles.
No account connection, keys, or real orders. USDT-denominated simulation.
"""
import datetime as dt
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

START = 1000.0  # USDT, NOT AUD
FEE_RATE = 0.001
SLIPPAGE = 0.002
MAX_POSITIONS = 3
ALLOCATION = 0.40
STOP_LOSS = 0.08
TAKE_PROFIT = 0.15
MOMENTUM_THRESHOLD = 0.025
VOLUME_RATIO = 1.25
MIN_TRADE = 20.0
STATE_FILE = Path('crypto_state.json')
SYDNEY = ZoneInfo('Australia/Sydney')
BASE_URL = 'https://data-api.binance.vision'
SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT',
    'DOGEUSDT', 'LINKUSDT', 'AVAXUSDT', 'ADAUSDT',
    'DOTUSDT', 'LTCUSDT', 'BCHUSDT', 'UNIUSDT',
]


def initial_state():
    return {'currency': 'USDT', 'cash': START, 'positions': {},
            'trades': [], 'snapshots': [], 'last_processed_bar': None}


def load_state():
    if not STATE_FILE.exists():
        return initial_state()
    with STATE_FILE.open(encoding='utf-8') as f:
        state = json.load(f)
    # Never reinterpret the old CoinGecko AUD ledger as USDT.
    if state.get('currency') != 'USDT':
        raise RuntimeError('Existing state is not USDT-denominated. Back it up and rename it before switching.')
    for key, value in initial_state().items():
        state.setdefault(key, value)
    return state


def save_state(state):
    temp = STATE_FILE.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, allow_nan=False)
    os.replace(temp, STATE_FILE)


def get_candles(session, symbol):
    response = session.get(
        BASE_URL + '/api/v3/klines',
        params={'symbol': symbol, 'interval': '1h', 'limit': 55}, timeout=20)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or len(rows) < 50:
        raise ValueError('Insufficient candles')
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    # A bar is usable only after its official close time.
    closed = [r for r in rows if int(r[6]) < now_ms]
    if len(closed) < 49:
        raise ValueError('Insufficient closed candles')
    bars = closed[-49:]
    for a, b in zip(bars, bars[1:]):
        if int(b[0]) - int(a[0]) != 3600000:
            raise ValueError('Missing hourly candle')
    if now_ms - int(bars[-1][6]) > 2 * 3600000:
        raise ValueError('Stale candles')
    closes = [float(r[4]) for r in bars]
    volumes = [float(r[7]) for r in bars]  # quote-asset USDT volume per hour
    if not all(math.isfinite(x) and x > 0 for x in closes):
        raise ValueError('Invalid price')
    if not all(math.isfinite(v) and v >= 0 for v in volumes):
        raise ValueError('Invalid volume')
    return {'price': closes[-1], 'closes': closes, 'volumes': volumes,
            'bar': int(bars[-1][0])}


def momentum(data):
    c, v = data['closes'], data['volumes']
    m6 = c[-1] / c[-7] - 1
    m24 = c[-1] / c[-25] - 1
    recent = sum(v[-6:]) / 6
    baseline = sum(v[-30:-6]) / 24
    ratio = recent / baseline if baseline > 0 else 0
    score = 0.6 * m6 + 0.4 * m24
    return (m6 > MOMENTUM_THRESHOLD and m24 > 0 and ratio > VOLUME_RATIO,
            score, m6, m24, ratio)


def trade(state, symbol, side, observed_price, bar, reason):
    if side == 'BUY':
        if symbol in state['positions'] or len(state['positions']) >= MAX_POSITIONS:
            return
        budget = min(state['cash'], START * ALLOCATION)
        if budget < MIN_TRADE:
            return
        fill = observed_price * (1 + SLIPPAGE)
        qty = budget / (fill * (1 + FEE_RATE))
        gross = qty * fill
        fee = gross * FEE_RATE
        state['cash'] -= gross + fee
        state['positions'][symbol] = {'quantity': qty, 'entry': fill}
    else:
        position = state['positions'].get(symbol)
        if not position:
            return
        qty = position['quantity']
        fill = observed_price * (1 - SLIPPAGE)
        gross = qty * fill
        fee = gross * FEE_RATE
        state['cash'] += gross - fee
        del state['positions'][symbol]
    state['trades'].append({'bar_utc_ms': bar, 'symbol': symbol,
                            'side': side, 'quantity': qty, 'fill_usdt': fill,
                            'fee_usdt': fee, 'reason': reason})
    print(f'PAPER {side} {symbol}: {qty:.6f} @ {fill:.6f} USDT | {reason}')


def main():
    state = load_state()
    with requests.Session() as session:
        market = {}
        failures = []
        for symbol in SYMBOLS:
            try:
                market[symbol] = get_candles(session, symbol)
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
                failures.append(f'{symbol}: {exc}')
        # Require a complete, synchronized scan; never trade on partial data.
        if failures:
            raise RuntimeError('Incomplete market scan; no state changed: ' + '; '.join(failures))
        bars = {d['bar'] for d in market.values()}
        if len(bars) != 1:
            raise RuntimeError('Unsynchronized candles; no trading')
        bar = bars.pop()
        if state['last_processed_bar'] is not None and bar <= state['last_processed_bar']:
            print('Already processed this hourly candle; no new trades')
            return

        ranked = []
        for symbol, data in market.items():
            eligible, score, m6, m24, ratio = momentum(data)
            print(f'{symbol}: {data["price"]:.6f} USDT | 6h {m6:+.2%} | '
                  f'24h {m24:+.2%} | volume {ratio:.2f}x')
            if eligible:
                ranked.append((score, symbol))

        for symbol, position in list(state['positions'].items()):
            if symbol not in market:
                raise RuntimeError(f'Missing held price: {symbol}')
            price = market[symbol]['price']
            change = price / position['entry'] - 1
            if change <= -STOP_LOSS:
                trade(state, symbol, 'SELL', price, bar, 'stop-loss signal')
            elif change >= TAKE_PROFIT:
                trade(state, symbol, 'SELL', price, bar, 'take-profit signal')

        for score, symbol in sorted(ranked, reverse=True):
            if symbol not in state['positions']:
                trade(state, symbol, 'BUY', market[symbol]['price'], bar,
                      f'momentum score {score:.4f}')

        holdings = sum(p['quantity'] * market[s]['price']
                       for s, p in state['positions'].items())
        total = state['cash'] + holdings
        state['snapshots'].append({
            'bar_utc_ms': bar, 'cash_usdt': round(state['cash'], 6),
            'holdings_usdt': round(holdings, 6), 'total_usdt': round(total, 6)})
        state['last_processed_bar'] = bar
        save_state(state)
        print(f'Cash: {state["cash"]:.2f} USDT | Holdings: {holdings:.2f} USDT')
        print(f'Portfolio: {total:.2f} USDT | Return: {total / START - 1:+.2%}')
        print(f'Trades recorded: {len(state["trades"])}')


if __name__ == '__main__':
    main()
