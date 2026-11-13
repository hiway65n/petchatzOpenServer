#!/bin/bash
# Install the on-prem PetChatz hub on a fresh Ubuntu machine.
# Usage, as root:  ./deploy/setup.sh <lan-ip>
# The LAN IP is the address phones and the PetChatz device use to reach this machine.
# Passwords are generated and printed once. They are not stored in git.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root on the hub machine." >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <lan-ip>" >&2
  exit 1
fi

LAN_IP="$1"
if [[ ! "$LAN_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "LAN IP must be an IPv4 address, for example 192.168.1.20" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/petchatz-hub

if [[ -f "$DEST/config.json" ]]; then
  echo "$DEST/config.json already exists. Refusing to rotate passwords." >&2
  echo "To update code only, copy hub/server.py hub/*.html over $DEST and restart petchatz-hub." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-aiohttp coturn openssl sshpass openssh-client

install -d -m 755 "$DEST"
install -m 644 "$ROOT/hub/server.py" "$ROOT/hub/device.html" \
  "$ROOT/hub/login.html" "$ROOT/hub/calls.html" "$ROOT/hub/users.html" \
  "$ROOT/hub/devices.html" "$ROOT/hub/settings.html" "$ROOT/hub/schedule.html" "$ROOT/hub/audit.html" "$ROOT/hub/app.css" "$ROOT/hub/common.js" "$DEST/"
install -m 644 "$ROOT/hub/petchatz-hub.service" /etc/systemd/system/petchatz-hub.service

TURN_PASS="$(openssl rand -hex 16)"
ADMIN_USER="${ADMIN_USER:-petchatz}"
ADMIN_PASS="${ADMIN_PASS:-$(openssl rand -hex 12)}"

python3 - "$DEST/config.json" "$LAN_IP" "$TURN_PASS" "$ADMIN_USER" "$ADMIN_PASS" << 'PY'
import json, sys
path, lan_ip, turn_pass, admin_user, admin_pass = sys.argv[1:]
with open(path, "w", encoding="utf-8") as fh:
    json.dump({
        "lan_ip": lan_ip,
        "turn_user": "petchatz",
        "turn_pass": turn_pass,
        "admin_user": admin_user,
        "admin_pass": admin_pass,
    }, fh, indent=2)
    fh.write("\n")
PY
chmod 600 "$DEST/config.json"

openssl req -x509 -nodes -newkey rsa:2048 \
  -keyout "$DEST/key.pem" -out "$DEST/cert.pem" -days 3650 \
  -subj "/CN=${LAN_IP}" -addext "subjectAltName=IP:${LAN_IP}"
chmod 600 "$DEST/key.pem"

python3 - "$DEST/turn.conf" "$LAN_IP" "$TURN_PASS" << 'PY'
import sys
path, lan_ip, turn_pass = sys.argv[1:]
# external-ip stays on the LAN address until deploy/enable-offlan.sh sets the public one.
open(path, "w", encoding="utf-8").write("""listening-port=3478
listening-ip=0.0.0.0
relay-ip=%s
external-ip=%s
min-port=49160
max-port=49200
fingerprint
lt-cred-mech
realm=petchatz
user=petchatz:%s
user-quota=8
total-quota=16
log-file=/var/log/turnserver.log
simple-log
no-cli
no-tls
no-dtls
no-multicast-peers
""" % (lan_ip, lan_ip, turn_pass))
PY
chmod 600 "$DEST/turn.conf"

if grep -q '^TURNSERVER_ENABLED=' /etc/default/coturn; then
  sed -i 's/^TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn
else
  echo 'TURNSERVER_ENABLED=1' >> /etc/default/coturn
fi

mkdir -p /etc/systemd/system/coturn.service.d
cat > /etc/systemd/system/coturn.service.d/override.conf << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/turnserver -c /opt/petchatz-hub/turn.conf --pidfile=/run/turnserver.pid
EOF

systemctl daemon-reload
systemctl enable coturn petchatz-hub
systemctl restart coturn
systemctl restart petchatz-hub
sleep 1
systemctl is-active coturn petchatz-hub

python3 - << 'PY'
import ssl, urllib.request
ctx = ssl._create_unverified_context()
body = urllib.request.urlopen("https://127.0.0.1/ping.json", context=ctx, timeout=5).read()
print(body.decode())
PY

echo
echo "Hub is listening on https://${LAN_IP}/"
echo "Admin username: ${ADMIN_USER}"
echo "Admin password: ${ADMIN_PASS}"
echo "Save that password now. It is not written anywhere except $DEST/config.json and the hub database."
echo "Browsers must accept the self-signed certificate warning."
echo "Next: point the device at ${LAN_IP}. See docs/device.md."
echo "Outside-LAN calls need docs/off-lan.md after the device is calling on the LAN."
