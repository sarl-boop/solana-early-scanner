import os
import requests

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]

def send(msg: str) -> None:
    requests.post(
        WEBHOOK,
        json={"content": msg},
        timeout=20,
    )

def check_tokens() -> None:
    url = "https://api.dexscreener.com/token-profiles/latest/v1"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    data = response.json()

    sent = 0

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

        sent += 1
        if sent >= 3:
            break

    if sent == 0:
        send("ℹ️ Scan OK: no new Solana token profiles matched the simple watchlist criteria.")

def main() -> None:
    check_tokens()

if __name__ == "__main__":
    send("TEST BOT OK")
