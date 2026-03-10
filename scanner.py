import requests
import time

WEBHOOK = "https://discord.com/api/webhooks/1480869213722902649/EgV-uIZYYrIs-53RuE8QSCusbpVZ7hCgyq_Lq4-in2obU8dPKYybatIyyg7QiF4248Xy"

def send(msg):
    requests.post(WEBHOOK, json={"content": msg})

def check_tokens():
    url = "https://api.dexscreener.com/latest/dex/tokens/solana"
    data = requests.get(url).json()

    for pair in data.get("pairs", [])[:20]:
        liq = pair.get("liquidity", {}).get("usd", 0)
        mcap = pair.get("fdv", 0)
        vol = pair.get("volume", {}).get("h24", 0)
        name = pair.get("baseToken", {}).get("name")

        if liq > 20000 and vol > 50000 and mcap < 5000000:
            send(f"🟢 BUY SIGNAL\n{name}\nMCAP {mcap}\nLIQ {liq}\nVOL {vol}")

while True:
    check_tokens()
    time.sleep(120)
