import requests
import os

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]

url = "https://api.dexscreener.com/latest/dex/pairs/solana"

data = requests.get(url).json()

pairs = data["pairs"][:10]

for p in pairs:

    liquidity = p["liquidity"]["usd"]
    token = p["baseToken"]["name"]

    if liquidity > 30000:

        message = f"""
⚡ NEW RAYDIUM LIQUIDITY

Token: {token}
Liquidity: ${liquidity}
Dex: Raydium
"""

        requests.post(WEBHOOK, json={"content": message})
