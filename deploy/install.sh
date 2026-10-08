#!/bin/sh
# Run only inside the dedicated Debian VM, as root.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte in der Debian-VM mit sudo ausführen."
    exit 1
fi
cd /opt/volleyball-overlay
test -f main.py
test -f deploy/volleyball-overlay.service
apt-get update
apt-get install -y python3 python3-venv ca-certificates tzdata
if ! id volleyball >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/volleyball-overlay --no-create-home --shell /usr/sbin/nologin volleyball
fi
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
install -m 0644 deploy/volleyball-overlay.service /etc/systemd/system/volleyball-overlay.service
systemctl daemon-reload
systemctl enable volleyball-overlay
systemctl restart volleyball-overlay
systemctl --no-pager status volleyball-overlay
