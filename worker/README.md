> **Render farm in the cloud.** This folder is the PC-worker code, also run
> by GitHub Actions (see `.github/workflows/render-worker.yml`) so videos
> render in the cloud for free — no PC required.
>
> Secrets to set in the repo (Settings → Secrets and variables → Actions):
>
> | Secret | Value |
> |---|---|
> | `AGENT_URL` | brain URL, e.g. https://hamres.onrender.com |
> | `WORKER_SECRET` | the same shared secret as on Render |
> | `BOT_TOKEN` | Telegram bot token |
> | `CHAT_ID` | your Telegram chat id |
> | `PEXELS_API_KEY` | Pexels key |
> | `YT_CLIENT_SECRET` | full JSON content of client_secret.json |
> | `YT_TOKEN` | full JSON content of token.json (from the PC) |
>
> The local PC copy in `faceless youtube\PC` stays fully usable — PC and
> cloud workers cooperate (whoever is free claims the next job).
