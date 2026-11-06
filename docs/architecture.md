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
5. That page (this repo's `device.html`) connects to `/signal?role=device`, fetches `/ice.json`, and waits for a ring.

## Signaling messages

Text JSON on the web socket.

| Type | Direction | Meaning |
| --- | --- | --- |
| `hello` | device to hub, then hub replies | Register `device_id`, `version`, `hw`. The reply carries the LAN ICE servers |
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
| `GET /` | Human call page |
| `POST /login`, `POST /logout`, `GET /api/session` | Session cookie `sid` |
| `GET /api/devices` | Devices for this session |
| `GET /api/users`, `POST /api/users`, `POST /api/assign` | Admin |
| `POST /api/password` | Signed-in user, with the current password |
| `GET /signal?role=device` or `role=user` | Web socket |

Passwords are stored as PBKDF2-HMAC-SHA256. The database lock is a re-entrant lock. A plain lock deadlocked on startup because opening the database also inserted the first admin while holding the same lock.

## ICE

ICE servers depend on who is asking. The device, and a browser that opened the LAN address, get:

- `stun:stun.l.google.com:19302`
- `stun:<hub-lan-ip>:3478`
- `turn:<hub-lan-ip>:3478` over UDP and TCP, with the TURN user from `config.json`

A signed-in browser that opened `public_host` gets the same list with that public name in place of the LAN address. The device never uses the public name, so a missing NAT hairpin does not take the kiosk offline.

`/ice.json` requires a login. The device gets its list in the `hello` reply on its socket. coturn's `external-ip` is `public/lan` after `deploy/enable-offlan.sh`, so a relay candidate uses the address an outside browser can send to. Relay ports are UDP 49160–49200. See [off-lan.md](off-lan.md).

A local TURN hop was not the source of the multi-second delay on LAN calls. Leave TURN in place.

## Why the hub speaks TLS 1.2

`server.py` sets `ssl_ctx.maximum_version` to TLS 1.2 before it loads the certificate. Chromium 69 on the device resets newer handshakes (`SSL_PROTOCOL_ERROR` / connection reset). Desktop browsers still connect.
