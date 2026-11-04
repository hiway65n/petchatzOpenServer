# Point the device at the hub

The device does not need a new call stack. Its kiosk browser already speaks WebRTC. On boot it loads a local page, waits until its own web socket on port 443 is up, then asks a configured host for `https://<host>/ping.json`. If that returns HTTP 200, it navigates to `https://<host>/petchatzDevice2.html` with the device id, software version, and hardware string.

The original host was `api2.petchatz.com`. Change it to the hub's LAN IP.

## The one edit

File on the device:

`/var/www/http/scripts/deviceSettingsMenu.js`

Keep a copy first:

```bash
cp /var/www/http/scripts/deviceSettingsMenu.js /var/www/http/scripts/deviceSettingsMenu.js.bak
```

Set the server variable to the hub address, with no scheme and no port:

```javascript
var petchatz_server = "192.168.1.20";
```

Use the same IP you passed to `deploy/setup.sh`. The first lab used a hub at `192.168.50.95` and a device at `192.168.50.58`. Those addresses are an example of a working LAN, not part of the software.

The page that probes the hub is served from `https://localhost`, so the hub's `/ping.json` response includes `Access-Control-Allow-Origin: *`. Without that header the device console shows status 0 and stays on "server not present" even though the hub log shows HTTP 200.

## Reload the kiosk page

The kiosk is `petchatz.service` (a user X session running Chromium). The local web socket process is `petchatz-ws.service`.

`petchatz-ws.service` has `WatchdogSec=20`, `StartLimitBurst=5`, and `FailureAction=reboot`. Restarting `petchatz.service` while the board is busy can trip that watchdog and reboot the device. Do not use `systemctl restart petchatz.service` to pick up a new page.

Chromium is started with `--remote-debugging-port=9222` on `127.0.0.1`. From a shell on the device:

```bash
python3 /tmp/reload_kiosk.py
```

Copy `tools/reload_kiosk.py` to the device first. If you copy it from Windows, strip CR characters (`sed -i 's/\r$//' /tmp/reload_kiosk.py`). The script talks to the debug port and evaluates `location.reload(true)`. Python 3.5 on the device is enough. The script does not use f-strings.

After a reload the hub log should show `device online` with that unit's id. The id comes from the device (`/device/get_id` on its local socket, or the query string of the call page). It looks like `40a5efd62de3`. Assign that id to a user when you create one.

User data for Chromium is wiped on each full start of the kiosk (`rm -rf /tmp/chromium` in `.xinitrc`). A reload through the debug port does not wipe it. A reboot does a full start and loads the edited JavaScript again.

## What the device actually is

Checked on the first unit, software `4.2.0rc1`:

- NXP i.MX6, one Cortex-A9 online, about 512 MB RAM, kernel `4.9.88-linux-anser`.
- Hostname `imx6dlpetchatz`. Camera nodes under `/dev/video*`, ALSA card 0.
- Chromium 69.0.3497.100 in a 320×240 kiosk, with autoplay allowed and a fake media-permission prompt so the camera can start.
- `petchatz-ws` on port 443. From another computer on the LAN, only SSH (port 22) accepted a connection. The kiosk reaches port 443 on the device itself. Do not plan on driving the local socket from the hub.
- The native `/stream/webrtc-start-chat` and accept-user-offer commands are stubs. The live call is the Chromium page, not those commands. `/stream/webrtc-start-silent-mode` still contains a native WebRTC offer path and was not used.
- SSH on the first unit is OpenSSH 7.5. The banner can take around 12 seconds, so short connect timeouts fail before the banner arrives. Use a timeout of about 45 seconds. That unit accepted `root` with an empty password. That is a property of the shipped firmware, not a recommended setup. Shut it down or give it a password before this system is reachable from outside the LAN.
- The TLS certificate shipped in `/opt/petchatz/` expired in 2016. The kiosk ignores certificate errors. The hub still has to offer TLS 1.2; Chromium 69 resets a TLS 1.3-only handshake.

Leave the second CPU alone. Trying to online it coincided with a reboot. One core is a reason the call has to stay small. See [call-quality.md](call-quality.md).

## Watchdog

`petchatz-ws` is a systemd notify service with a 20 second watchdog. The stock menu page pets it by sending `/ping` on `wss://localhost` every 5 seconds. The call page has to keep doing that after it leaves the menu. If the pings stop, systemd aborts `petchatz-ws`. The unit's `FailureAction=reboot` and `StartLimitAction=reboot` then reboot the board, which showed up as a reboot about every four minutes.

`deploy/petchatz-ws-no-reboot.conf` is a drop-in for the device. It leaves the 20 second watchdog in place so a stuck process is restarted, and it turns off the actions that reboot the board. The call page opens `wss://localhost` and sends `/ping` every 5 seconds.

## Local commands that are not part of a call

The kiosk's local socket still answers plain-text paths such as `/device/get_id`, `/get_software_version`, `/settings/get_treat_count`, and `/network/get_status`. Treat and scent commands exist on the device and are not wired through the hub yet.
