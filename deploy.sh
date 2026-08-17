#!/usr/bin/env bash
# Deploy the current origin/main branch to the race-day NAS.
# Usage: ./deploy.sh
#
# Requires: sshpass (brew install hudochenkov/sshpass/sshpass | apt/dnf install sshpass)
set -euo pipefail

SSH_HOST_LAN=172.24.160.6       # NAS real: aquí vive Docker y el repo, se llega por SSH
SSH_HOST_TAILSCALE=100.120.91.17 # Mismo NAS, accesible por Tailscale cuando no hay LAN
PUBLIC_HOST=172.24.160.5        # Reverse proxy hacia el NAS, mismo puerto 8000; es lo que usan los clientes (solo LAN)
SSH_PORT=1022
SSH_USER=marcos
REMOTE_DIR=/volume1/docker/carrera_informatico_2026

# Comprueba si un host:puerto acepta conexiones TCP en un plazo corto.
host_reachable() {
  local host="$1"
  timeout 3 bash -c "cat < /dev/null > /dev/tcp/${host}/${SSH_PORT}" 2>/dev/null
}

echo "==> Detectando ruta de acceso al NAS"
if host_reachable "$SSH_HOST_LAN"; then
  SSH_HOST="$SSH_HOST_LAN"
  VIA_LAN=1
  echo "Usando red local (${SSH_HOST})"
elif host_reachable "$SSH_HOST_TAILSCALE"; then
  SSH_HOST="$SSH_HOST_TAILSCALE"
  VIA_LAN=0
  echo "Red local no disponible; usando Tailscale (${SSH_HOST})"
else
  echo "No se pudo contactar al NAS ni por red local (${SSH_HOST_LAN}) ni por Tailscale (${SSH_HOST_TAILSCALE})" >&2
  exit 1
fi

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

echo "==> Verificando health check (directo al NAS)"
if curl -sf --max-time 8 "http://${SSH_HOST}:8000/health" > /dev/null; then
  echo "API responde OK en http://${SSH_HOST}:8000"
else
  echo "El API NO respondió en http://${SSH_HOST}:8000 -- revisa 'docker compose logs server'" >&2
  exit 1
fi

if [ "$VIA_LAN" -eq 1 ]; then
  echo "==> Verificando health check (a través del reverse proxy, ruta real del cliente)"
  if curl -sf --max-time 8 "http://${PUBLIC_HOST}:8000/health" > /dev/null; then
    echo "API responde OK en http://${PUBLIC_HOST}:8000 (vía proxy)"
  else
    echo "El API NO respondió a través de http://${PUBLIC_HOST}:8000 -- revisa el reverse proxy" >&2
    exit 1
  fi
else
  echo "==> Saltando verificación del reverse proxy (${PUBLIC_HOST} solo es accesible por LAN, estamos por Tailscale)"
fi
