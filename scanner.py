import asyncio, json, os, time
import requests, websockets

DISCORD = os.environ["DISCORD_WEBHOOK_URL"]

TOKENS = {}
ALERTED = {}

MAX_MC = 5_000_000
MIN_LIQ = 800

# =========================
# UTILS
# =========================

def now(): return int(time.time())

def send(msg):
    try:
        requests.post(DISCORD, json={"content": msg}, timeout=5)
    except:
        pass

def f(x):
    try: return float(x)
    except: return 0.0

# =========================
# SIGNALS X100
# =========================

def burst(t):
    return len(t["buys"]) >= 5

def cluster(t):
    return len(set(t["buyers"])) >= 4

def dev_accum(t):
    for w,c in t["count"].items():
        if c >= 3: return True
    return False

def sniper_trap(t):
    if len(t["buyers"]) < 3 and len(t["buys"]) > 8:
        return True
    return False

# =========================
# SCORE
# =========================

def score(t, mc, liq):
    s = 0

    if mc < 50_000: s += 3
    elif mc < 150_000: s += 2

    if liq > mc * 0.3: s += 2
    if burst(t): s += 2
    if cluster(t): s += 2
    if dev_accum(t): s += 1

    if sniper_trap(t): s -= 5

    return max(0, min(10, s))

# =========================
# CORE
# =========================

async def run():
    uri = "wss://pumpportal.fun/api/data"
    async with websockets.connect(uri) as ws:

        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        await ws.send(json.dumps({"method": "subscribeTokenTrade"}))

        while True:
            data = json.loads(await ws.recv())
            mint = data.get("mint")
            if not mint: continue

            t = TOKENS.setdefault(mint, {
                "buyers": [],
                "buys": [],
                "count": {},
                "first": now()
            })

            if "buy" in str(data).lower():
                w = data.get("traderPublicKey","")
                t["buyers"].append(w)
                t["buys"].append(1)
                t["count"][w] = t["count"].get(w,0)+1

            # fetch dex
            try:
                r = requests.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}", timeout=5)
                pair = r.json()[0]
            except:
                continue

            mc = f(pair.get("marketCap") or pair.get("fdv"))
            liq = f(pair.get("liquidity",{}).get("usd"))

            if mc == 0 or liq < MIN_LIQ or mc > MAX_MC:
                continue

            s = score(t, mc, liq)

            if s >= 6 and mint not in ALERTED:
                ALERTED[mint] = now()

                send(
f"""🟡 GOLD
MC: {int(mc)}
LIQ: {int(liq)}
Score: {s}/10

Buy 25€"""
                )

asyncio.run(run())
