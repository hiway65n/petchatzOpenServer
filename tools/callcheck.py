"""Place a call from the hub and print the device offer, then hang up.

This does not answer, so it does not prove audio or video. It only shows
whether the device produced an SDP offer.

    HUB_URL=https://192.0.2.10 \
    HUB_USER=petchatz \
    HUB_PASS='the admin password' \
    DEVICE_ID=thedeviceid \
    python3 tools/callcheck.py
"""

import asyncio
import os
import ssl
import sys

import aiohttp

HUB_URL = os.environ.get("HUB_URL", "").rstrip("/")
HUB_USER = os.environ.get("HUB_USER", "")
HUB_PASS = os.environ.get("HUB_PASS", "")
DEVICE_ID = os.environ.get("DEVICE_ID", "")


async def main():
    if not (HUB_URL and HUB_USER and HUB_PASS and DEVICE_ID):
        print("Set HUB_URL, HUB_USER, HUB_PASS, and DEVICE_ID.", file=sys.stderr)
        return 2
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.post(
            HUB_URL + "/login",
            data={"username": HUB_USER, "password": HUB_PASS},
            ssl=ssl_ctx,
            allow_redirects=False,
        ) as response:
            print("login", response.status)
            if response.status not in (200, 302):
                return 1
        async with session.ws_connect(HUB_URL + "/signal?role=user", ssl=ssl_ctx) as ws:
            first = await ws.receive_json()
            print("devices", first.get("devices"))
            await ws.send_json({"type": "call", "device_id": DEVICE_ID})
            offer = None
            for _ in range(15):
                incoming = await asyncio.wait_for(ws.receive_json(), 10)
                kind = incoming.get("type")
                if kind == "offer":
                    offer = incoming
                    for line in incoming.get("sdp", "").splitlines():
                        if (
                            line.startswith("m=video")
                            or line.startswith("b=AS")
                            or "VP8" in line
                        ):
                            print(line)
                    break
                print(kind, str(incoming)[:240])
            call_id = offer["call_id"] if offer else None
            await ws.send_json({"type": "hangup", "call_id": call_id})
            print("hungup", call_id)
            return 0 if offer else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
