name: Solana Sniper Scanner Extreme

on:
  workflow_dispatch:
  schedule:
    - cron: "0 */6 * * *"  # toutes les 6h (stable)

concurrency:
  group: solana-sniper-bot
  cancel-in-progress: true  # le plus récent remplace l'ancien (comme tu voulais)

jobs:
  run-bot:
    runs-on: ubuntu-latest
    timeout-minutes: 350  # légèrement < 6h pour éviter coupure brutale

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      # ✅ RESTORE STATE (mémoire bot)
      - name: Restore state
        uses: actions/cache/restore@v4
        with:
          path: state.json
          key: solana-scanner-state-${{ github.ref_name }}

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install requests websockets

      # ✅ RUN BOT
      - name: Run scanner
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          SOLANA_RPC_URL: ${{ secrets.SOLANA_RPC_URL }}
          PUMPPORTAL_API_KEY: ${{ secrets.PUMPPORTAL_API_KEY }}
          SMART_WALLETS: ${{ secrets.SMART_WALLETS }}
          HELD_TOKENS: ${{ secrets.HELD_TOKENS }}
          RUGCHECK_URL: ${{ secrets.RUGCHECK_URL }}
          GOPLUS_URL: ${{ secrets.GOPLUS_URL }}
          HONEYPOT_URL: ${{ secrets.HONEYPOT_URL }}
        run: |
          python scanner.py

      # ✅ SAVE STATE (apprentissage wallets etc.)
      - name: Save state
        if: always()
        uses: actions/cache/save@v4
        with:
          path: state.json
          key: solana-scanner-state-${{ github.ref_name }}-${{ github.run_id }}
