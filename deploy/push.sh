#!/usr/bin/env bash
# Ship the current working tree to the server and rebuild. Run from the repo root on
# your PC (Git Bash works):  deploy/push.sh root@SERVER_IP
# Secrets never travel with the code: the server keeps its own ~/ankigpt/.env.
set -euo pipefail
TARGET="${1:?usage: deploy/push.sh root@SERVER_IP}"
KEY="${ANKIGPT_SSH_KEY:-$HOME/.ssh/ankigpt_server}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$TARGET")

echo "==> Syncing code to $TARGET"
tar czf - \
  --exclude=.git --exclude=.venv --exclude=venv --exclude=instance --exclude=.env --exclude='.env.*' \
  --exclude=.claude --exclude=__pycache__ --exclude=.pytest_cache --exclude=docs/images \
  . | "${SSH[@]}" 'mkdir -p ~/ankigpt && tar xzf - -C ~/ankigpt'

echo "==> Building and restarting"
"${SSH[@]}" 'cd ~/ankigpt && test -f .env || { echo "Missing ~/ankigpt/.env on the server"; exit 1; }; docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build --remove-orphans && docker image prune -f >/dev/null && docker compose --env-file .env -f deploy/docker-compose.prod.yml ps'
