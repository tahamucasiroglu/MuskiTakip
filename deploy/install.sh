#!/usr/bin/env bash
# Raspberry Pi kurulumu. Kod /opt/muskitakip/app altına açıldıktan sonra çalıştırılır:
#   sudo bash /opt/muskitakip/app/deploy/install.sh
# Tekrar çalıştırmak güvenlidir (güncelleme için de kullanılır). Sistem chromium'una dokunmaz.
set -euo pipefail

APP=/opt/muskitakip/app
VENV=/opt/muskitakip/venv
CHROME_DIR=/opt/muskitakip/chromium
DATA=/var/lib/muskitakip
CHROME_VER="${CHROME_VER:-1:155.0.8059.39-1~deb13u1+rpt1}"

id muskitakip >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin muskitakip
install -d -o muskitakip -g muskitakip "$DATA" "$DATA/backups"

# Projeye özel chromium + chromedriver (aynı sürüm). Sistemdeki chromium'a ve onu kullanan uygulamalara dokunulmaz.
if [ ! -x "$CHROME_DIR/chromedriver" ] || [ "$(cat "$CHROME_DIR/VERSION" 2>/dev/null)" != "$CHROME_VER" ]; then
  tmp=$(mktemp -d)
  (cd "$tmp" && apt-get download "chromium=$CHROME_VER" "chromium-common=$CHROME_VER" "chromium-driver=$CHROME_VER")
  mkdir -p "$tmp/x"
  for f in "$tmp"/*.deb; do dpkg-deb -x "$f" "$tmp/x"; done
  rm -rf "$CHROME_DIR.new"
  cp -a "$tmp/x/usr/lib/chromium" "$CHROME_DIR.new"
  cp "$tmp/x/usr/bin/chromedriver" "$CHROME_DIR.new/chromedriver"
  echo "$CHROME_VER" > "$CHROME_DIR.new/VERSION"
  rm -rf "$CHROME_DIR" && mv "$CHROME_DIR.new" "$CHROME_DIR"
  rm -rf "$tmp"
fi

[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP/requirements.txt"

[ -f /etc/muskitakip.env ] || install -m 644 "$APP/deploy/muskitakip.env" /etc/muskitakip.env
for u in muskitakip-scraper.service muskitakip-scraper.timer muskitakip-web.service \
         muskitakip-backup.service muskitakip-backup.timer muskitakip-telegram.service; do
  install -m 644 "$APP/deploy/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now muskitakip-web.service muskitakip-scraper.timer muskitakip-backup.timer
systemctl restart muskitakip-web.service
# Telegram botu yalnızca gizli ayar dosyası varsa (MUSKI_TG_TOKEN, MUSKI_TG_CLAIM_CODE) çalışır.
if [ -f /etc/muskitakip-telegram.env ]; then
  chown root:muskitakip /etc/muskitakip-telegram.env && chmod 640 /etc/muskitakip-telegram.env
  systemctl enable muskitakip-telegram.service
  systemctl restart muskitakip-telegram.service
fi
echo "Kurulum tamam. Arayüz: http://$(hostname -I | awk '{print $1}'):5090"
