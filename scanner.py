import requests
import os

WEBHOOK = os.environ["https://discord.com/api/webhooks/1480873681528815642/hiOVyFgqrvRwF3ys8Nb-pA6ccSC29PhStzQbcFCURJe1Fx4KkEUNPzAFeELVjBj9xuNU"]

def send(msg):
    requests.post(WEBHOOK, json={"content": msg}, timeout=20)

def check_tokens():
    url = "https://api.dexscreener.com/token-profiles/latest/v1"
    data = requests.get(url, timeout=20).json()

    for item in data[:20]:
        chain = (item.get("chainId") or "").lower()
        if chain != "solana":
            continue

        name = item.get("tokenName") or item.get("header") or "Unknown"
        token_address = item.get("tokenAddress") or "N/A"

        send(
            f"🟡 WATCHLIST\n"
            f"Token: {name}\n"
            f"Address: {token_address}\n"
            f"Reason: New Solana token profile detected\n"
            f"Action: research deeper"
        )

def main():
    check_tokens()

if __name__ == "__main__":
    main()
