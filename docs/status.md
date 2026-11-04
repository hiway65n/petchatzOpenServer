# Current status

Last checked against a real call on the first unit: video was acceptable in both directions. Audio from the computer to the device was good. Audio from the device back to the computer was delayed, and still usable.

This is an on-premises LAN system. It is not ready to publish as a finished project.

## Works on the local network

- The device finds the hub, comes online, and stays registered.
- Sign-in, admin and user roles, and assigning a device to a user.
- A two-way audio and video call between a desktop browser and the device kiosk.
- A local TURN server so the two browsers can exchange media when a direct path is awkward.

## Works from outside the house

- The call page and its web socket, through a reverse proxy such as Zoraxy. See [off-lan.md](off-lan.md).
- The media relay, by forwarding TURN straight to the hub. The proxy does not carry that UDP traffic.

## Not done

- Treats and scent. The device still has those local commands. The hub does not send them.
- A polished admin UI. Creating a user and assigning a device works. Editing and removing assignments does not.
- Replacing the self-signed certificate with a name people can trust.
- Hardening the device. Stock SSH and the expired kiosk certificate are left as they were, aside from the one server-name change.
- More than one device has not been tested. The hub can track more than one, but only one unit has been on this software.

## Before this repository is public

- Before publishing, replace any house-specific addresses that were added while testing. The docs should keep using example names.
- Remove or clearly mark anything that only applies to the first lab (specific addresses, the first device id, leftover experiments).
- Rotate any password that was ever typed into a chat, a shell history, or a ticket, and do not copy those values into the docs.
- Review device SSH. An empty root password is acceptable only on a trusted LAN you control, and it should not be the published setup.
- Re-read [docs/call-quality.md](call-quality.md) and delete advice that no longer matches the code.

## Do not "tidy" these without a call test

- The hub TLS listener caps the protocol at TLS 1.2. Chromium 69 on the device fails the handshake otherwise.
- `GET /ping.json` sends `Access-Control-Allow-Origin: *`. The device probes the hub from `https://localhost`, so a missing CORS header looks like "server not present" even when the hub returned 200.
- Device page script has to stay compatible with Chromium 69. No optional chaining, no newer JavaScript syntax.
- Do not put `b=AS` bandwidth lines into the SDP. That was tried. The computer-to-device direction still played, and the device stopped sending its own audio and video. See [docs/call-quality.md](call-quality.md).
- Do not set `jitterBufferTarget` or `playoutDelayHint` to 0 on the computer page. A zero jitter buffer made current Chrome drop the device's media.
- Do not restart `petchatz.service` on the device to pick up a page change. Use `tools/reload_kiosk.py`. See [docs/device.md](device.md).
