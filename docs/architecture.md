# How a call is put together

```
desktop browser                         hub (Ubuntu)                         device kiosk
calls.html                              server.py :443                       device.html
    |  POST /login, cookie                  |                                    |
    |  wss /signal?role=user                |  wss /signal?role=device           |
    | <-------- offer / answer / ice -----> | <-------- offer / answer / ice --> |
    |                                                                                 |
    +---------------- RTP audio/video, direct or via coturn UDP 3478 ---------------+
```

The hub forwards signaling. It does not transcode. Media is peer-to-peer WebRTC between Chromium 69 on the device and a current browser on the computer.

## Boot path on the device

1. `petchatz.service` starts X and Chromium against the local site.
2. Local JavaScript waits for `wss://localhost`.
3. It fetches `https://<petchatz_server>/ping.json`.
4. On HTTP 200 it loads `https://<petchatz_server>/petchatzDevice2.html?device_id&version&hw`.
5. That page (this repo's `device.html`) connects to `/signal?role=device`, takes its relay list from the hello reply, and waits for a ring. While idle it shows Ready, Out of service, or a JPEG saved for every camera.

## Signaling messages

Text JSON on the web socket.

| Type | Direction | Meaning |
| --- | --- | --- |
| `hello` | device to hub, then hub replies | Register `device_id`, `version`, `hw`. The reply carries the LAN ICE servers, that camera's capture settings, whether it is inside an operating window, and which idle pictures exist |
| `service` | hub to device | The window opened or closed. The screen shows Out of service only while idle and closed |
| `screen` | hub to device | The shared ready or out-of-service JPEG was saved or removed |
| `capture` | hub to device | Updated picture size, frame rate, and microphone processing |
| `treat` | hub to device | Dispense one treat. The device page sends `/dispense/treat` on `wss://localhost`. Ignored unless that camera has treats turned on |
| `scent` | hub to device | Run the scent fan for `seconds` (10). The device page enables scent, dispenses, then sends `/stop_scent`. Ignored unless that camera has scent turned on |
| `devices` | hub to user | Devices this account is allowed to see |
| `call` | user to hub | Start a call to `device_id` |
| `ring` | hub to device | Start the offer |
| `ringing` | hub to user | Call id assigned |
| `offer` | device to user, via hub | SDP offer |
| `answer` | user to device, via hub | SDP answer |
| `ice` | either way | One ICE candidate (`candidate`, `sdpMid`, `sdpMLineIndex`) |
| `state` | device to user | Peer connection state, for the status line and the hub log |
| `hangup` | either way | End the call |
| `log` | device to hub | Diagnostic line, written to the journal |
| `error` | hub to user | Not allowed, offline, or already in a call |

The device is the caller in WebRTC terms: it creates the offer when it receives `ring`. The computer answers. That matches the first successful call. Do not reverse it without retesting both directions.

## HTTP routes

| Route | Who |
| --- | --- |
| `GET /ping.json` | Device boot probe. CORS `*` |
| `GET /ice.json` | STUN and TURN server list |
| `GET /petchatzDevice2.html` | Device call page |
| `GET /` | Sign-in page |
| `GET /calls` | Call page for a signed-in person |
| `GET /help` | Caller guide. An admin who is signed in also receives the admin sections. Anyone else does not |
| `GET /logo` | Saved logo picture. Anyone can load it. Missing until an admin saves one |
| `GET /users`, `GET /devices`, `GET /schedule`, `GET /audit`, `GET /settings` | Admin pages. Anyone else is sent back to Calls |
| `POST /login`, `POST /logout`, `GET /api/session` | Session cookie `sid` |
| `GET /api/devices` | Cameras for this session |
| `GET /api/users`, `POST /api/users`, `POST /api/users/delete`, `POST /api/assign`, `POST /api/account` | Admin. Accounts, camera access, enabled flag, expiration, and deleting an account |
| `POST /api/guest` | Admin. Create a guest for one camera with a generated username and password. The password is returned once and is not stored in the activity log |
| `POST /api/password` | Admin. Set a user account's password, or change your own after the current one. A user cannot change a password |
| `POST /api/device`, `POST /api/device/remove` | Admin. Save or remove one camera |
| `POST /api/device/deploy` | Admin. Given a home-network address, install the watchdog drop-in, point the camera at the hub, and reload its screen. Uses the empty root password the camera shipped with |
| `POST /api/treat` | Signed-in user who can call that camera, and only when treats are turned on for it. Refused outside an operating window or after that camera's 24-hour limit |
| `POST /api/scent` | Signed-in user who can call that camera, and only when scent is turned on for it. Refused outside an operating window or after that camera's 24-hour limit |
| `GET /api/hours`, `POST /api/hours`, `POST /api/hours/remove`, `POST /api/hours/limits`, `POST /api/hours/timezone` | Admin. Operating windows, daily treat and scent limits, and the house time zone |
| `GET /api/settings`, `POST /api/settings` | Admin. Public name, tunnel, relays, and the mail server. Secrets are not returned, only whether one is saved and its last four characters |
| `POST /api/mail/test` | Admin. Sends one test message after mail is saved |
| `POST /api/logo`, `POST /api/logo/remove` | Admin. Save or remove the PNG, JPEG, or GIF used as the heading and in guest mail |
| `GET /api/tasks`, `POST /api/tasks`, `POST /api/tasks/run` | Admin. Daily tasks. The built-in task deletes expired guest accounts |
| `GET /api/audit` | Admin. Newest hub actions, filtered with `username` and `device` |
| `GET /api/screen`, `POST /api/screen`, `POST /api/screen/remove` | Admin. Upload or remove the JPEG shown on every camera while it is ready, and the JPEG shown while it is out of service |
| `GET /screen/ready.jpg`, `GET /screen/closed.jpg` | The idle pictures, when an admin has saved them. The camera page loads these |
| `GET /signal?role=device` or `role=user` | Web socket |

Passwords are stored as PBKDF2-HMAC-SHA256. `hub.db` is mode 600, including its journal files. The database lock is a re-entrant lock. A plain lock deadlocked on startup because opening the database also inserted the first admin while holding the same lock. Each message on a signed-in Calls socket checks that the account is still on and not expired. If it is not, the hub closes that socket and ends a call it owned. After several failed sign-ins from the same address, further attempts wait and skip the password check for about 10 minutes.

The `hours` table stores operating windows for a camera: `allow` or `deny`, `weekly` or `once`, the days or the date, and a start and end in minutes. No rows means the camera can be used any time. If any `allow` row exists, the camera is open only inside one of those, and a `deny` row closes it even then. Treat and scent use the same open check. `feature_uses` counts each successful treat or scent for 24 hours. `devices.treat_daily_max` and `scent_daily_max` default to 12. The house time zone is `timezone` in `config.json`. Until it is set, windows use the hub's own clock.

The `audit` table records hub actions: who did it, the address they connected from, the action, a camera id and the name it had then, an optional subject (another account, or a camera address), and a short detail. The password itself is not stored. Rows older than the newest 10,000 are deleted as new ones are written. When activity mail is errors only or every activity, the hub emails that same record. The guest sign-in message is a separate email and is the one that contains the new password.

A guest account is a user with `source` `wizard`, one camera, an end date, and an optional email. The password is hashed the same way as any other account. The printed page and the email use the guest instruction text from Settings. That text can include `{name}`, `{logo}`, `{camera}`, `{expires}`, `{address}`, `{username}`, and `{password}`. Until an admin changes it, the hub uses its starting text. The name at the top of each page is `instance_name` in `config.json`, and it is OpenPetChatz until one is saved. That heading can be the name, a logo picture stored on the hub, or both. `{logo}` in the guest instructions places that picture on the printout and in the email. The `tasks` table holds the daily job that deletes those accounts after the end date. It does not delete any other account. Mail server settings stay in `config.json` with the other secrets.

When Settings uses a Cloudflare Tunnel, the hub runs one `cloudflared`. As a service rewrites `cloudflared.service` and restarts it, including a service created with `cloudflared service install`. Standalone rewrites `petchatz-tunnel` instead. Saving stops the other unit and any extra `cloudflared` process, then checks that one remains. Turning the tunnel off stops both. The token is in a root-only file and is not placed on the command line. See [off-lan.md](off-lan.md).

## ICE

ICE servers depend on who is asking. The device, and a browser that opened the LAN address, get:

- `stun:stun.l.google.com:19302`
- `stun:<hub-lan-ip>:3478`
- `turn:<hub-lan-ip>:3478` over UDP and TCP, with the TURN user from `config.json`

A signed-in browser that opened `public_host` gets the same list with that public name in place of the LAN address. The device never uses the public name, so a missing NAT hairpin does not take the kiosk offline.

An admin can also turn on Metered Open Relay, Cloudflare TURN, or both, from Devices. The hub fetches those credentials and adds the same entries for the camera and for every browser. Keys stay in `config.json`. The settings page shows only the last four characters. If a fetch fails, the list still includes Google STUN and this hub's TURN when that box is checked. Leave this hub's TURN on for calls inside the house. One cloud relay is enough. Both at once makes the camera spend longer gathering addresses.

`/ice.json` requires a login. The device gets its list in the `hello` reply on its socket. coturn's `external-ip` is `public/lan` after `deploy/enable-offlan.sh`, so a relay candidate uses the address an outside browser can send to. Relay ports are UDP 49160–49200. See [off-lan.md](off-lan.md).

A local TURN hop was not the source of the multi-second delay on LAN calls. Leave TURN in place.

## Why the hub speaks TLS 1.2

`server.py` sets `ssl_ctx.maximum_version` to TLS 1.2 before it loads the certificate. Chromium 69 on the device resets newer handshakes (`SSL_PROTOCOL_ERROR` / connection reset). Desktop browsers still connect.
