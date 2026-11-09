#!/bin/bash
# Point TURN and the outside browser at this house's public address.
# The device keeps using the LAN address already in config.json.
#
#   sudo ./deploy/enable-offlan.sh
#   sudo ./deploy/enable-offlan.sh calls.example.com
#
# With no argument, outside users open https://<public-ip>/ and accept the
# self-signed warning. With a domain, put a real certificate at
# /opt/petchatz-hub/public-cert.pem and public-key.pem (see docs/off-lan.md).
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root on the hub." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/petchatz-hub
PUBLIC_HOST="${1:-}"

if [[ ! -f "$DEST/config.json" ]]; then
  echo "No $DEST/config.json. Run deploy/setup.sh first." >&2
  exit 1
fi

install -m 644 "$ROOT/hub/server.py" "$ROOT/hub/device.html" \
  "$ROOT/hub/login.html" "$ROOT/hub/calls.html" "$ROOT/hub/users.html" \
  "$ROOT/hub/devices.html" "$ROOT/hub/schedule.html" "$ROOT/hub/audit.html" "$ROOT/hub/app.css" "$ROOT/hub/common.js" "$DEST/"
install -m 755 "$ROOT/deploy/refresh-public-ip.sh" "$DEST/refresh-public-ip.sh"

PUBLIC_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org)"
if [[ ! "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Could not learn the public IPv4 address." >&2
  exit 1
fi
if [[ -z "$PUBLIC_HOST" ]]; then
  PUBLIC_HOST="$PUBLIC_IP"
fi

python3 - "$DEST" "$PUBLIC_HOST" "$PUBLIC_IP" << 'PY'
import json, os, sys
dest, public_host, public_ip = sys.argv[1:]
path = os.path.join(dest, "config.json")
with open(path, encoding="utf-8") as fh:
    cfg = json.load(fh)
cfg["public_host"] = public_host
cfg["public_ip"] = public_ip
cfg["turn_min_port"] = int(cfg.get("turn_min_port") or 49160)
cfg["turn_max_port"] = int(cfg.get("turn_max_port") or 49200)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
os.chmod(path, 0o600)
lan = cfg["lan_ip"]
turn = os.path.join(dest, "turn.conf")
open(turn, "w", encoding="utf-8").write("""listening-port=3478
listening-ip=0.0.0.0
relay-ip=%s
external-ip=%s/%s
min-port=%s
max-port=%s
fingerprint
lt-cred-mech
realm=petchatz
user=%s:%s
user-quota=8
total-quota=16
log-file=/var/log/turnserver.log
simple-log
no-cli
no-tls
no-dtls
no-multicast-peers
denied-peer-ip=0.0.0.0-0.255.255.255
denied-peer-ip=10.0.0.0-10.255.255.255
denied-peer-ip=100.64.0.0-100.127.255.255
denied-peer-ip=127.0.0.0-127.255.255.255
denied-peer-ip=169.254.0.0-169.254.255.255
denied-peer-ip=172.16.0.0-172.31.255.255
denied-peer-ip=192.0.2.0-192.0.2.255
denied-peer-ip=192.168.0.0-192.168.255.255
denied-peer-ip=198.18.0.0-198.19.255.255
denied-peer-ip=198.51.100.0-198.51.100.255
denied-peer-ip=203.0.113.0-203.0.113.255
""" % (
    lan, public_ip, lan, cfg["turn_min_port"], cfg["turn_max_port"],
    cfg["turn_user"], cfg["turn_pass"],
))
os.chmod(turn, 0o600)
print("public_host", public_host)
print("public_ip", public_ip)
print("lan_ip", lan)
print("turn_ports", cfg["turn_min_port"], cfg["turn_max_port"])
PY

# The default certificate must name every address the device or a browser uses
# without a separate public certificate. A domain name is served by SNI when
# public-cert.pem exists; this self-signed file remains the fallback.
LAN_IP="$(python3 -c 'import json; print(json.load(open("/opt/petchatz-hub/config.json"))["lan_ip"])')"
SAN="IP:${LAN_IP},IP:${PUBLIC_IP}"
if [[ "$PUBLIC_HOST" != "$PUBLIC_IP" ]]; then
  SAN="${SAN},DNS:${PUBLIC_HOST}"
fi
openssl req -x509 -nodes -newkey rsa:2048 \
  -keyout "$DEST/key.pem" -out "$DEST/cert.pem" -days 3650 \
  -subj "/CN=${PUBLIC_HOST}" -addext "subjectAltName=${SAN}"
chmod 600 "$DEST/key.pem"

install -m 644 "$ROOT/deploy/petchatz-public-ip.service" /etc/systemd/system/petchatz-public-ip.service
install -m 644 "$ROOT/deploy/petchatz-public-ip.timer" /etc/systemd/system/petchatz-public-ip.timer
systemctl daemon-reload
systemctl enable --now petchatz-public-ip.timer
systemctl restart coturn
systemctl restart petchatz-hub
sleep 1
systemctl is-active coturn petchatz-hub

echo
echo "Outside browsers: https://${PUBLIC_HOST}/"
echo "Inside browsers and the device stay on https://${LAN_IP}/"
echo "Forward these to ${LAN_IP} on the router, and do not forward port 22:"
echo "  TCP 443"
echo "  TCP 3478"
echo "  UDP 3478"
echo "  UDP 49160-49200"
echo "Change the admin password from the People section before the forward is open."
echo "Reload the device page with tools/reload_kiosk.py so it picks up the new call page."
