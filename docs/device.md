# Set up the device

Do these steps in order, or use **Deploy to this camera** on the hub's Devices page. That button does steps 2 through 4 when the camera still accepts SSH as root with an empty password. Install the watchdog drop-in before changing the server name. If the call page loads while the stock unit is still in place, the board reboots about every four minutes.

The device already has the camera, microphone, and a Chromium 69 kiosk. This setup does not replace that software. It tells the kiosk to load the call page from your hub, and it stops the local service from rebooting the board when that page is on screen.

## 1. Sign in

SSH to the device as `root`. On the first unit the password was empty, which is how that firmware shipped. Give it a password, or stop exposing SSH, before the house network is reachable from the internet.

The SSH banner can take about 12 seconds. A connect timeout under about 45 seconds often fails before the banner arrives.

From another computer on the LAN, SSH is the only port that accepts a connection. The device's own port 443 is for the kiosk, not for the hub.

## 2. Install the watchdog drop-in

`petchatz-ws` is the process behind `wss://localhost`. The menu page keeps it alive. The call page used to leave it unattended, and the unit is set to reboot the whole board when that happens.

Two different pings are easy to mix up:

| Message | Where | What it does |
| --- | --- | --- |
| `/ping` | Text frame on `wss://localhost` | Tells `petchatz-ws` to pet its systemd watchdog |
| `GET /ping.json` | HTTPS request to the hub | Tells the menu page that the hub is up |

The stock menu, after it opens the local socket, sends `/ping` every 5 seconds. That code is in `/var/www/http/scripts/deviceSettingsMenu.js`. `petchatz-ws` has `WatchdogSec=20`. If 20 seconds pass with no pet, systemd aborts the process. The stock unit also has `FailureAction=reboot` and `StartLimitAction=reboot`. The abort handler in this binary wedges, systemd cannot finish the stop cleanly, and the board reboots. From the outside that looks like a fresh boot every four minutes, because the boot, the missed pet, and the stuck shutdown add up to about that long.

This repo's call page opens `wss://localhost` and sends `/ping` every 5 seconds, which is what the menu used to do. That pet is required, and it is not a substitute for the drop-in. A missed pet would still reboot the board until the unit is changed.

The drop-in keeps the 20 second watchdog, so a stuck `petchatz-ws` is still restarted. It turns off the two actions that reboot the board, and it shortens the stop timeout so a wedged shutdown is killed instead of sitting there.

From a checkout of this repo, copy the file to the device and install it:

```bash
scp deploy/petchatz-ws-no-reboot.conf root@<device-ip>:/tmp/petchatz-ws-no-reboot.conf
ssh root@<device-ip>
sed -i 's/\r$//' /tmp/petchatz-ws-no-reboot.conf
mkdir -p /etc/systemd/system/petchatz-ws.service.d
cp /tmp/petchatz-ws-no-reboot.conf /etc/systemd/system/petchatz-ws.service.d/override.conf
systemctl daemon-reload
systemctl restart petchatz-ws.service
```

`daemon-reload` has to happen before the restart. The restart applies to `petchatz-ws` only. Do not restart `petchatz.service` here. That unit is the screen.

Check the result before continuing:

```bash
systemctl show petchatz-ws.service -p ActiveState,Result,WatchdogUSec,FailureAction,StartLimitAction
```

You want `ActiveState=active`, `Result=success`, `WatchdogUSec=20s`, `FailureAction=none`, and `StartLimitAction=none`.

If the old process does not exit within 10 seconds, systemd reports a timeout and starts a new one. That is the stop limit in the drop-in. The board should stay up. Continue only after `ActiveState` is `active`.

If the board is already in the reboot loop, sign in during the first minute after it comes back, then run the commands above before it cycles again.

## 3. Point the menu at the hub

File on the device:

`/var/www/http/scripts/deviceSettingsMenu.js`

Keep a copy first:

```bash
cp /var/www/http/scripts/deviceSettingsMenu.js /var/www/http/scripts/deviceSettingsMenu.js.bak
```

Set the server variable to the hub's LAN address, with no scheme and no port:

```javascript
var petchatz_server = "192.168.1.20";
```

Use the same address you passed to `deploy/setup.sh`. Leave this as the LAN address even after you add an outside name. The kiosk should not be sent to the public name. See [off-lan.md](off-lan.md).

On boot the kiosk waits until `wss://localhost` is up, then requests `https://<petchatz_server>/ping.json`. HTTP 200 sends it to `https://<petchatz_server>/petchatzDevice2.html` with the device id, software version, and hardware string. The hub answers `/ping.json` with `Access-Control-Allow-Origin: *` because that request comes from `https://localhost`. Without that header the device console shows status 0 and stays on "server not present" even when the hub log shows HTTP 200.

## 4. Reload the call page

Chromium is started with `--remote-debugging-port=9222` on `127.0.0.1`. Use that to reload. Restarting `petchatz.service` to pick up a page is how the watchdog gets tripped while the board is busy.

Copy `tools/reload_kiosk.py` to the device and run it there. Python 3.5 is enough. Strip CR characters if the copy came from Windows:

```bash
sed -i 's/\r$//' /tmp/reload_kiosk.py
python3 /tmp/reload_kiosk.py
```

The script evaluates `location.reload(true)` in the kiosk. A full start of the kiosk, including a reboot, also loads the edited JavaScript. A debug reload does not wipe the Chromium profile. A full start does (`rm -rf /tmp/chromium` in `.xinitrc`).

## 5. Confirm it stayed up

On the hub:

```bash
journalctl -u petchatz-hub -f
```

After the reload you should see `device online` and `device log: local watchdog on`. `local watchdog off` appears only while `petchatz-ws` is restarting. It should be followed by `local watchdog on` within a few seconds.

On the device, `uptime` should keep growing past five minutes, and `journalctl -u petchatz-ws` should not show `Watchdog timeout`.

The device id is the `device_id` query on the call page, and the id in that hub log line. It is twelve hex characters. Assign that id to a user when you create one.

## What the drop-in does not change

`petchatz-ws` still has to receive `/ping` about every 5 seconds once the call page is up. The page in this repo does that. If you replace `hub/device.html` and drop that socket, the service will be restarted every 20 seconds. The board will stay up, and the kiosk's local commands will flap.

Treats are sent from the call page on the device. When Devices has Treat dispenser on for that camera, the hub asks the page to send `/dispense/treat` on `wss://localhost`. Scent is still not sent.

## Leave these alone

- Do not restart `petchatz.service` to reload a page. Use `tools/reload_kiosk.py`.
- Do not turn the second CPU on. The board has one Cortex-A9 online. Trying to online the other one coincided with a reboot. One core is also why the call has to stay small. See [call-quality.md](call-quality.md).
- Do not drive `wss://localhost` from the hub. That socket is on the device, for the kiosk.

## The unit that was checked

Software `4.2.0rc1`, hostname `imx6dlpetchatz`, kernel `4.9.88-linux-anser`, about 512 MB RAM, Chromium 69 in a 320×240 kiosk. The certificate in `/opt/petchatz/` expired in 2016. The kiosk is started with certificate errors ignored. The hub still has to offer TLS 1.2, because this Chromium resets a TLS 1.3-only handshake.

The live call is the kiosk page. The native `/stream/webrtc-start-chat` and accept-user-offer commands are stubs.
