# Install the hub

Use a small Ubuntu machine that stays on. The first hub was Ubuntu 24.04 with Python 3.12 and the `python3-aiohttp` apt package (3.9.x). The service listens on port 443, so it runs as root. coturn listens on UDP 3478.

From a checkout of this repo on that machine:

```bash
sudo ./deploy/setup.sh 192.168.1.20
```

Replace `192.168.1.20` with the hub's LAN address. The script installs `python3-aiohttp`, `coturn`, `openssl`, `sshpass`, and an SSH client, writes `/opt/petchatz-hub/`, creates a 10-year self-signed certificate for that IP, generates an admin password and a TURN password, and starts `coturn` and `petchatz-hub`. `sshpass` is what the Devices page uses to sign in to a camera that still has an empty root password.

The admin username defaults to `petchatz`. Set `ADMIN_USER` or `ADMIN_PASS` in the environment before running the script if you want to choose them. If you do not set `ADMIN_PASS`, the script prints a random one once. Store it somewhere outside this repo. The only copies on the hub are `/opt/petchatz-hub/config.json` (mode 600) and the password hash inside `/opt/petchatz-hub/hub.db`.

The script stops if `config.json` is already there, so a second run cannot rotate the live passwords by accident.

## Check

```bash
systemctl status petchatz-hub coturn
python3 -c "import ssl,urllib.request; c=ssl._create_unverified_context(); print(urllib.request.urlopen('https://127.0.0.1/ping.json', context=c).read())"
```

`ping.json` should return `{"status": "ok"}`.

Open `https://<hub-ip>/` from another computer on the LAN and accept the certificate warning. Sign in with the admin account the script printed.

## Update code on a hub that is already installed

`setup.sh` will not overwrite an existing hub. Copy the changed files and restart only the hub service:

```bash
sudo apt-get install -y sshpass
sudo cp hub/server.py hub/device.html hub/login.html hub/calls.html hub/users.html hub/devices.html hub/schedule.html hub/audit.html hub/app.css hub/common.js /opt/petchatz-hub/
sudo systemctl restart petchatz-hub
```

Restarting the hub drops live calls. It does not reboot the device. If `device.html` changed, also reload the kiosk page with [docs/device.md](device.md). People who already have the call page open should refresh. The pages are sent with `Cache-Control: no-store`, but an open tab keeps the old script until reload.

## Files the installer creates and git ignores

| File | Purpose |
| --- | --- |
| `/opt/petchatz-hub/config.json` | LAN IP, TURN user, TURN password, first admin, optional `timezone`, and the outside-access choices from the Devices page |
| `/opt/petchatz-hub/cert.pem` and `key.pem` | TLS certificate for the hub IP |
| `/opt/petchatz-hub/turn.conf` | coturn config, including the TURN password |
| `/opt/petchatz-hub/hub.db` | Accounts, sessions, camera settings, operating windows, treat and scent counts, and the activity log. Mode 600 |
| `/opt/petchatz-hub/screen-ready.jpg` and `screen-closed.jpg` | Optional idle pictures from the Devices page. Missing files mean the camera shows text |

`hub/config.example.json` shows the keys only. Do not copy a live `config.json` into git. Leave `timezone` empty to use the hub's own clock. An admin can set a house time zone, such as `America/Chicago`, on the Schedule page. That value is what operating windows use.

Metered, Cloudflare TURN, and a Cloudflare Tunnel are optional and are filled in on the Devices page. The installer does not download `cloudflared`. When a tunnel token is saved, the hub writes `/opt/petchatz-hub/tunnel.token` (mode 600, gitignored). As a service updates `cloudflared.service`, including a copy installed outside this hub. Standalone updates `petchatz-tunnel` and stops the other. Clearing the token or switching back to a web address stops both. The hub then checks that only one `cloudflared` process is running. It does not restart `petchatz.service` on a camera.

## Firewall

On the LAN, callers need TCP 443 to the hub. Media needs UDP 3478 to the hub, and the usual high UDP ports between the two browsers. If a later off-LAN setup forwards ports, forward those on purpose and then change this document. Do not assume the first LAN install is that setup.

## Certificate warning

The first certificate is self-signed. Every new browser has to click through the warning. The device kiosk is already started with `--ignore-certificate-errors`, so it does not prompt. A public name and a trusted certificate are in [off-lan.md](off-lan.md).

The next step is the device, and the watchdog drop-in comes before the server-name edit. See [device.md](device.md).
