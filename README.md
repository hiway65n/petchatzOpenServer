# Notice
This was created using Grok AI (xAI) and is provided AS-IS, feel free to fork and make your own changes.
I created this for some friends who have a bunch of these petchatz devices and was looking for a way to repurpose them.

# PetChatz on-premises hub

This is a local replacement for the original PetChatz cloud (`api2.petchatz.com`). A device that can no longer reach that cloud places audio and video calls through a small hub on the same network. People sign in with a browser. An admin can call any device that is online. A normal user can call only the cameras assigned to them.

Calls work on the local network. From outside the house, the page can go through a reverse proxy or a Cloudflare Tunnel, and the audio and video relay can be this hub's TURN server or a Metered or Cloudflare TURN service. Rough edges that are still open are listed in [docs/status.md](docs/status.md). Do not put live passwords, TURN credentials, private keys, or the hub database in git.

## What you need

- The PetChatz device, on the same LAN as the hub, with SSH available.
- A Linux machine for the hub. The first one was a fresh Ubuntu 24.04 host. It needs to be up whenever someone wants to call.
- A current desktop browser for the person placing the call. The device itself uses the Chromium 69 kiosk already installed on it.

## Start here

1. [Install the hub](docs/install.md).
2. [Set up the device](docs/device.md). Install the watchdog drop-in first, then point the kiosk at the hub. Skipping the drop-in makes the board reboot about every four minutes.
3. [Sign in and call](docs/using.md).
4. When a call works on the LAN, [open it to the outside](docs/off-lan.md). One path is a reverse proxy plus a TURN port forward. The other is a Cloudflare Tunnel for the page and Metered or Cloudflare TURN for the call, which does not need those inbound ports.

The design of the call path is in [docs/architecture.md](docs/architecture.md). What was learned about delay and one-way media is in [docs/call-quality.md](docs/call-quality.md).

## Layout

| Path | Role |
| --- | --- |
| `hub/server.py` | HTTPS signaling hub, login, and roles |
| `hub/login.html` | Sign-in page |
| `hub/calls.html` | Call page for a person |
| `hub/users.html` | Accounts, access, and expiration |
| `hub/devices.html` | Camera settings, removal, treats, scent, idle pictures, outside access, and one-step setup |
| `hub/schedule.html` | Operating hours and daily treat and scent limits |
| `hub/audit.html` | Admin activity log, with username and camera filters |
| `hub/device.html` | Page the device kiosk loads. It places the call and shows Ready, Out of service, or an idle picture |
| `hub/petchatz-hub.service` | systemd unit |
| `hub/config.example.json` | Shape of the config file created at install |
| `deploy/setup.sh` | First-time install on Ubuntu |
| `deploy/enable-offlan.sh` | Set the external name and TURN's public address |
| `deploy/petchatz-ws-no-reboot.conf` | Device drop-in so a missed watchdog restarts the service instead of the board |
| `tools/reload_kiosk.py` | Reload the device page without restarting its services |
| `tools/callcheck.py` | Check that a device returns an SDP offer |

Certificates, `config.json`, `turn.conf`, `hub.db`, `tunnel.env`, and the idle-screen JPEGs are created on the hub and are listed in `.gitignore`.
