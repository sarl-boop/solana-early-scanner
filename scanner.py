import requests
import json
import time

WEBHOOK = "https://discord.com/api/webhooks/1480897492198621396/f6DYvrP3PvzfKSOBiPevVNBbRO4Jp7rxSJGOpteo3D2JGQT4UALxSlygzvH_KF4mbAAW"

STATE_FILE = "state.json"

DEX_API = "https://api.dexscreener.com/latest/dex/pairs/solana"


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"tokens": {}, "last_status": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send(msg):

    data = {"content": msg}

    requests.post(WEBHOOK, json=data)


def classify(liq, mc, vol):

    score = 0

    if liq > 30000:
        score += 1
    if liq > 60000:
        score += 1
    if liq > 100000:
        score += 1

    if vol > 100000:
        score += 1
    if vol > 250000:
        score += 1
    if vol > 500000:
        score += 1

    if mc < 5000000:
        score += 1
    if mc < 3000000:
        score += 1
    if mc < 1500000:
        score += 1

    if score >= 7:
        return "🟨 GOLD", "BUY priority", score

    if score >= 4:
        return "🟢 GREEN", "BUY small", score

    return None, None, score


def main():

    state = load_state()

    r = requests.get(DEX_API)

    pairs = r.json()["pairs"]

    found = False

    for p in pairs[:30]:

        addr = p["baseToken"]["address"]

        if addr in state["tokens"]:
            continue

        name = p["baseToken"].get("name") or p["baseToken"].get("symbol") or "Unknown Token"

        mc = p.get("fdv") or 0
        liq = p.get("liquidity", {}).get("usd", 0)
        vol = p.get("volume", {}).get("h24", 0)

        signal, action, score = classify(liq, mc, vol)

        state["tokens"][addr] = True

        if signal:

            msg = f"""
{signal}

Token: {name}
Address: {addr}

Score: {score}/9
MC: ${int(mc):,}
Liq: ${int(liq):,}
Vol24h: ${int(vol):,}

Action: {action}
"""

            send(msg)

            found = True

    now = int(time.time())

    if not found and now - state.get("last_status", 0) > 3600:

        send("🤖 SCANNER ACTIVE — No signals detected")

        state["last_status"] = now

    save_state(state)


if __name__ == "__main__":
    main()
