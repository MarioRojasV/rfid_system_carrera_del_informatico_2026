#!/usr/bin/env bash
# Deploy the current origin/main branch to the race-day NAS.
# Usage: ./deploy.sh
#
# Requires: sshpass (brew install hudochenkov/sshpass/sshpass | apt/dnf install sshpass)
set -euo pipefail

SSH_HOST=172.24.160.6
SSH_PORT=1022
SSH_USER=marcos
REMOTE_DIR=/volume1/docker/carrera_informatico_2026

read -rsp "Contraseña SSH de ${SSH_USER}@${SSH_HOST}: " SSH_PASS
echo

ssh_cmd() {
  sshpass -p "$SSH_PASS" ssh -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new \
    "${SSH_USER}@${SSH_HOST}" "$@"
}

echo "==> Sincronizando repo remoto con origin/main"
ssh_cmd "cd '$REMOTE_DIR' && git fetch origin && git reset --hard origin/main"

echo "==> Reconstruyendo y reiniciando contenedores (mongo/redis/server/worker)"
ssh_cmd "export PATH=\$PATH:/usr/local/bin; cd '$REMOTE_DIR' && echo '$SSH_PASS' | sudo -S docker compose up -d --build --remove-orphans"

echo "==> Limpiando imágenes huérfanas"
ssh_cmd "export PATH=\$PATH:/usr/local/bin; echo '$SSH_PASS' | sudo -S docker image prune -f"

echo "==> Estado de los contenedores"
ssh_cmd "export PATH=\$PATH:/usr/local/bin; cd '$REMOTE_DIR' && echo '$SSH_PASS' | sudo -S docker compose ps"

echo "==> Verificando health check"
if curl -sf --max-time 8 "http://${SSH_HOST}:8000/health" > /dev/null; then
  echo "API responde OK en http://${SSH_HOST}:8000"
else
  echo "El API NO respondió en http://${SSH_HOST}:8000 -- revisa 'docker compose logs server'" >&2
  exit 1
fi
