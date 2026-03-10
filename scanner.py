import os
import requests
import time
import json
from pathlib import Path

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]

STATE_FILE = Path("state.json")

DEX_URL = "https://api.dexscreener.com/token-profiles/latest/v1"

MAX_ALERTS = 2

# ---------------------

def send(msg):
    requests.post(WEBHOOK, json={"content": msg})

# ---------------------

def load_state():

    if not STATE_FILE.exists():
        return {"tokens": {}, "last_status": 0}

    return json.loads(STATE_FILE.read_text())

# ---------------------

def save_state(state):

    STATE_FILE.write_text(json.dumps(state, indent=2))

# ---------------------

def classify(liq, mc, vol):

    if liq > 100000 and vol > 300000 and mc < 3000000:
        return "🟨 GOLD", "BUY priority"

    if liq > 30000 and vol > 100000 and mc < 5000000:
        return "🟢 GREEN", "BUY small"

    return None, None

# ---------------------

def main():

    state = load_state()

    alerts = []

    data = requests.get(DEX_URL).json()

    for token in data[:20]:

        name = token.get("tokenName")

        addr = token.get("tokenAddress")

        try:

            pair = requests.get(
                f"https://api.dexscreener.com/token-pairs/v1/solana/{addr}"
            ).json()[0]

        except:

            continue

        liq = pair.get("liquidity", {}).get("usd", 0)

        mc = pair.get("marketCap", 0)

        vol = pair.get("volume", {}).get("h24", 0)

        signal, action = classify(liq, mc, vol)

        if not signal:
            continue

        if addr in state["tokens"]:
            continue

        state["tokens"][addr] = True

        msg = f"""
{signal}

Token: {name}

MC: ${int(mc):,}
Liq: ${int(liq):,}
Vol24h: ${int(vol):,}

Action: {action}
"""

        alerts.append(msg)

    for m in alerts[:MAX_ALERTS]:
        send(m)

    # ----- hourly status -----

    now = int(time.time())

    if now - state["last_status"] > 3600:

        send("🤖 SCANNER ACTIVE — No signals detected")

        state["last_status"] = now

    save_state(state)


# ---------------------

if __name__ == "__main__":

    main()
