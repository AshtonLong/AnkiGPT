#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 22.04/24.04 VPS (RackNerd or any other provider).
# Run on the server as root, after your SSH key is in ~/.ssh/authorized_keys:
#   bash server-setup.sh
set -euo pipefail
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
export DEBIAN_FRONTEND=noninteractive

echo "==> Updating packages"
$SUDO apt-get update -y
$SUDO apt-get upgrade -y
$SUDO apt-get install -y ca-certificates curl ufw unattended-upgrades

echo "==> Installing Docker Engine and the compose plugin"
$SUDO install -m 0755 -d /etc/apt/keyrings
$SUDO curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
$SUDO chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null
$SUDO apt-get update -y
$SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
$SUDO systemctl enable --now docker
[ "$(id -u)" -eq 0 ] || $SUDO usermod -aG docker "$USER"

echo "==> 2 GB swap (headroom for image builds and large PDFs on a 1 GB server)"
if ! swapon --show | grep -q /swapfile; then
  $SUDO fallocate -l 2G /swapfile
  $SUDO chmod 600 /swapfile
  $SUDO mkswap /swapfile
  $SUDO swapon /swapfile
  echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab > /dev/null
  echo 'vm.swappiness=10' | $SUDO tee /etc/sysctl.d/99-swappiness.conf > /dev/null
  $SUDO sysctl -p /etc/sysctl.d/99-swappiness.conf
fi

echo "==> Firewall: SSH, HTTP, HTTPS only"
$SUDO ufw allow OpenSSH
$SUDO ufw allow 80/tcp
$SUDO ufw allow 443/tcp
$SUDO ufw allow 443/udp
$SUDO ufw --force enable

echo "==> SSH: keys only (bots constantly guess root passwords)"
if [ -s "$HOME/.ssh/authorized_keys" ]; then
  printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
    | $SUDO tee /etc/ssh/sshd_config.d/10-ankigpt.conf > /dev/null
  $SUDO systemctl reload ssh 2>/dev/null || $SUDO systemctl reload sshd
else
  echo "   Skipped: no ~/.ssh/authorized_keys yet, so password login stays on."
fi

echo "==> Automatic security updates"
$SUDO dpkg-reconfigure -f noninteractive unattended-upgrades

mkdir -p ~/ankigpt
echo "Done. Next: put the production .env at ~/ankigpt/.env and run deploy/push.sh from your PC."
