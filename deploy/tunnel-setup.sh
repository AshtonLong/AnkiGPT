#!/usr/bin/env bash
# Create a permanent Cloudflare Tunnel for your domain. Run once, from the repo root
# (Git Bash on Windows works):   deploy/tunnel-setup.sh yourdomain.com
#
# Prerequisite: a free Cloudflare account with the domain added (switch the domain's
# nameservers at your registrar to the two Cloudflare gives you).
#
# Writes deploy/cloudflared/{cert.pem,<tunnel-id>.json,config.yml}. Keep that folder
# private: it holds the tunnel's credentials. Copy it to move hosting to another machine.
set -euo pipefail
DOMAIN="${1:?usage: deploy/tunnel-setup.sh yourdomain.com}"
NAME="${TUNNEL_NAME:-ankispark}"
DIR="deploy/cloudflared"
mkdir -p "$DIR"

# Docker on Windows needs a Windows-style absolute path for the bind mount.
HOST_DIR="$(cd "$DIR" && (pwd -W 2>/dev/null || pwd))"
cf() { MSYS_NO_PATHCONV=1 docker run --rm -v "$HOST_DIR:/home/nonroot/.cloudflared" cloudflare/cloudflared:latest "$@"; }

if [ ! -f "$DIR/cert.pem" ]; then
  echo "==> Authorize Cloudflare: open the URL below, sign in, and pick $DOMAIN"
  cf tunnel login
fi

if ! ls "$DIR"/*.json >/dev/null 2>&1; then
  echo "==> Creating tunnel '$NAME'"
  cf tunnel create "$NAME"
fi
CRED="$(ls "$DIR"/*.json | head -1)"
TUNNEL_ID="$(basename "$CRED" .json)"

echo "==> Pointing $DOMAIN and www.$DOMAIN at the tunnel"
cf tunnel route dns --overwrite-dns "$TUNNEL_ID" "$DOMAIN"
cf tunnel route dns --overwrite-dns "$TUNNEL_ID" "www.$DOMAIN"

cat > "$DIR/config.yml" <<EOF
tunnel: $TUNNEL_ID
credentials-file: /etc/cloudflared/$TUNNEL_ID.json
ingress:
  - hostname: $DOMAIN
    service: http://web:8000
  - hostname: www.$DOMAIN
    service: http://web:8000
  - service: http_status:404
EOF

echo "Done. Start it with:"
echo "  docker compose -p ankigpt-home -f deploy/docker-compose.home.yml --profile named up -d --build"
