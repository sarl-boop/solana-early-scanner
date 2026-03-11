import requests
import time
import os

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")

def send(msg):
    if WEBHOOK:
        requests.post(WEBHOOK, json={"content": msg})

# ---------------------------------------------------
# Fetch new pools (GeckoTerminal)
# ---------------------------------------------------

def get_new_pools():

    tokens = []

    for page in range(1,11):  # 10 pages ≈ 500 pools

        try:

            url = f"https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page={page}"
            r = requests.get(url,timeout=10)

            if r.status_code != 200:
                continue

            data = r.json()

            for pool in data["data"]:
                tokens.append(pool)

        except:
            continue

        time.sleep(1)

    return tokens


# ---------------------------------------------------
# Pump.fun radar
# ---------------------------------------------------

def pumpfun_signal(token_name, liquidity):

    name = token_name.lower()

    keywords = [
        "dog",
        "cat",
        "pepe",
        "pump",
        "moon",
        "bonk",
        "inu"
    ]

    for k in keywords:
        if k in name:
            if liquidity < 200000:
                return True

    return False


# ---------------------------------------------------
# Dexscreener check
# ---------------------------------------------------

def check_dexscreener(address):

    try:

        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        r = requests.get(url,timeout=10)

        if r.status_code != 200:
            return None

        return r.json()

    except:
        return None


# ---------------------------------------------------
# Rug protection
# ---------------------------------------------------

def rug_score(liquidity,volume):

    if liquidity < 20000:
        return True

    if volume < 5000:
        return True

    return False


# ---------------------------------------------------
# Volume burst detection
# ---------------------------------------------------

def volume_score(volume):

    if volume > 120000:
        return 3

    if volume > 60000:
        return 2

    if volume > 20000:
        return 1

    return 0


# ---------------------------------------------------
# Main scanner
# ---------------------------------------------------

def scan():

    pools = get_new_pools()

    checked = 0

    for pool in pools:

        checked += 1

        try:

            attributes = pool["attributes"]

            base = attributes["base_token"]

            token_name = base["name"]
            token_address = base["address"]

            liquidity = attributes.get("reserve_in_usd",0)
            volume = attributes.get("volume_usd",{}).get("h24",0)
            marketcap = attributes.get("fdv_usd",0)

            if rug_score(liquidity,volume):
                continue

            score = 0

            # volume burst
            score += volume_score(volume)

            # microcap bonus
            if marketcap < 5000000:
                score += 2

            # liquidity bonus
            if liquidity > 100000:
                score += 2

            # pump.fun radar
            if pumpfun_signal(token_name,liquidity):
                score += 2

            # Dexscreener radar
            dex = check_dexscreener(token_address)

            if dex and "pairs" in dex:
                score += 1


            # ---------------------------------------------------
            # Signals
            # ---------------------------------------------------

            if score >= 8:

                send(f"""
🟡 GOLD

Token: {token_name}
MC: ${round(marketcap)}
Liquidity: ${round(liquidity)}
Volume: ${round(volume)}

Score: {score}/10

https://dexscreener.com/solana/{token_address}
""")

            elif score >= 6:

                send(f"""
🟢 GREEN

Token: {token_name}
MC: ${round(marketcap)}
Liquidity: ${round(liquidity)}
Volume: ${round(volume)}

Score: {score}/10

https://dexscreener.com/solana/{token_address}
""")


        except:
            continue

    send(f"🤖 SCANNER ACTIVE — No signals detected | Checked {checked} pools")


# ---------------------------------------------------

if __name__ == "__main__":
    scan()
