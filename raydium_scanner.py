import requests
import os

WEBHOOK = os.getenv("DISCORD_WEBHOOK")

def send(msg):
    requests.post(WEBHOOK, json={"content": msg})

def scan_raydium():

    url = "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"

    r = requests.get(url)

    pools = r.json()["official"]

    for pool in pools[:5]:

        token = pool["baseMint"]

        msg = f"""
💧 NEW LIQUIDITY POOL

Token:
{token}

DEX:
Raydium
"""

        send(msg)

scan_raydium()
