# Deploying AnkiSpark cheaply

A production setup for about $22/year in fixed costs: a budget VPS (RackNerd's 1 GB
plan, ~$11/year) running the Docker image behind Caddy (automatic HTTPS), a domain
(~$11/year), the Neon free Postgres plan, and Resend's free tier for password-reset
email. Everything here can be paid with PayPal; no credit card is needed. Variable
costs are the OpenRouter bill and Stripe's per-payment fee.

```text
browser ──HTTPS──> Caddy (:443, Let's Encrypt) ──> gunicorn app (:8000) ──> Neon Postgres
                                                         │
                                          OpenRouter · Stripe · Resend (SMTP)
```

The app idles at ~180 MB for both gunicorn workers, so 1 GB of RAM plus the 2 GB swap
file that `server-setup.sh` adds is enough. Any Ubuntu VPS works the same way (Vultr,
Hetzner, DigitalOcean); only the price changes.

## 1. Accounts (you)

| Service | What to do |
|---|---|
| RackNerd | Buy the 1 GB KVM VPS from racknerd.com/specials (pay with PayPal). Choose **Ubuntu 24.04** and a location near your users. The root password arrives by email. |
| Domain | Buy one at Porkbun or Namecheap (both take PayPal, ~$11/yr for .com). |
| Resend | Sign up, add your domain, add the DNS records it shows, create an API key. |
| Stripe | Activate payments (identity + bank). Set the statement descriptor under Settings → Public details. |

## 2. Install your SSH key on the server

Run this in PowerShell on your PC and type the root password from RackNerd's email
when asked. It's the only time you need the password:

```powershell
type $env:USERPROFILE\.ssh\ankigpt_server.pub | ssh root@SERVER_IP "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

After this, `ssh -i ~/.ssh/ankigpt_server root@SERVER_IP` logs in without a password.

## 3. DNS

At your registrar, add an `A` record for `@` and one for `www`, both pointing at the
server's IP address.

## 4. Prepare the server

```bash
scp -i ~/.ssh/ankigpt_server deploy/server-setup.sh root@SERVER_IP:
ssh -i ~/.ssh/ankigpt_server root@SERVER_IP 'bash server-setup.sh'
```

This installs Docker, adds 2 GB of swap, allows only SSH/HTTP/HTTPS through the
firewall, turns off SSH password login (bots guess root passwords constantly), and
enables automatic security updates.

## 5. Production `.env`

Create `~/ankigpt/.env` on the server (never commit it). Start from `example.env` and set:

```ini
DOMAIN=yourdomain.com
SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
DATABASE_URL=<Neon pooled URL for the production branch>
OPENROUTER_API_KEY=...
OPENROUTER_SITE_URL=https://yourdomain.com

BILLING_ENABLED=true
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=   # printed by the setup script in step 7
STRIPE_PORTAL_CONFIGURATION=   # printed by the setup script in step 7

MAIL_SMTP_HOST=smtp.resend.com
MAIL_SMTP_PORT=465
MAIL_SMTP_USERNAME=resend
MAIL_SMTP_PASSWORD=re_...
MAIL_FROM=AnkiSpark <no-reply@yourdomain.com>

LEGAL_NAME=<your name or business name>
SUPPORT_EMAIL=support@yourdomain.com
LEGAL_JURISDICTION=<Province>, Canada
```

`PROXY_FIX_HOPS=1` and `SESSION_COOKIE_SECURE=true` are set by the production compose
file. Use a separate Neon branch for local development so test data never touches
production.

## 6. Deploy

From the repo root on your PC (Git Bash):

```bash
deploy/push.sh root@SERVER_IP
```

It copies the working tree (never `.env`, `.venv` or `instance/`), then builds and
restarts the stack on the server. Caddy fetches the HTTPS certificate on first start.
Run the same command to ship every update.

## 7. Go live with Stripe

On the server, after the first deploy:

```bash
cd ~/ankigpt
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec web \
  python -m scripts.stripe_setup --webhook-url https://yourdomain.com/billing/webhook
# paste the printed STRIPE_PORTAL_CONFIGURATION and STRIPE_WEBHOOK_SECRET into .env
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec web \
  python -m scripts.reset_billing_ids --yes     # clear test-mode customer ids
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d
```

Then buy a plan with a real card (or PayPal-linked card), confirm Plan & billing shows it, cancel in the
portal, confirm the webhook moves it to "Cancels at period end", and refund yourself
from the Stripe dashboard.

## Operations

- **Logs:** `docker compose --env-file .env -f deploy/docker-compose.prod.yml logs -f web`
- **Restarts:** containers restart automatically after crashes and reboots.
- **Database:** Neon keeps point-in-time history on its own. The free plan has 0.5 GB of
  storage (roughly 300 decks at current sizes); upgrade Neon or move Postgres onto a
  bigger VPS when it fills.
- **Generation during deploys:** a deploy restarts the app and interrupts decks that are
  mid-generation. Their retries are free for users, but deploy at quiet times.
