#!/usr/bin/env python3
"""Home hub that replaces api2.petchatz.com for a local PetChatz unit."""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import ssl
import threading
import time
import uuid

from aiohttp import WSMsgType, web

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "hub.db")
CONFIG_PATH = os.path.join(BASE, "config.json")
log = logging.getLogger("petchatz-hub")

db_lock = threading.RLock()
db = None

# device_id -> {ws, version, hw, call_id}
devices = {}
# user_id -> set of websockets
user_sockets = {}
# call_id -> {device_id, user_id, user_ws}
calls = {}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_config(cfg):
    temporary = CONFIG_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, CONFIG_PATH)


def clean_public_host(value):
    host = (value or "").strip().lower().rstrip(".")
    if not host:
        return ""
    if ":" in host or "/" in host or len(host) > 253:
        raise ValueError("use a hostname or IPv4 address, without a port")
    if not re.match(r"^[a-z0-9.-]+$", host):
        raise ValueError("that external address has characters the hub cannot use")
    return host


def connect_db():
    global db
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    with db_lock:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assignments (
                user_id INTEGER NOT NULL,
                device_id TEXT NOT NULL,
                UNIQUE(user_id, device_id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires INTEGER NOT NULL
            );
            """
        )
        db.commit()
        count = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if count == 0:
            cfg = load_config()
            add_user(cfg["admin_user"], cfg["admin_pass"], "admin")
            log.info("created admin user %s", cfg["admin_user"])


def hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200000)
    return salt.hex() + ":" + digest.hex()


def check_password(password, stored):
    salt_hex, digest_hex = stored.split(":", 1)
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200000)
    return secrets.compare_digest(digest.hex(), digest_hex)


def add_user(username, password, role):
    with db_lock:
        db.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            (username, hash_password(password), role),
        )
        db.commit()
        return db.execute(
            "SELECT id, username, role FROM users WHERE username = ?", (username,)
        ).fetchone()


def user_by_id(user_id):
    with db_lock:
        return db.execute(
            "SELECT id, username, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def user_by_name(username):
    with db_lock:
        return db.execute(
            "SELECT id, username, password, role FROM users WHERE username = ?",
            (username,),
        ).fetchone()


def assigned_devices(user_id):
    with db_lock:
        rows = db.execute(
            "SELECT device_id FROM assignments WHERE user_id = ?", (user_id,)
        ).fetchall()
    return [row["device_id"] for row in rows]


def session_user(token):
    if not token:
        return None
    now = int(time.time())
    with db_lock:
        row = db.execute(
            "SELECT user_id, expires FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        if row is None or row["expires"] < now:
            return None
        return db.execute(
            "SELECT id, username, role FROM users WHERE id = ?", (row["user_id"],)
        ).fetchone()


def request_user(request):
    return session_user(request.cookies.get("sid"))


def turn_entries(host, cfg):
    user = cfg["turn_user"]
    password = cfg["turn_pass"]
    return [
        {"urls": "stun:%s:3478" % host},
        {
            "urls": "turn:%s:3478?transport=udp" % host,
            "username": user,
            "credential": password,
        },
        {
            "urls": "turn:%s:3478?transport=tcp" % host,
            "username": user,
            "credential": password,
        },
    ]


def ice_servers(audience):
    """LAN callers and the device use the hub's private address.

    A browser that opened the public name gets that name instead. The device
    stays on the private address so a call does not depend on the router
    looping public traffic back into the house.
    """
    cfg = load_config()
    servers = [{"urls": "stun:stun.l.google.com:19302"}]
    public = (cfg.get("public_host") or "").strip()
    lan = (cfg.get("lan_ip") or "").strip()
    if audience == "public" and public:
        servers.extend(turn_entries(public, cfg))
    elif lan:
        servers.extend(turn_entries(lan, cfg))
    return servers


def header_host(value):
    return (value or "").split(",")[0].strip().lower().split(":")[0].rstrip(".")


def request_audience(request):
    """Public ICE only when the browser opened the configured external name.

    Zoraxy may pass that name in Host, X-Forwarded-Host, or not at all. The
    page also sends it as via=. via= is accepted only when it matches the
    configured name, so a caller cannot aim TURN at some other host.
    """
    cfg = load_config()
    public = (cfg.get("public_host") or "").strip().lower().rstrip(".")
    if not public:
        return "lan"
    names = {
        header_host(request.url.host),
        header_host(request.headers.get("X-Forwarded-Host")),
        header_host(request.query.get("via")),
    }
    if public in names:
        return "public"
    return "lan"


def device_snapshot(user):
    allowed = None if user["role"] == "admin" else set(assigned_devices(user["id"]))
    items = []
    seen = set()
    for device_id, info in devices.items():
        seen.add(device_id)
        if allowed is not None and device_id not in allowed:
            continue
        items.append(
            {
                "device_id": device_id,
                "online": info.get("ws") is not None,
                "version": info.get("version") or "",
                "hw": info.get("hw") or "",
                "in_call": bool(info.get("call_id")),
            }
        )
    if allowed:
        for device_id in allowed:
            if device_id not in seen:
                items.append(
                    {
                        "device_id": device_id,
                        "online": False,
                        "version": "",
                        "hw": "",
                        "in_call": False,
                    }
                )
    items.sort(key=lambda item: item["device_id"])
    return items


def can_use_device(user, device_id):
    if user["role"] == "admin":
        return True
    return device_id in assigned_devices(user["id"])


async def send_json(ws, payload):
    if ws is None or ws.closed:
        return
    try:
        await ws.send_str(json.dumps(payload))
    except ConnectionError:
        log.info("socket send failed")


async def push_devices():
    dead = []
    for user_id, sockets in list(user_sockets.items()):
        user = user_by_id(user_id)
        if user is None:
            continue
        payload = {"type": "devices", "devices": device_snapshot(user)}
        for ws in list(sockets):
            if ws.closed:
                dead.append((user_id, ws))
                continue
            await send_json(ws, payload)
    for user_id, ws in dead:
        user_sockets.get(user_id, set()).discard(ws)


async def end_call(call_id, reason):
    call = calls.pop(call_id, None)
    if call is None:
        return
    info = devices.get(call["device_id"])
    if info and info.get("call_id") == call_id:
        info["call_id"] = None
    message = {"type": "hangup", "call_id": call_id, "reason": reason}
    await send_json(call.get("user_ws"), message)
    if info:
        await send_json(info.get("ws"), message)
    await push_devices()


def read_html(name):
    path = os.path.join(BASE, name)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


async def ping(_request):
    response = web.json_response({"status": "ok"})
    # The kiosk page is https://localhost and probes this URL with XHR.
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


async def ice(request):
    # TURN passwords must not be readable by anyone who can open the port.
    if request_user(request) is None:
        raise web.HTTPUnauthorized()
    audience = request_audience(request)
    log.info("ice list for signed-in user via %s", audience)
    return web.json_response({"iceServers": ice_servers(audience)})


async def device_page(_request):
    return web.Response(
        text=read_html("device.html"),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def user_page(_request):
    return web.Response(
        text=read_html("user.html"),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def login(request):
    data = await request.post()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    row = user_by_name(username)
    if row is None or not check_password(password, row["password"]):
        raise web.HTTPFound("/?error=1")
    token = secrets.token_urlsafe(32)
    expires = int(time.time()) + 14 * 24 * 3600
    with db_lock:
        db.execute(
            "INSERT INTO sessions (token, user_id, expires) VALUES (?, ?, ?)",
            (token, row["id"], expires),
        )
        db.commit()
    response = web.HTTPFound("/")
    response.set_cookie(
        "sid", token, max_age=14 * 24 * 3600, httponly=True, secure=True, samesite="Lax"
    )
    return response


async def logout(request):
    token = request.cookies.get("sid")
    if token:
        with db_lock:
            db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            db.commit()
    response = web.HTTPFound("/")
    response.del_cookie("sid")
    return response


async def api_session(request):
    user = request_user(request)
    if user is None:
        return web.json_response({"user": None})
    return web.json_response(
        {"user": {"username": user["username"], "role": user["role"]}}
    )


async def api_devices(request):
    user = request_user(request)
    if user is None:
        raise web.HTTPUnauthorized()
    return web.json_response({"devices": device_snapshot(user)})


async def api_users(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    with db_lock:
        rows = db.execute("SELECT id, username, role FROM users ORDER BY username").fetchall()
        assignments = db.execute(
            "SELECT user_id, device_id FROM assignments"
        ).fetchall()
    by_user = {}
    for item in assignments:
        by_user.setdefault(item["user_id"], []).append(item["device_id"])
    return web.json_response(
        {
            "users": [
                {
                    "username": row["username"],
                    "role": row["role"],
                    "devices": by_user.get(row["id"], []),
                }
                for row in rows
            ]
        }
    )


async def api_create_user(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") or "user"
    device_id = (body.get("device_id") or "").strip()
    if role not in ("user", "admin"):
        raise web.HTTPBadRequest(text="role must be user or admin")
    if not username or len(password) < 6:
        raise web.HTTPBadRequest(text="username and a password of at least 6 characters are required")
    try:
        created = add_user(username, password, role)
    except sqlite3.IntegrityError:
        raise web.HTTPConflict(text="that username already exists")
    if device_id and role == "user":
        with db_lock:
            db.execute(
                "INSERT OR IGNORE INTO assignments (user_id, device_id) VALUES (?, ?)",
                (created["id"], device_id),
            )
            db.commit()
    await push_devices()
    return web.json_response({"ok": True})


async def api_assign(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    username = (body.get("username") or "").strip()
    device_id = (body.get("device_id") or "").strip()
    target = user_by_name(username)
    if target is None or not device_id:
        raise web.HTTPBadRequest(text="username and device_id are required")
    with db_lock:
        db.execute(
            "INSERT OR IGNORE INTO assignments (user_id, device_id) VALUES (?, ?)",
            (target["id"], device_id),
        )
        db.commit()
    await push_devices()
    return web.json_response({"ok": True})


async def api_settings(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    cfg = load_config()
    return web.json_response(
        {
            "public_host": cfg.get("public_host") or "",
            "lan_ip": cfg.get("lan_ip") or "",
        }
    )


async def api_settings_save(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    try:
        public_host = clean_public_host(body.get("public_host"))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    cfg = load_config()
    cfg["public_host"] = public_host
    save_config(cfg)
    log.info("external address set to %s", public_host or "(none)")
    return web.json_response({"ok": True, "public_host": public_host})


async def api_password(request):
    user = request_user(request)
    if user is None:
        raise web.HTTPUnauthorized()
    body = await request.json()
    current = user_by_name(user["username"])
    if not check_password(body.get("old") or "", current["password"]):
        raise web.HTTPForbidden(text="current password is wrong")
    new_password = body.get("new") or ""
    if len(new_password) < 6:
        raise web.HTTPBadRequest(text="new password must be at least 6 characters")
    with db_lock:
        db.execute(
            "UPDATE users SET password = ? WHERE id = ?",
            (hash_password(new_password), user["id"]),
        )
        db.commit()
    return web.json_response({"ok": True})


async def signal(request):
    role = request.query.get("role")
    if role == "user":
        user = request_user(request)
        if user is None:
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        user_sockets.setdefault(user["id"], set()).add(ws)
        audience = request_audience(request)
        await send_json(
            ws,
            {
                "type": "devices",
                "devices": device_snapshot(user),
                "iceServers": ice_servers(audience),
            },
        )
        try:
            await user_loop(ws, user)
        finally:
            user_sockets.get(user["id"], set()).discard(ws)
            for call_id, call in list(calls.items()):
                if call.get("user_ws") is ws:
                    await end_call(call_id, "user disconnected")
        return ws

    if role != "device":
        raise web.HTTPBadRequest(text="role is required")

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    device_id = None
    try:
        device_id = await device_loop(ws)
    finally:
        if device_id and devices.get(device_id, {}).get("ws") is ws:
            call_id = devices[device_id].get("call_id")
            devices[device_id]["ws"] = None
            devices[device_id]["call_id"] = None
            if call_id:
                await end_call(call_id, "device disconnected")
            await push_devices()
    return ws


async def device_loop(ws):
    device_id = None
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
            continue
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            continue
        kind = data.get("type")
        if kind == "hello":
            device_id = (data.get("device_id") or "").strip()
            if not device_id:
                continue
            previous = devices.get(device_id, {})
            if previous.get("ws") and previous.get("ws") is not ws and not previous["ws"].closed:
                await previous["ws"].close()
            devices[device_id] = {
                "ws": ws,
                "version": data.get("version") or "",
                "hw": data.get("hw") or "",
                "call_id": previous.get("call_id"),
            }
            log.info("device online %s version %s", device_id, data.get("version"))
            await send_json(ws, {"type": "hello", "iceServers": ice_servers("lan")})
            await push_devices()
        elif kind == "offer" and device_id:
            call = calls.get(data.get("call_id"))
            if call and call["device_id"] == device_id:
                await send_json(
                    call["user_ws"],
                    {
                        "type": "offer",
                        "call_id": call_id_of(data),
                        "sdp": data.get("sdp") or "",
                    },
                )
        elif kind == "ice" and device_id:
            call = calls.get(data.get("call_id"))
            if call and call["device_id"] == device_id:
                await send_json(
                    call["user_ws"],
                    {
                        "type": "ice",
                        "call_id": data.get("call_id"),
                        "candidate": data.get("candidate"),
                    },
                )
        elif kind == "state" and device_id:
            call = calls.get(data.get("call_id"))
            log.info("device %s call %s state %s", device_id, data.get("call_id"), data.get("state"))
            if call and call["device_id"] == device_id:
                await send_json(
                    call["user_ws"],
                    {
                        "type": "state",
                        "call_id": data.get("call_id"),
                        "state": data.get("state"),
                        "detail": data.get("detail") or "",
                    },
                )
        elif kind == "hangup" and device_id:
            await end_call(data.get("call_id"), "device hung up")
        elif kind == "log":
            log.info("device log: %s", data.get("message"))
    return device_id


def call_id_of(data):
    return data.get("call_id")


async def user_loop(ws, user):
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
            continue
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            continue
        kind = data.get("type")
        if kind == "call":
            await start_call(ws, user, data)
        elif kind == "answer":
            call = calls.get(data.get("call_id"))
            if call and call["user_id"] == user["id"]:
                info = devices.get(call["device_id"])
                if info:
                    await send_json(
                        info.get("ws"),
                        {
                            "type": "answer",
                            "call_id": data.get("call_id"),
                            "sdp": data.get("sdp") or "",
                        },
                    )
        elif kind == "ice":
            call = calls.get(data.get("call_id"))
            if call and call["user_id"] == user["id"]:
                info = devices.get(call["device_id"])
                if info:
                    await send_json(
                        info.get("ws"),
                        {
                            "type": "ice",
                            "call_id": data.get("call_id"),
                            "candidate": data.get("candidate"),
                        },
                    )
        elif kind == "hangup":
            call = calls.get(data.get("call_id"))
            if call and call["user_id"] == user["id"]:
                await end_call(data.get("call_id"), "user hung up")


async def start_call(ws, user, data):
    device_id = (data.get("device_id") or "").strip()
    if not can_use_device(user, device_id):
        await send_json(ws, {"type": "error", "message": "you cannot call that device"})
        return
    info = devices.get(device_id)
    if info is None or info.get("ws") is None or info["ws"].closed:
        await send_json(ws, {"type": "error", "message": "that device is offline"})
        return
    if info.get("call_id"):
        await send_json(ws, {"type": "error", "message": "that device is already in a call"})
        return
    call_id = uuid.uuid4().hex[:12]
    calls[call_id] = {"device_id": device_id, "user_id": user["id"], "user_ws": ws}
    info["call_id"] = call_id
    log.info("call %s user %s device %s", call_id, user["username"], device_id)
    await send_json(ws, {"type": "ringing", "call_id": call_id, "device_id": device_id})
    await send_json(info["ws"], {"type": "ring", "call_id": call_id})
    await push_devices()


def make_app():
    app = web.Application()
    app.add_routes(
        [
            web.get("/ping.json", ping),
            web.get("/ice.json", ice),
            web.get("/petchatzDevice2.html", device_page),
            web.get("/", user_page),
            web.post("/login", login),
            web.post("/logout", logout),
            web.get("/api/session", api_session),
            web.get("/api/devices", api_devices),
            web.get("/api/users", api_users),
            web.post("/api/users", api_create_user),
            web.post("/api/assign", api_assign),
            web.post("/api/password", api_password),
            web.get("/api/settings", api_settings),
            web.post("/api/settings", api_settings_save),
            web.get("/signal", signal),
        ]
    )
    return app


def tls_context(cert_path, key_path):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # The device browser is Chromium 69, which speaks TLS 1.2.
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    connect_db()
    ssl_ctx = tls_context(
        os.path.join(BASE, "cert.pem"), os.path.join(BASE, "key.pem")
    )
    public_cert = os.path.join(BASE, "public-cert.pem")
    public_key = os.path.join(BASE, "public-key.pem")
    if os.path.isfile(public_cert) and os.path.isfile(public_key):
        public_ctx = tls_context(public_cert, public_key)
        public_name = (load_config().get("public_host") or "").strip().lower()

        def select_certificate(sock, server_name, _initial):
            if server_name and server_name.lower() == public_name:
                sock.context = public_ctx

        ssl_ctx.sni_callback = select_certificate
        log.info("public certificate enabled for %s", public_name)
    web.run_app(make_app(), host="0.0.0.0", port=443, ssl_context=ssl_ctx, print=None)


if __name__ == "__main__":
    main()
