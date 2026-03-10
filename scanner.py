import os
import re
import requests

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]

DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"

BAD_WORDS = {
    "test", "official", "pump", "100x", "1000x", "presale", "casino",
    "bet", "airdrop", "giveaway", "free", "elon", "moon", "lfg"
}

def send(msg: str) -> None:
    requests.post(WEBHOOK, json={"content": msg}, timeout=20)

def clean_name(item: dict) -> str:
    name = (item.get("tokenName") or "").strip()
    if not name:
        return ""
    if name.startswith("http://") or name.startswith("https://"):
        return ""
    if "cdn.dexscreener.com" in name.lower():
        return ""
    return name

def extract_links(item: dict):
    links = item.get("links") or []
    website = ""
    x_link = ""
    discord_link = ""

    for link in links:
        label = (link.get("label") or "").lower()
        url = (link.get("url") or "").strip()
        if not url:
            continue

        if "website" in label and not website:
            website = url
        elif ("twitter" in label or label == "x") and not x_link:
            x_link = url
        elif "discord" in label and not discord_link:
            discord_link = url

    return website, x_link, discord_link

def looks_bad(name: str, website: str, x_link: str, discord_link: str) -> bool:
    low = name.lower()

    if not name:
        return True
    if len(name) < 3 or len(name) > 28:
        return True
    if re.search(r"https?://", name):
        return True
    if sum(ch.isdigit() for ch in name) > 4:
        return True
    if any(word in low for word in BAD_WORDS):
        return True

    # pas de projet sérieux sans site
    if not website:
        return True

    # au moins une présence sociale
    if not (x_link or discord_link):
        return True

    return False

def classify(item: dict) -> tuple[str, str]:
    score = 0

    if item["website"]:
        score += 2
    if item["x_link"]:
        score += 1
    if item["discord_link"]:
        score += 1
    if item["source"] == "boost":
        score += 2

    if score >= 5:
        return "🟨 GOLD", "buy small starter position"
    if score >= 3:
        return "🟢 GREEN", "buy small starter position"
    return "🔴 RED", "avoid"

def get_candidates():
    items = []

    r1 = requests.get(DEX_PROFILES, timeout=20)
    r1.raise_for_status()
    profiles = r1.json()

    for item in profiles[:30]:
        if (item.get("chainId") or "").lower() != "solana":
            continue

        name = clean_name(item)
        website, x_link, discord_link = extract_links(item)

        items.append({
            "name": name,
            "token_address": item.get("tokenAddress") or "",
            "website": website,
            "x_link": x_link,
            "discord_link": discord_link,
            "source": "profile",
        })

    r2 = requests.get(DEX_BOOSTS, timeout=20)
    r2.raise_for_status()
    boosts = r2.json()

    for item in boosts[:30]:
        if (item.get("chainId") or "").lower() != "solana":
            continue

        name = clean_name(item)
        website, x_link, discord_link = extract_links(item)

        items.append({
            "name": name,
            "token_address": item.get("tokenAddress") or "",
            "website": website,
            "x_link": x_link,
            "discord_link": discord_link,
            "source": "boost",
        })

    seen = set()
    out = []

    for item in items:
        addr = item["token_address"]
        if not addr or addr in seen:
            continue
        seen.add(addr)
        out.append(item)

    return out

def main():
    candidates = get_candidates()
    alerts = []

    for item in candidates:
        name = item["name"]
        website = item["website"]
        x_link = item["x_link"]
        discord_link = item["discord_link"]

        if looks_bad(name, website, x_link, discord_link):
            # on n’envoie RED que si le token est boosté, sinon silence
            if item["source"] == "boost" and name:
                alerts.append(
                    f"🔴 RED\n"
                    f"Token: {name}\n"
                    f"Address: {item['token_address']}\n"
                    f"Reason: boosted but weak / risky profile\n"
                    f"Action: avoid"
                )
            continue

        color, action = classify(item)

        if color == "🔴 RED":
            continue

        alerts.append(
            f"{color}\n"
            f"Token: {name}\n"
            f"Address: {item['token_address']}\n"
            f"Source: {item['source']}\n"
            f"Website: {website or 'N/A'}\n"
            f"X: {x_link or 'N/A'}\n"
            f"Discord: {discord_link or 'N/A'}\n"
            f"Action: {action}"
        )

    # pas de déchet : max 2 alertes utiles par run, sinon silence
    for msg in alerts[:2]:
        send(msg)

if __name__ == "__main__":
    main()
