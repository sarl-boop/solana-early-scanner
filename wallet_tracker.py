import requests
import os

WEBHOOK = os.getenv("DISCORD_WEBHOOK")

SMART_WALLETS = [
"5Q544fKrFoe6tsdMxtZp8rZn4h3M9Nq7...",
"7GhT3nX9nPq5Uhs6...",
]

def send(msg):
    requests.post(WEBHOOK, json={"content": msg})

def check_wallets():

    for wallet in SMART_WALLETS:

        url = f"https://public-api.solscan.io/account/tokens?account={wallet}"

        r = requests.get(url)

        if r.status_code == 200:

            tokens = r.json()

            for t in tokens[:2]:

                msg = f"""
🐋 SMART WALLET ACTIVITY

Wallet:
{wallet}

Token:
{t['tokenSymbol']}

Balance:
{t['tokenAmount']['uiAmount']}
"""

                send(msg)

check_wallets()
