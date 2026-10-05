# Deploying AnkiGPT cheaply

A production setup for about $22/year in fixed costs: a budget VPS (RackNerd's 1 GB
plan, ~$11/year) running the Docker image behind Caddy (automatic HTTPS), a domain
(~$11/year), and Resend's free tier for password-reset email. Everything here can be
paid with PayPal; no credit card is needed. There are no variable costs for you:
AnkiGPT is free, and each user's AI calls run on their own OpenRouter key.

```text
browser ──HTTPS──> Caddy (:443, Let's Encrypt) ──> gunicorn app (:8000) ──> SQLite file
                                                         │                  (Docker volume)
                                    OpenRouter (each user's key) · Resend (SMTP)
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
OPENROUTER_SITE_URL=https://yourdomain.com

MAIL_SMTP_HOST=smtp.resend.com
MAIL_SMTP_PORT=465
MAIL_SMTP_USERNAME=resend
MAIL_SMTP_PASSWORD=re_...
MAIL_FROM=AnkiGPT <no-reply@yourdomain.com>

LEGAL_NAME=<your name or business name>
SUPPORT_EMAIL=support@yourdomain.com
LEGAL_JURISDICTION=<Province>, Canada
```

`PROXY_FIX_HOPS=1` and `SESSION_COOKIE_SECURE=true` are set by the production compose
file. Leave `DATABASE_URL` at its default: the SQLite file lives in the `ankigpt-data`
volume. Leave `OPENROUTER_API_KEY` empty so every user has to save their own key under
**My profile**; a server key would be spent by any account that has none.

Choose `SECRET_KEY` once and keep it. It signs sessions and encrypts the OpenRouter
keys users save, so changing it later signs everyone out and makes them enter their
keys again.

## 6. Deploy

From the repo root on your PC (Git Bash):

```bash
deploy/push.sh root@SERVER_IP
```

It copies the working tree (never `.env`, `.venv` or `instance/`), then builds and
restarts the stack on the server. Caddy fetches the HTTPS certificate on first start.
Run the same command to ship every update.

## 7. Check it end to end

Open `https://yourdomain.com`, create an account, save an OpenRouter API key under
**My profile**, and generate a small deck. Then request a password reset to confirm
email delivery.

## Operations

- **Logs:** `docker compose --env-file .env -f deploy/docker-compose.prod.yml logs -f web`
- **Restarts:** containers restart automatically after crashes and reboots.
- **Database and backups:** all data is one SQLite file, `/app/instance/ankigpt.db`, in
  the `ankigpt-data` volume. Nothing backs it up for you, so copy it off the server on a
  schedule. Take the copy with SQLite's backup API so recent writes in the WAL are
  included:

  ```bash
  cd ~/ankigpt
  docker compose --env-file .env -f deploy/docker-compose.prod.yml exec web python -c \
    "import sqlite3; s = sqlite3.connect('/app/instance/ankigpt.db'); d = sqlite3.connect('/app/instance/backup.db'); s.backup(d); d.close(); s.close()"
  docker compose --env-file .env -f deploy/docker-compose.prod.yml cp web:/app/instance/backup.db ./ankigpt-backup.db
  ```

  Then download `ankigpt-backup.db` to your PC with `scp`. About 300 decks fit in 0.5 GB at current sizes, so
  watch the server's disk (`df -h`) as the library grows.
- **Generation during deploys:** a deploy restarts the app and interrupts decks that are
  mid-generation. Users can retry them, but deploy at quiet times.
