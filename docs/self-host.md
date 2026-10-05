# Self-hosting on your own machine

Run production AnkiGPT on a PC or laptop you already own. Cloudflare Tunnel carries
public HTTPS traffic to it over an outbound connection, so there is no router port
forwarding, your home IP address stays hidden, and HTTPS certificates are handled for
you. Fixed cost: a domain (~$11/year). Everything else is free.

```text
visitor ──HTTPS──> Cloudflare ══tunnel══> cloudflared ──> gunicorn app ──> SQLite file
                                        (on your machine, in Docker)
```

For renting a server instead, see [deploy.md](deploy.md).

## What the machine needs

- Docker Desktop (Windows/macOS) or Docker Engine (Linux).
- To stay on and awake. Visitors get an error page whenever it sleeps, restarts or loses
  its internet connection, and decks being generated at that moment are interrupted.

On Windows, set these yourself:

1. **Settings → System → Power:** set "When plugged in, put my device to sleep after" to
   **Never**. On a laptop, also set the lid-close action to **Do nothing** (Control Panel →
   Power Options → Choose what closing the lid does) and keep it plugged in.
2. **Docker Desktop → Settings → General:** enable **Start Docker Desktop when you sign in**.
   The containers restart on their own once Docker is running.
3. **Settings → Windows Update → Advanced options → Active hours:** cover the hours people
   use the app, so update restarts happen overnight.
4. After a reboot, sign in to Windows so Docker Desktop starts.

## 1. Try it on a temporary address (no account needed)

```bash
docker compose -p ankigpt-home -f deploy/docker-compose.home.yml --profile quick up -d --build
docker compose -p ankigpt-home -f deploy/docker-compose.home.yml logs quick-tunnel | grep trycloudflare
```

This prints a random `https://….trycloudflare.com` address that works from anywhere.
It changes on every restart, so it's for testing only: your users need a permanent
address. Stop it with:

```bash
docker compose -p ankigpt-home -f deploy/docker-compose.home.yml --profile quick rm -sf quick-tunnel
```

You can always reach the app on this machine at http://localhost:5050.

## 2. Use your own domain

1. Buy a domain (Porkbun or Namecheap accept PayPal).
2. Create a free Cloudflare account, choose **Add a domain**, pick the **Free** plan, and at
   your registrar replace the domain's nameservers with the two Cloudflare shows. It
   usually becomes active within an hour.
3. From the repo root in Git Bash:

   ```bash
   deploy/tunnel-setup.sh yourdomain.com
   ```

   It prints a Cloudflare link: open it, sign in, and pick your domain. The script then
   creates the tunnel, points `yourdomain.com` and `www.yourdomain.com` at it, and writes
   `deploy/cloudflared/` (gitignored — it contains the tunnel's secret).
4. Start the permanent tunnel:

   ```bash
   docker compose -p ankigpt-home -f deploy/docker-compose.home.yml --profile named up -d --build
   ```

## 3. Production settings

In `.env`, set `SECRET_KEY`, the Resend SMTP settings, `LEGAL_NAME`, `SUPPORT_EMAIL` and
`LEGAL_JURISDICTION` as listed in [deploy.md](deploy.md#5-production-env), and set
`OPENROUTER_SITE_URL=https://yourdomain.com`. The compose file already sets
`PROXY_FIX_HOPS=1` and `SESSION_COOKIE_SECURE=true`. Leave `DATABASE_URL` at its default
and `OPENROUTER_API_KEY` empty, so each user saves their own key under **My profile**.

All data is one SQLite file in the `ankigpt-home_ankigpt-data` Docker volume on this
machine, so back it up yourself. Take the copy with SQLite's backup API so recent writes
are included:

```bash
docker compose -p ankigpt-home -f deploy/docker-compose.home.yml exec web python -c \
  "import sqlite3; s = sqlite3.connect('/app/instance/ankigpt.db'); d = sqlite3.connect('/app/instance/backup.db'); s.backup(d); d.close(); s.close()"
docker compose -p ankigpt-home -f deploy/docker-compose.home.yml cp web:/app/instance/backup.db ./ankigpt-backup.db
```

## Updating

After changing code, rebuild in place:

```bash
docker compose -p ankigpt-home -f deploy/docker-compose.home.yml --profile named up -d --build
```

## Moving to another machine

Install Docker there, copy the repo, `.env`, `deploy/cloudflared/` and a backup of the
database (above), then stop the stack on the old machine. On the new one, start the
stack once so the volume exists, copy the backup in as `/app/instance/ankigpt.db` with
`docker compose cp`, and restart it. Keep the same `SECRET_KEY`, or saved OpenRouter
keys become unreadable. Never run the named tunnel on two machines at once, or
Cloudflare splits visitors between them.

## Local development alongside

The hosted app publishes only `localhost:5050`. `python run.py` still serves development
on `localhost:5000` with its own database, `instance/ankigpt.db` in the checkout, so
test data never touches the hosted app's volume.
