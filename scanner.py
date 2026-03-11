import os
import requests
import time

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")

def send(msg):
    try:
        requests.post(WEBHOOK, json={"content": msg})
    except:
        pass


############################################
# SOURCES
############################################

GECKO_NEW = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q=solana"


############################################
# SCORING
############################################

def opportunity(mc, liq, vol):

    score = 0

    if liq > 30000:
        score += 1
    if liq > 60000:
        score += 1
    if liq > 100000:
        score += 1

    if vol > 50000:
        score += 1
    if vol > 150000:
        score += 1
    if vol > 300000:
        score += 1

    if mc < 5000000:
        score += 1
    if mc < 2000000:
        score += 1
    if mc < 800000:
        score += 1

    return score


############################################
# RISK
############################################

def risk(liq, vol):

    if liq < 15000:
        return "HIGH"

    if vol < 20000:
        return "MEDIUM"

    return "LOW"


############################################
# FORMAT
############################################

def format_buy(name, addr, mc, liq, vol, score):

    if score >= 8:
        color = "🟡 GOLD"
        action = "Buy 50€ now"

    elif score >= 6:
        color = "🟢 GREEN"
        action = "Buy 25€ now"

    else:
        return None

    return f"""
{color}

Token: {name}
Address: {addr}

Market cap: ${int(mc):,}
Liquidity: ${int(liq):,}
Volume: ${int(vol):,}

Score: {score}/9

Action: {action}
"""


############################################
# FETCH TOKENS
############################################

def get_tokens():

    tokens = []

    try:
        r = requests.get(GECKO_NEW, timeout=10).json()

        for p in r["data"]:

            name = p["attributes"]["name"]
            addr = p["attributes"]["base_token_address"]

            mc = float(p["attributes"].get("fdv_usd",0))
            liq = float(p["attributes"].get("reserve_in_usd",0))
            vol = float(p["attributes"].get("volume_usd",0))

            tokens.append((name,addr,mc,liq,vol))

    except:
        pass

    return tokens


############################################
# SCAN
############################################

def scan():

    tokens = get_tokens()

    if not tokens:
        send("🤖 SCANNER ACTIVE — No signals detected")
        return

    alerts = 0

    for name,addr,mc,liq,vol in tokens:

        score = opportunity(mc,liq,vol)

        r = risk(liq,vol)

        if r == "HIGH":
            continue

        msg = format_buy(name,addr,mc,liq,vol,score)

        if msg:

            send(msg)

            alerts += 1

        if alerts >= 3:
            break


############################################
# RUN
############################################

if __name__ == "__main__":

    scan()
