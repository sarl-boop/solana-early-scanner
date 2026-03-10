import os
import requests

WEBHOOK = os.environ["https://discord.com/api/webhooks/1480897492198621396/f6DYvrP3PvzfKSOBiPevVNBbRO4Jp7rxSJGOpteo3D2JGQT4UALxSlygzvH_KF4mbAAW"]

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
        description = item.get("description") or ""
        links = item.get("links") or []

        website = ""
        x_link = ""
        discord_link = ""

        for link in links:
            label = (link.get("label") or "").lower()
            url_link = link.get("url") or ""
            if "website" in label and not website:
                website = url_link
            if ("twitter" in label or "x" == label) and not x_link:
                x_link = url_link
            if "discord" in label and not discord_link:
                discord_link = url_link

        msg = (
            f"🟡 WATCHLIST\n"
            f"Token: {name}\n"
            f"Address: {token_address}\n"
            f"Website: {website or 'N/A'}\n"
            f"X: {x_link or 'N/A'}\n"
            f"Discord: {discord_link or 'N/A'}\n"
            f"Reason: New Solana token profile detected on DexScreener\n"
            f"Action: research deeper"
        )

        send(msg)
        sent += 1

        if sent >= 3:
            break

    if sent == 0:
        send("ℹ️ Scan OK: no new Solana token profiles matched the simple watchlist criteria.")

def main() -> None:
    check_tokens()

if __name__ == "__main__":
    main()
