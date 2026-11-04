#!/bin/bash
# Update coturn when the house public IP changes.
# Installed at /opt/petchatz-hub/refresh-public-ip.sh and run by systemd.
# Does nothing when the address is unchanged, so a timer tick does not drop a call.
set -euo pipefail

DEST=/opt/petchatz-hub
NEW="$(curl -4 -fsS --max-time 10 https://api.ipify.org || true)"
if [[ ! "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "public IP lookup failed" >&2
  exit 0
fi

ACTION="$(python3 - "$DEST" "$NEW" << 'PY'
import json, os, sys
dest, new_ip = sys.argv[1:]
cfg_path = os.path.join(dest, "config.json")
turn_path = os.path.join(dest, "turn.conf")
with open(cfg_path, encoding="utf-8") as fh:
    cfg = json.load(fh)
old = (cfg.get("public_ip") or "").strip()
if not old or old == new_ip:
    print("same")
    raise SystemExit(0)
lan = cfg["lan_ip"]
text = open(turn_path, encoding="utf-8").read()
needle = "external-ip=%s/%s" % (old, lan)
if needle not in text:
    raise SystemExit("turn.conf has no external-ip line for the previous address")
open(turn_path, "w", encoding="utf-8").write(text.replace(needle, "external-ip=%s/%s" % (new_ip, lan), 1))
os.chmod(turn_path, 0o600)
host = (cfg.get("public_host") or "").strip()
regen = "keep"
if host == old:
    cfg["public_host"] = new_ip
    regen = "regen"
cfg["public_ip"] = new_ip
with open(cfg_path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
os.chmod(cfg_path, 0o600)
print(regen)
PY
)"

if [[ "$ACTION" == "same" ]]; then
  exit 0
fi

if [[ "$ACTION" == "regen" ]]; then
  LAN_IP="$(python3 -c 'import json; print(json.load(open("/opt/petchatz-hub/config.json"))["lan_ip"])')"
  openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$DEST/key.pem" -out "$DEST/cert.pem" -days 3650 \
    -subj "/CN=${NEW}" -addext "subjectAltName=IP:${LAN_IP},IP:${NEW}"
  chmod 600 "$DEST/key.pem"
fi

systemctl restart coturn
systemctl restart petchatz-hub
echo "public IP is now ${NEW}; coturn and the hub were restarted"
