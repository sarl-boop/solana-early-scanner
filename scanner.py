import asyncio
import json
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional, List

import requests
import websockets

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

SMART_WALLETS = {
    x.strip() for x in os.environ.get("SMART_WALLETS", "").split(",") if x.strip()
}

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEX_TOKEN_API = "https://api.dexscreener.com/tokens/v1/solana/"

STATE_FILE = Path("state.json")

TOKEN_TTL_SECONDS = 7200
ALERT_COOLDOWN_SECONDS = 3600

MAX_MARKET_CAP = 5_000_000
MIN_LIQUIDITY = 15000

STATE = {
    "tokens": {},
    "alerted": {},
    "wallet_stats": {}
}

# =====================================================
# UTILS
# =====================================================

def now():
    return int(time.time())


def send_discord(msg):
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})
    except:
        pass


def load_state():
    global STATE
    if STATE_FILE.exists():
        STATE = json.loads(STATE_FILE.read_text())


def save_state():
    STATE_FILE.write_text(json.dumps(STATE))


# =====================================================
# TOKEN MANAGEMENT
# =====================================================

def ensure_token(mint):

    if mint not in STATE["tokens"]:
        STATE["tokens"][mint] = {
            "first_seen": now(),
            "early_buys": 0,
            "early_sells": 0,
            "buyers": set(),
            "sellers": set(),
            "smart_wallet_hits": set()
        }

    return STATE["tokens"][mint]


# =====================================================
# WALLET ALPHA DISCOVERY
# =====================================================

def update_wallet_stats(wallet, win=False):

    stats = STATE["wallet_stats"].setdefault(wallet, {"wins":0,"trades":0})

    stats["trades"] += 1

    if win:
        stats["wins"] += 1


def is_alpha_wallet(wallet):

    stats = STATE["wallet_stats"].get(wallet)

    if not stats:
        return False

    if stats["trades"] < 5:
        return False

    return stats["wins"] / stats["trades"] > 0.4


# =====================================================
# SCORE
# =====================================================

def compute_score(pair, token):

    mc = pair["fdv"]
    liq = pair["liquidity"]["usd"]

    buys = token["early_buys"]
    sells = token["early_sells"]

    unique_buyers = len(token["buyers"])

    score = 0

    # early volume
    if buys >= 8:
        score += 2

    # buyer dispersion
    if unique_buyers >= 6:
        score += 2

    # buy pressure
    if buys > sells * 2:
        score += 2

    # liquidity strength
    if liq >= mc * 0.7:
        score += 2

    # smart wallets
    score += len(token["smart_wallet_hits"]) * 2

    return score


# =====================================================
# PUMP PROBABILITY
# =====================================================

def pump_probability(token):

    buyers = len(token["buyers"])
    buys = token["early_buys"]

    if buyers >= 10 and buys >= 12:
        return 0.9

    if buyers >= 6:
        return 0.7

    if buyers >= 4:
        return 0.5

    return 0.2


# =====================================================
# DEX
# =====================================================

def get_pair(mint):

    try:
        r = requests.get(DEX_TOKEN_API + mint, timeout=10)
        data = r.json()

        if not data:
            return None

        return data[0]

    except:
        return None


# =====================================================
# ALERT
# =====================================================

def alert(mint, score, pair, token):

    if mint in STATE["alerted"]:
        return

    prob = pump_probability(token)

    if score >= 9:
        signal = "🟡 GOLD-A"
        action = "Buy 50€"

    elif score >= 7:
        signal = "🟡 GOLD-B"
        action = "Buy 25€"

    else:
        return

    msg = f"""

{signal}

Token: {mint}

MC: {pair['fdv']}
Liquidity: {pair['liquidity']['usd']}

Early buys: {token['early_buys']}
Unique buyers: {len(token['buyers'])}

Pump probability: {prob}

Action: {action}

https://dexscreener.com/solana/{mint}

"""

    send_discord(msg)

    STATE["alerted"][mint] = now()


# =====================================================
# WEBSOCKET
# =====================================================

async def websocket_loop():

    while True:

        try:

            async with websockets.connect(PUMPPORTAL_WS) as ws:

                await ws.send('{"method":"subscribeNewToken"}')

                while True:

                    raw = await ws.recv()
                    msg = json.loads(raw)

                    mint = msg.get("mint")

                    if not mint:
                        continue

                    token = ensure_token(mint)

                    wallet = msg.get("traderPublicKey")

                    side = msg.get("txType")

                    if side == "buy":

                        token["early_buys"] += 1

                        if wallet:
                            token["buyers"].add(wallet)

                        if wallet in SMART_WALLETS or is_alpha_wallet(wallet):
                            token["smart_wallet_hits"].add(wallet)

                    elif side == "sell":

                        token["early_sells"] += 1

                        if wallet:
                            token["sellers"].add(wallet)

                    pair = get_pair(mint)

                    if not pair:
                        continue

                    score = compute_score(pair, token)

                    alert(mint, score, pair, token)

        except Exception as e:

            print("WS reconnect", e)

            await asyncio.sleep(5)


# =====================================================
# MAIN
# =====================================================

async def main():

    load_state()

    await websocket_loop()


if __name__ == "__main__":

    asyncio.run(main())
