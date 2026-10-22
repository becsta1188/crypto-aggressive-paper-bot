
"""Aggressive multi-crypto PAPER trader. No real orders or API keys."""
import datetime as dt
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

START = 1000.0
FEE_RATE = 0.001
SLIPPAGE = 0.002
MAX_POSITIONS = 3
ALLOCATION = 0.40
STOP_LOSS = 0.08
TAKE_PROFIT = 0.15
MOMENTUM_THRESHOLD = 0.025
VOLUME_RATIO = 1.25

STATE_FILE = Path("crypto_state.json")
SYDNEY = ZoneInfo("Australia/Sydney")

COINS = [
    "bitcoin", "ethereum", "solana", "ripple",
    "dogecoin", "chainlink", "avalanche-2", "cardano",
    "polkadot", "litecoin", "bitcoin-cash", "uniswap"
]

API = "https://api.coingecko.com/api/v3"


def initial_state():
    return {
        "cash": START,
        "positions": {},
        "trades": [],
        "snapshots": [],
        "last_prices": {},
        "last_bar": {}
    }


def load_state():
    if not STATE_FILE.exists():
        return initial_state()
    with STATE_FILE.open() as f:
        state = json.load(f)
    for key, default in initial_state().items():
        state.setdefault(key, default)
    return state


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")
    with temporary.open("w") as f:
        json.dump(state, f, indent=2)
    os.replace(temporary, STATE_FILE)


def fetch_market():
    response = requests.get(
        f"{API}/coins/markets",
        params={
            "vs_currency": "aud",
            "ids": ",".join(COINS),
            "order": "market_cap_desc",
            "per_page": 100,
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "1h,24h"
        },
        timeout=30
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("Unexpected market response")
    return {row["id"]: row for row in rows if row.get("id") in COINS}


def fetch_history(coin):
    response = requests.get(
        f"{API}/coins/{coin}/market_chart",
        params={"vs_currency": "aud", "days": "7"},
        timeout=30
    )
    response.raise_for_status()
    data = response.json()
    prices = data.get("prices", [])
    volumes = data.get("total_volumes", [])
    if len(prices) < 25 or len(volumes) < 25:
        raise ValueError("Insufficient history")
    return prices, volumes


def signals(coin, market):
    prices, volumes = fetch_history(coin)
    current = float(market["current_price"])
    if current <= 0:
        raise ValueError("Invalid price")

    # Approximate hourly observations, sampled by timestamp.
    latest_time = prices[-1][0]
    def earlier(hours):
        target = latest_time - hours * 3600000
        eligible = [p for p in prices if p[0] <= target]
        if not eligible:
            raise ValueError("Insufficient lookback")
        return float(eligible[-1][1])

    p6 = earlier(6)
    p24 = earlier(24)
    momentum6 = current / p6 - 1
    momentum24 = current / p24 - 1

    recent_volumes = [float(v[1]) for v in volumes[-24:]]
    previous_volumes = [float(v[1]) for v in volumes[-48:-24]]
    if not previous_volumes:
        raise ValueError("Insufficient volume lookback")

    # CoinGecko supplies rolling volume measurements, so this is
    # a volume-level comparison, not true hourly traded volume.
    baseline = sum(previous_volumes) / len(previous_volumes)
    ratio = recent_volumes[-1] / baseline if baseline > 0 else 0

    score = momentum6 * 0.6 + momentum24 * 0.4
    eligible = (
        momentum6 > MOMENTUM_THRESHOLD
        and momentum24 > 0
        and ratio > VOLUME_RATIO
    )
    return eligible, score, momentum6, momentum24, ratio


def execute(state, coin, action, price, timestamp, reason):
    if action == "BUY":
        if coin in state["positions"]:
            return False
        if len(state["positions"]) >= MAX_POSITIONS:
            return False
        budget = min(state["cash"], START * ALLOCATION)
        if budget < 20:
            return False

        fill = price * (1 + SLIPPAGE)
        quantity = budget / (fill * (1 + FEE_RATE))
        cost = quantity * fill
        fee = cost * FEE_RATE
        state["cash"] -= cost + fee
        state["positions"][coin] = {
            "quantity": quantity,
            "entry": fill,
            "entry_fee": fee
        }
    else:
        position = state["positions"].get(coin)
        if not position:
            return False
        quantity = position["quantity"]
        fill = price * (1 - SLIPPAGE)
        proceeds = quantity * fill
        fee = proceeds * FEE_RATE
        state["cash"] += proceeds - fee
        del state["positions"][coin]

    state["trades"].append({
        "time": timestamp,
        "coin": coin,
        "action": action,
        "quantity": quantity,
        "price_aud": fill,
        "fee_aud": fee,
        "reason": reason
    })
    print(f"PAPER {action}: {coin} @ A${fill:.5f} | {reason}")
    return True


def main():
    now = dt.datetime.now(SYDNEY).isoformat()
    state = load_state()
    market = fetch_market()

    if len(market) < 8:
        raise RuntimeError("Too few market quotes; refusing to trade")

    candidates = []
    valid_prices = {}

    for coin in COINS:
        row = market.get(coin)
        if not row:
            print(f"SKIP {coin}: no quote")
            continue

        try:
            price = float(row["current_price"])
            updated = row.get("last_updated")
            if not updated:
                raise ValueError("Missing quote timestamp")
            quote_time = dt.datetime.fromisoformat(
                updated.replace("Z", "+00:00")
            )
            age = dt.datetime.now(dt.timezone.utc) - quote_time
            if age.total_seconds() > 3600 or age.total_seconds() < -300:
                raise ValueError("Stale or future quote")
            if price <= 0 or not math.isfinite(price):
                raise ValueError("Invalid quote")

            valid_prices[coin] = price
            eligible, score, m6, m24, vr = signals(coin, row)
            print(
                f"{coin}: A${price:.5f}, "
                f"6h {m6:+.1%}, 24h {m24:+.1%}, "
                f"volume ratio {vr:.2f}"
            )
            if eligible:
                candidates.append((score, coin))
        except Exception as exc:
            print(f"SKIP {coin}: {exc}")

    # Never value a held coin using an absent or stale price.
    missing = set(state["positions"]) - set(valid_prices)
    if missing:
        raise RuntimeError(f"Missing held-coin prices: {sorted(missing)}")

    # Sell first, then consider new opportunities.
    for coin, position in list(state["positions"].items()):
        price = valid_prices[coin]
        change = price / position["entry"] - 1
        if change <= -STOP_LOSS:
            execute(state, coin, "SELL", price, now, "stop-loss signal")
        elif change >= TAKE_PROFIT:
            execute(state, coin, "SELL", price, now, "profit target")

    for score, coin in sorted(candidates, reverse=True):
        if coin not in state["positions"]:
            execute(state, coin, "BUY", valid_prices[coin], now,
                    f"momentum score {score:.3f}")

    holdings = sum(
        p["quantity"] * valid_prices[coin]
        for coin, p in state["positions"].items()
    )
    total = state["cash"] + holdings

    state["snapshots"].append({
        "time": now,
        "cash": round(state["cash"], 4),
        "holdings": round(holdings, 4),
        "total": round(total, 4)
    })
    state["last_prices"] = valid_prices
    save_state(state)

    print(f"\nCash: A${state['cash']:.2f}")
    print(f"Holdings: A${holdings:.2f}")
    print(f"Portfolio: A${total:.2f}")
    print(f"Return: {(total / START - 1):+.2%}")
    print(f"Trades recorded: {len(state['trades'])}")


if __name__ == "__main__":
    main()
