import requests
import time
import os

WEBHOOK = os.getenv("DISCORD_WEBHOOK")

def send_discord(msg):
    requests.post(WEBHOOK, json={"content": msg})

def check_pump():
    url = "https://frontend-api.pump.fun/coins/latest"

    r = requests.get(url)
    data = r.json()

    for coin in data[:5]:

        name = coin["name"]
        symbol = coin["symbol"]
        mint = coin["mint"]

        message = f"""
🚀 NEW PUMP TOKEN

Name: {name}
Symbol: {symbol}

Contract:
{mint}

https://pump.fun/{mint}
"""

        send_discord(message)

while True:
    check_pump()
    time.sleep(120)
