#!/usr/bin/env bash
# Geliştirme bilgisayarından Pi'ye son commit'i gönderir ve kurulumu çalıştırır.
#   bash deploy/deploy.sh kullanici@pi-adresi     (ya da: MUSKI_PI_HOST=kullanici@pi-adresi bash deploy/deploy.sh)
set -euo pipefail
HOST="${1:-${MUSKI_PI_HOST:?Pi adresini ver: bash deploy/deploy.sh kullanici@pi-adresi}}"
cd "$(dirname "$0")/.."
git archive --format=tar HEAD | ssh "$HOST" 'sudo mkdir -p /opt/muskitakip/app && sudo tar -x -C /opt/muskitakip/app'
ssh "$HOST" 'sudo bash /opt/muskitakip/app/deploy/install.sh'
