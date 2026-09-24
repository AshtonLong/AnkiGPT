# Self-hosting on your own machine

Run production AnkiSpark on a PC or laptop you already own. Cloudflare Tunnel carries
public HTTPS traffic to it over an outbound connection, so there is no router port
forwarding, your home IP address stays hidden, and HTTPS certificates are handled for
you. Fixed cost: a domain (~$11/year). Everything else is free.

```text
visitor ──HTTPS──> Cloudflare ══tunnel══> cloudflared ──> gunicorn app ──> Neon Postgres
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
It changes on every restart, so it's for testing only: Stripe webhooks and customers
need a permanent address. Stop it with:

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
`PROXY_FIX_HOPS=1` and `SESSION_COOKIE_SECURE=true`. Stripe go-live is the same as
[deploy.md step 7](deploy.md#7-go-live-with-stripe), with this compose command:

```bash
docker compose -p ankigpt-home -f deploy/docker-compose.home.yml exec web \
  python -m scripts.stripe_setup --webhook-url https://yourdomain.com/billing/webhook
```

## Updating

After changing code, rebuild in place:

```bash
docker compose -p ankigpt-home -f deploy/docker-compose.home.yml --profile named up -d --build
```

## Moving to another machine

Install Docker there, copy the repo, `.env` and `deploy/cloudflared/`, then stop the
stack on the old machine and start it on the new one. Never run the named tunnel on two
machines at once, or Cloudflare splits visitors between them.

## Local development alongside

The hosted app publishes only `localhost:5050`. `python run.py` still serves development
on `localhost:5000`. Point development at a separate Neon branch so test data never
touches production.
