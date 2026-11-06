# Current status

Last checked against a real call on the first unit: video was acceptable in both directions. Audio from the computer to the device was good. Audio from the device back to the computer was delayed, and still usable.

This is a working on-premises hub. Calls work on the LAN, and from outside the house when the proxy and TURN forwards in [off-lan.md](off-lan.md) are in place. The gaps below are still open.

## Works on the local network

- The device finds the hub, comes online, and stays registered.
- Sign-in, separate Calls, People, and Devices pages, and choosing which cameras each user can call.
- Accounts can be turned off or given an end time.
- An admin can send the watchdog change and the hub address to a camera from Devices.
- Treats can be turned on for one camera. They stay off until then.
- A two-way audio and video call between a desktop browser and the device kiosk.
- Two different units on one hub, each with its own name, picture size, frame rate, and microphone processing.
- A local TURN server so the two browsers can exchange media when a direct path is awkward.

## Works from outside the house

- The call page and its web socket, through a reverse proxy such as Zoraxy. See [off-lan.md](off-lan.md).
- The media relay, by forwarding TURN straight to the hub. The proxy does not carry that UDP traffic.

## Not done

- Scent. The device still has that local command. The hub does not send it.
- A still picture on the device screen while nobody is in a call. A looping video is a lower priority than the still picture.
- A one-time password that is generated for an account and expires after a chosen time.
- A diagnostics page that shows one device's recent log lines.
- A mobile app. The pages already talk to the hub with JSON, which is the shape an app would use.
- Replacing the self-signed certificate with a name people can trust, if the proxy is not already presenting one.
- Hardening the device. Stock SSH and the expired kiosk certificate are left as they were, aside from the watchdog drop-in and the server-name change.

## If you run this yourself

- Keep house-specific addresses, device ids, and passwords out of git. The docs use example names. Live config, certificates, and `hub.db` stay on the hub.
- Rotate any password that was ever typed into a chat, a shell history, or a ticket, and do not copy those values into the docs.
- Review device SSH. An empty root password is acceptable only on a trusted LAN you control.
- Re-read [docs/call-quality.md](call-quality.md) before changing capture or SDP. Advice that no longer matches the code should be updated in the same change.

## Do not "tidy" these without a call test

- The hub TLS listener caps the protocol at TLS 1.2. Chromium 69 on the device fails the handshake otherwise.
- `GET /ping.json` sends `Access-Control-Allow-Origin: *`. The device probes the hub from `https://localhost`, so a missing CORS header looks like "server not present" even when the hub returned 200.
- Device page script has to stay compatible with Chromium 69. No optional chaining, no newer JavaScript syntax.
- Do not put `b=AS` bandwidth lines into the SDP. That was tried. The computer-to-device direction still played, and the device stopped sending its own audio and video. See [docs/call-quality.md](call-quality.md).
- Do not set `jitterBufferTarget` or `playoutDelayHint` to 0 on the computer page. A zero jitter buffer made current Chrome drop the device's media.
- Do not restart `petchatz.service` on the device to pick up a page change. Use `tools/reload_kiosk.py`. See [docs/device.md](device.md).
