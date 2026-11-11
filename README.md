# Configuration

The app loads settings from `.env` in the project directory. Copy `.env.example`
to `.env`, then fill in `YNAB_TOKEN`, `YNAB_BUDGET_ID`, and
`TELEGRAM_BOT_TOKEN`. The OpenAI-compatible endpoint, model, and web server
settings can also be adjusted there.

Keep `.env` private; it is excluded from Git. Start the server with:

```sh
uv run python bot.py
```

If YNAB returns `401 Unauthorized`, create a new YNAB personal access token,
replace `YNAB_TOKEN` in `.env`, and restart the bot.

To start Gunicorn, expose it through Pinggy, and register the Telegram webhook
in one command, set `TELEGRAM_BOT_TOKEN` in `.env` and run:

```sh
uv run python run_tunnel.py
```

The launcher first validates the YNAB credentials and budget, then starts
`apfel --serve` if the configured local model endpoint is not already running.
It waits for the model before starting Gunicorn, then opens the Pinggy tunnel
and registers the Telegram webhook. Keep the command running while the bot is
in use. Press Ctrl+C to stop the services started by the launcher.

For each transaction, the bot presents paginated account buttons. If an account
was matched from the message, use **Use suggested account** to review and confirm
it, or select a different account. Confirm the transaction before it is saved.
Use **Change account** on the review step to choose a different one. Before
confirming, send a follow-up message such as `payee should be Cafe Amazon`,
`category should be Dining`, or `memo should be lunch` to revise the preview.

To check YNAB access by itself, run:

```sh
uv run python check_ynab.py
```