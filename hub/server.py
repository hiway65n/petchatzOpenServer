#!/usr/bin/env python3
"""Home hub that replaces api2.petchatz.com for a local PetChatz unit."""

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import smtplib
import sqlite3
import ssl
import subprocess
import threading
import time
from contextvars import ContextVar
from email.message import EmailMessage
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from aiohttp import ClientTimeout, WSMsgType, web

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "hub.db")
CONFIG_PATH = os.path.join(BASE, "config.json")
log = logging.getLogger("petchatz-hub")

db_lock = threading.RLock()
db = None

# device_id -> {ws, version, hw, call_id}
devices = {}
# device_id -> last treat request time
treat_sent_at = {}
# device_id -> last scent request time
scent_sent_at = {}
SCENT_SECONDS = 10
TASK_DELETE_GUESTS = "delete-expired-guests"
GUEST_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
DEFAULT_INSTANCE_NAME = "OpenPetChatz"
DEFAULT_GUEST_TEMPLATE = """You can call one camera: {camera}
This account stops at {expires}.

{address}

Sign in with these:
Username: {username}
Password: {password}

After you sign in, the page is Calls. Choose the camera and press Call.
The other pages are not shown for this account.
When the time above has passed, this sign-in stops working."""
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HOUSE_ZONES = (
    "America/Chicago",
    "America/New_York",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "Etc/UTC",
)
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
                role TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                expires_at INTEGER
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
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL DEFAULT 320,
                height INTEGER NOT NULL DEFAULT 240,
                frame_rate INTEGER NOT NULL DEFAULT 12,
                echo_cancel INTEGER NOT NULL DEFAULT 1,
                noise_suppression INTEGER NOT NULL DEFAULT 1,
                treats_enabled INTEGER NOT NULL DEFAULT 0,
                scent_enabled INTEGER NOT NULL DEFAULT 0,
                treat_daily_max INTEGER NOT NULL DEFAULT 12,
                scent_daily_max INTEGER NOT NULL DEFAULT 12
            );
            CREATE TABLE IF NOT EXISTS hours (
                id INTEGER PRIMARY KEY,
                device_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                repeat TEXT NOT NULL,
                days TEXT NOT NULL DEFAULT '',
                start_min INTEGER NOT NULL,
                end_min INTEGER NOT NULL,
                on_date TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS feature_uses (
                id INTEGER PRIMARY KEY,
                device_id TEXT NOT NULL,
                feature TEXT NOT NULL,
                at INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY,
                at INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                device_id TEXT NOT NULL DEFAULT '',
                nickname TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                remote TEXT NOT NULL DEFAULT ''
            );
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(devices)")}
        if "echo_cancel" not in columns:
            db.execute("ALTER TABLE devices ADD COLUMN echo_cancel INTEGER NOT NULL DEFAULT 1")
        if "noise_suppression" not in columns:
            db.execute("ALTER TABLE devices ADD COLUMN noise_suppression INTEGER NOT NULL DEFAULT 1")
        if "treats_enabled" not in columns:
            db.execute("ALTER TABLE devices ADD COLUMN treats_enabled INTEGER NOT NULL DEFAULT 0")
        if "scent_enabled" not in columns:
            db.execute("ALTER TABLE devices ADD COLUMN scent_enabled INTEGER NOT NULL DEFAULT 0")
        if "treat_daily_max" not in columns:
            db.execute("ALTER TABLE devices ADD COLUMN treat_daily_max INTEGER NOT NULL DEFAULT 12")
        if "scent_daily_max" not in columns:
            db.execute("ALTER TABLE devices ADD COLUMN scent_daily_max INTEGER NOT NULL DEFAULT 12")
        if "last_ip" not in columns:
            db.execute("ALTER TABLE devices ADD COLUMN last_ip TEXT NOT NULL DEFAULT ''")
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "enabled" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        if "expires_at" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN expires_at INTEGER")
        audit_columns = {row["name"] for row in db.execute("PRAGMA table_info(audit)")}
        if "remote" not in audit_columns:
            db.execute("ALTER TABLE audit ADD COLUMN remote TEXT NOT NULL DEFAULT ''")
        if "email" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "source" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                hour INTEGER NOT NULL DEFAULT 3,
                minute INTEGER NOT NULL DEFAULT 0,
                last_run INTEGER,
                last_result TEXT NOT NULL DEFAULT '',
                armed_at INTEGER
            )
            """
        )
        task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        if "armed_at" not in task_columns:
            db.execute("ALTER TABLE tasks ADD COLUMN armed_at INTEGER")
        existing_task = db.execute(
            "SELECT id FROM tasks WHERE id = ?", (TASK_DELETE_GUESTS,)
        ).fetchone()
        if existing_task is None:
            db.execute(
                """
                INSERT INTO tasks (id, title, enabled, hour, minute)
                VALUES (?, 'Delete expired guest accounts', 0, 3, 0)
                """,
                (TASK_DELETE_GUESTS,),
            )
        db.commit()
        count = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if count == 0:
            cfg = load_config()
            add_user(cfg["admin_user"], cfg["admin_pass"], "admin")
            log.info("created admin user %s", cfg["admin_user"])
    protect_db()


def protect_db():
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = DB_PATH + suffix
        if os.path.exists(path):
            os.chmod(path, 0o600)


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


async def hash_password_async(password):
    return await asyncio.get_running_loop().run_in_executor(None, hash_password, password)


async def check_password_async(password, stored):
    return await asyncio.get_running_loop().run_in_executor(None, check_password, password, stored)


LOGIN_WINDOW = 600
LOGIN_LIMIT = 8
login_failures = {}


def too_many_logins(ip):
    count, start = login_failures.get(ip or "", (0, 0))
    return count >= LOGIN_LIMIT and time.monotonic() - start <= LOGIN_WINDOW


def note_login_failure(ip):
    key = ip or ""
    now = time.monotonic()
    count, start = login_failures.get(key, (0, now))
    if now - start > LOGIN_WINDOW:
        count, start = 0, now
    login_failures[key] = (count + 1, start)
    if len(login_failures) > 400:
        expired = [item for item, (_, began) in login_failures.items() if now - began > LOGIN_WINDOW]
        for item in expired:
            login_failures.pop(item, None)


def note_login_success(ip):
    login_failures.pop(ip or "", None)


def add_user(username, password, role, already_hashed=False):
    stored = password if already_hashed else hash_password(password)
    with db_lock:
        db.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            (username, stored, role),
        )
        db.commit()
        return db.execute(
            "SELECT id, username, role, enabled, expires_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()


def user_by_id(user_id):
    with db_lock:
        return db.execute(
            "SELECT id, username, role, enabled, expires_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


def user_by_name(username):
    with db_lock:
        return db.execute(
            "SELECT id, username, password, role, enabled, expires_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()


def account_open(row, now=None):
    if row is None or not row["enabled"]:
        return False
    expires_at = row["expires_at"]
    if expires_at is not None and int(expires_at) <= int(now or time.time()):
        return False
    return True


def other_open_admins(except_id):
    now = int(time.time())
    with db_lock:
        rows = db.execute(
            """
            SELECT id FROM users
            WHERE role = 'admin' AND enabled = 1
              AND (expires_at IS NULL OR expires_at > ?)
              AND id != ?
            """,
            (now, except_id),
        ).fetchall()
    return len(rows) > 0


def assigned_devices(user_id):
    with db_lock:
        rows = db.execute(
            "SELECT device_id FROM assignments WHERE user_id = ?", (user_id,)
        ).fetchall()
    return [row["device_id"] for row in rows]


def clean_device_ids(raw):
    if not isinstance(raw, list):
        raise ValueError("devices must be a list")
    cleaned = []
    for item in raw:
        device_id = str(item or "").strip()
        if not device_id:
            continue
        if len(device_id) > 64 or any(ord(ch) < 32 for ch in device_id):
            raise ValueError("a device id is not valid")
        if device_id not in cleaned:
            cleaned.append(device_id)
    return cleaned


def replace_assignments(user_id, device_ids):
    with db_lock:
        db.execute("DELETE FROM assignments WHERE user_id = ?", (user_id,))
        for device_id in device_ids:
            db.execute(
                "INSERT INTO assignments (user_id, device_id) VALUES (?, ?)",
                (user_id, device_id),
            )
        db.commit()


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
        user = db.execute(
            "SELECT id, username, role, enabled, expires_at FROM users WHERE id = ?",
            (row["user_id"],),
        ).fetchone()
        if not account_open(user, now):
            db.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))
            db.commit()
            return None
        return user


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


_relay_cache = {}


def turn_choices(cfg):
    return {
        "local": cfg.get("turn_local", True) is not False,
        "metered_enabled": bool(cfg.get("metered_enabled")),
        "metered_domain": (cfg.get("metered_domain") or "").strip(),
        "metered_api_key": cfg.get("metered_api_key") or "",
        "cloudflare_turn_enabled": bool(cfg.get("cloudflare_turn_enabled")),
        "cloudflare_turn_key_id": (cfg.get("cloudflare_turn_key_id") or "").strip(),
        "cloudflare_turn_token": cfg.get("cloudflare_turn_token") or "",
    }


def flatten_ice(servers):
    entries = []
    for item in servers or []:
        if not isinstance(item, dict):
            continue
        urls = item.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        username = item.get("username") or ""
        credential = item.get("credential") or ""
        for url in urls or []:
            if not isinstance(url, str) or url.split("?", 1)[0].endswith(":53"):
                continue
            entry = {"urls": url}
            if username:
                entry["username"] = username
                entry["credential"] = credential
            entries.append(entry)
    return entries


async def fetch_json(method, url, headers=None, payload=None):
    timeout = ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=headers, json=payload) as response:
            if response.status >= 400:
                raise ValueError("status %s" % response.status)
            return await response.json(content_type=None)


async def cached_relay(name, ttl, factory):
    now = time.monotonic()
    hit = _relay_cache.get(name)
    if hit and hit[0] > now:
        return hit[1]
    try:
        servers = flatten_ice(await factory())
    except Exception:
        log.warning("relay %s is not available", name)
        if hit:
            return hit[1]
        return []
    if servers:
        _relay_cache[name] = (now + ttl, servers)
    return servers


async def metered_ice(domain, api_key):
    url = "https://%s/api/v1/turn/credentials?apiKey=%s" % (domain, urllib.parse.quote(api_key, safe=""))

    async def factory():
        body = await fetch_json("GET", url)
        if isinstance(body, dict):
            return body.get("iceServers") or []
        return body

    return await cached_relay("metered:" + domain, 1800, factory)


async def cloudflare_ice(key_id, token):
    url = "https://rtc.live.cloudflare.com/v1/turn/keys/%s/credentials/generate-ice-servers" % key_id

    async def factory():
        body = await fetch_json(
            "POST",
            url,
            headers={"Authorization": "Bearer " + token},
            payload={"ttl": 43200},
        )
        return (body or {}).get("iceServers") or []

    return await cached_relay("cloudflare:" + key_id, 3600, factory)


async def ice_servers(audience):
    """LAN callers and the device use the hub's private address.

    A browser that opened the public name gets that name instead. The device
    stays on the private address so a call does not depend on the router
    looping public traffic back into the house. Cloud relays are added for
    every caller when an admin has turned them on.
    """
    cfg = load_config()
    choices = turn_choices(cfg)
    servers = [{"urls": "stun:stun.l.google.com:19302"}]
    public = (cfg.get("public_host") or "").strip()
    lan = (cfg.get("lan_ip") or "").strip()
    if choices["local"]:
        if audience == "public" and public:
            servers.extend(turn_entries(public, cfg))
        elif lan:
            servers.extend(turn_entries(lan, cfg))
    if choices["metered_enabled"] and choices["metered_domain"] and choices["metered_api_key"]:
        servers.extend(await metered_ice(choices["metered_domain"], choices["metered_api_key"]))
    if choices["cloudflare_turn_enabled"] and choices["cloudflare_turn_key_id"] and choices["cloudflare_turn_token"]:
        servers.extend(await cloudflare_ice(choices["cloudflare_turn_key_id"], choices["cloudflare_turn_token"]))
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


def device_profile(device_id):
    with db_lock:
        row = db.execute(
            "SELECT nickname, width, height, frame_rate, echo_cancel, noise_suppression, treats_enabled, scent_enabled, treat_daily_max, scent_daily_max, last_ip FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
    if row is None:
        return {
            "nickname": "",
            "width": 320,
            "height": 240,
            "frame_rate": 12,
            "echo_cancel": True,
            "noise_suppression": True,
            "treats": False,
            "scent": False,
            "treat_daily_max": 12,
            "scent_daily_max": 12,
            "last_ip": "",
        }
    return {
        "nickname": row["nickname"] or "",
        "width": row["width"],
        "height": row["height"],
        "frame_rate": row["frame_rate"],
        "echo_cancel": bool(row["echo_cancel"]),
        "noise_suppression": bool(row["noise_suppression"]),
        "treats": bool(row["treats_enabled"]),
        "scent": bool(row["scent_enabled"]),
        "treat_daily_max": int(row["treat_daily_max"]),
        "scent_daily_max": int(row["scent_daily_max"]),
        "last_ip": row["last_ip"] or "",
    }


def note_device_ip(device_id, ip):
    ip = clean_ip(ip)
    if not device_id or not ip:
        return
    remember_device(device_id)
    with db_lock:
        db.execute("UPDATE devices SET last_ip = ? WHERE device_id = ?", (ip, device_id))
        db.commit()


def like_pattern(value):
    text = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + text + "%"


client_addr = ContextVar("client_addr", default="")


def clean_ip(value):
    text = (value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text or len(text) > 45:
        return ""
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return ""
    return text


def peer_is_local(value):
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def client_ip(request):
    """The address of the person, including through the house proxy or a tunnel.

    A browser that reached the hub directly keeps the connection address. A
    connection from this machine or another private address uses the visitor
    address the proxy added.
    """
    peer = clean_ip(getattr(request, "remote", "") or "")
    if not peer_is_local(peer):
        return peer
    for header in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
        raw = request.headers.get(header) or ""
        if header == "X-Forwarded-For":
            raw = raw.split(",")[0]
        forwarded = clean_ip(raw)
        if forwarded:
            return forwarded
    return peer


@web.middleware
async def remember_client(request, handler):
    token = client_addr.set(client_ip(request))
    try:
        return await handler(request)
    finally:
        client_addr.reset(token)


def record_audit(username, role, action, device_id="", nickname=None, subject="", detail="", remote=None):
    try:
        if nickname is None and device_id:
            nickname = device_profile(device_id)["nickname"]
        if remote is None:
            remote = client_addr.get()
        with db_lock:
            db.execute(
                """
                INSERT INTO audit (at, username, role, action, device_id, nickname, subject, detail, remote)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    (username or "")[:80],
                    (role or "")[:20],
                    (action or "")[:80],
                    (device_id or "")[:80],
                    (nickname or "")[:80],
                    (subject or "")[:80],
                    (detail or "")[:500],
                    (remote or "")[:45],
                ),
            )
            db.execute(
                """
                DELETE FROM audit
                WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 10000)
                """
            )
            db.commit()
    except Exception:
        log.exception("audit write failed")
        return
    queue_audit_email(
        username, role, action, device_id, nickname or "", subject, detail, remote or ""
    )


def audit_is_error(action):
    text = (action or "").lower()
    return "fail" in text or "refus" in text


def queue_audit_email(username, role, action, device_id, nickname, subject, detail, remote):
    try:
        cfg = load_config()
        mode = cfg.get("audit_email") or "off"
        if mode not in ("all", "errors"):
            return
        if mode == "errors" and not audit_is_error(action):
            return
        recipient = (cfg.get("audit_email_to") or "").strip()
        if not recipient or not mail_ready(cfg):
            return
        lines = [
            instance_name(cfg) + " activity",
            "",
            "When: " + format_when(datetime.now(hub_zone())),
            "Who: " + (username or "unknown") + (" (" + role + ")" if role else ""),
            "Action: " + (action or ""),
        ]
        if device_id or nickname:
            lines.append("Camera: " + (nickname or device_id))
        if subject:
            lines.append("Subject: " + subject)
        if detail:
            lines.append("Detail: " + detail)
        if remote:
            lines.append("From: " + remote)
        loop = asyncio.get_running_loop()
        loop.create_task(deliver_mail(recipient, instance_name(cfg) + ": " + (action or "activity"), "\n".join(lines)))
    except RuntimeError:
        return
    except Exception:
        log.exception("activity mail was not queued")


def mail_ready(cfg):
    return bool((cfg.get("smtp_host") or "").strip() and (cfg.get("smtp_from") or "").strip())


def smtp_port_value(cfg):
    try:
        port = int(cfg.get("smtp_port") or 587)
    except (TypeError, ValueError):
        return 587
    if 1 <= port <= 65535:
        return port
    return 587


def send_mail(cfg, recipient, subject, body):
    host = (cfg.get("smtp_host") or "").strip()
    sender = (cfg.get("smtp_from") or "").strip()
    if not host or not sender:
        raise ValueError("mail is not configured")
    security = cfg.get("smtp_security") or "starttls"
    if security not in ("starttls", "ssl", "none"):
        security = "starttls"
    try:
        port = int(cfg.get("smtp_port") or (465 if security == "ssl" else 587))
    except (TypeError, ValueError):
        port = 587
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        client = smtplib.SMTP(host, port, timeout=30)
    try:
        client.ehlo()
        if security == "starttls":
            client.starttls()
            client.ehlo()
        user = (cfg.get("smtp_user") or "").strip()
        if user:
            client.login(user, cfg.get("smtp_password") or "")
        client.send_message(message)
    finally:
        try:
            client.quit()
        except Exception:
            pass


async def deliver_mail(recipient, subject, body):
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, send_mail, load_config(), recipient, subject, body
        )
    except Exception as exc:
        log.warning("mail to %s failed: %s", recipient, exc)
        return False
    log.info("mail sent to %s", recipient)
    return True


def clean_email(value, label):
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        raise ValueError(label + " needs an email address")
    return text


def clean_smtp_password(value):
    text = (value or "").strip()
    if any(ord(ch) < 32 for ch in text):
        raise ValueError("the mail password cannot include control characters")
    if len(text) > 200:
        raise ValueError("the mail password is too long")
    return text


def guest_code(length):
    return "".join(secrets.choice(GUEST_ALPHABET) for _ in range(length))


def end_of_house_day(date_text):
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (date_text or "").strip())
    if match is None:
        raise ValueError("choose an end date")
    try:
        start = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=hub_zone())
    except ValueError:
        raise ValueError("that end date is not a real date")
    end = start + timedelta(days=1) - timedelta(seconds=1)
    stamp = int(end.timestamp())
    if stamp <= int(time.time()):
        raise ValueError("choose an end date that is still ahead")
    if stamp > 4000000000:
        raise ValueError("that end date is too far away")
    return stamp


def instance_name(cfg=None):
    if cfg is None:
        cfg = load_config()
    name = (cfg.get("instance_name") or "").strip()
    return name or DEFAULT_INSTANCE_NAME


def guest_template_text(cfg):
    text = cfg.get("guest_template")
    if text is None or not str(text).strip():
        return DEFAULT_GUEST_TEMPLATE
    return str(text).strip()


def html_text(value):
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def branded_html(name):
    return read_html(name).replace("{{name}}", html_text(instance_name()))


def clean_instance_name(value):
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) > 40 or any(ord(ch) < 32 for ch in text):
        raise ValueError("the name must be 40 characters or less")
    return text


def clean_guest_template(value):
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""
    if len(text) > 8000:
        raise ValueError("the guest instructions are too long")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        raise ValueError("the guest instructions cannot include control characters")
    if "{username}" not in text or "{password}" not in text:
        raise ValueError("the instructions need {username} and {password}")
    return text.strip()


def address_block(cfg):
    host = visit_address(cfg)
    if host:
        return "Open this address in a web browser:\n" + host
    return (
        "Open the web address written below. The hub does not have a public name yet.\n"
        "Web address: _______________________________"
    )


def guest_instructions(cfg, username, password, camera, when):
    values = {
        "name": instance_name(cfg),
        "camera": camera,
        "expires": when,
        "address": address_block(cfg),
        "username": username,
        "password": password,
    }
    text = guest_template_text(cfg)
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def visit_address(cfg):
    host = (cfg.get("public_host") or "").strip()
    if not host:
        return ""
    return "https://%s/" % host


def remove_account_rows(user_id):
    with db_lock:
        db.execute("DELETE FROM assignments WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()


def delete_expired_guest_accounts():
    now = int(time.time())
    removed = []
    with db_lock:
        rows = db.execute(
            """
            SELECT id, username FROM users
            WHERE source = 'wizard' AND expires_at IS NOT NULL AND expires_at <= ?
            """,
            (now,),
        ).fetchall()
        for row in rows:
            removed.append((row["id"], row["username"]))
            db.execute("DELETE FROM assignments WHERE user_id = ?", (row["id"],))
            db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
            db.execute("DELETE FROM users WHERE id = ?", (row["id"],))
        db.commit()
    return removed


def task_is_due(row, now):
    if not row["enabled"]:
        return False
    local = now.astimezone(hub_zone())
    scheduled = local.replace(hour=int(row["hour"]), minute=int(row["minute"]), second=0, microsecond=0)
    if local < scheduled:
        return False
    armed = row["armed_at"]
    if armed is not None and scheduled.timestamp() <= int(armed):
        return False
    last = row["last_run"]
    if last is not None and int(last) >= int(scheduled.timestamp()):
        return False
    return True


async def finish_guest_cleanup(removed, actor, role):
    if not removed:
        return "No expired guest accounts."
    for user_id, username in removed:
        await close_user_sockets(user_id)
        record_audit(actor, role, "Deleted guest account", subject=username, detail="expired")
    count = len(removed)
    if count == 1:
        return "Removed 1 guest account."
    return "Removed %d guest accounts." % count


async def run_task(task_id, actor, role):
    if task_id != TASK_DELETE_GUESTS:
        raise ValueError("that task does not exist")
    result = await finish_guest_cleanup(delete_expired_guest_accounts(), actor, role)
    with db_lock:
        db.execute(
            "UPDATE tasks SET last_run = ?, last_result = ? WHERE id = ?",
            (int(time.time()), result, task_id),
        )
        db.commit()
    log.info("task %s: %s", task_id, result)
    return result


async def run_due_tasks():
    now = hub_now()
    with db_lock:
        rows = db.execute(
            "SELECT id, enabled, hour, minute, last_run, armed_at FROM tasks"
        ).fetchall()
    for row in rows:
        if task_is_due(row, now):
            await run_task(row["id"], "task", "hub")


async def task_loop():
    while True:
        try:
            await run_due_tasks()
        except Exception:
            log.exception("task check failed")
        await asyncio.sleep(30)


def remember_device(device_id):
    with db_lock:
        db.execute(
            "INSERT OR IGNORE INTO devices (device_id, nickname, width, height, frame_rate) VALUES (?, '', 320, 240, 12)",
            (device_id,),
        )
        db.commit()


def saved_device_ids():
    with db_lock:
        rows = db.execute("SELECT device_id FROM devices").fetchall()
    return [row["device_id"] for row in rows]


def device_record(device_id, info=None):
    profile = device_profile(device_id)
    access = camera_access(device_id)
    info = info or {}
    return {
        "device_id": device_id,
        "nickname": profile["nickname"],
        "width": profile["width"],
        "height": profile["height"],
        "frame_rate": profile["frame_rate"],
        "echo_cancel": profile["echo_cancel"],
        "noise_suppression": profile["noise_suppression"],
        "treats": profile["treats"],
        "scent": profile["scent"],
        "open": access["open"],
        "schedule_message": access["schedule_message"],
        "treat_daily_max": access["treat_daily_max"],
        "scent_daily_max": access["scent_daily_max"],
        "treat_used": access["treat_used"],
        "scent_used": access["scent_used"],
        "treat_blocked": access["treat_blocked"],
        "scent_blocked": access["scent_blocked"],
        "online": info.get("ws") is not None,
        "version": info.get("version") or "",
        "hw": info.get("hw") or "",
        "in_call": bool(info.get("call_id")),
        "last_ip": profile["last_ip"],
    }


def capture_payload(device_id):
    profile = device_profile(device_id)
    return {
        "type": "capture",
        "width": profile["width"],
        "height": profile["height"],
        "frameRate": profile["frame_rate"],
        "echoCancellation": profile["echo_cancel"],
        "noiseSuppression": profile["noise_suppression"],
    }


def clean_capture(body):
    nickname = (body.get("nickname") or "").strip()
    if len(nickname) > 40:
        raise ValueError("nickname must be 40 characters or less")
    if any(ord(ch) < 32 for ch in nickname):
        raise ValueError("nickname cannot include control characters")
    try:
        width = int(body.get("width"))
        height = int(body.get("height"))
        frame_rate = int(body.get("frame_rate"))
    except (TypeError, ValueError):
        raise ValueError("width, height, and frame rate must be whole numbers")
    if not 160 <= width <= 1280 or not 120 <= height <= 720:
        raise ValueError("use a picture from 160x120 through 1280x720")
    if not 5 <= frame_rate <= 30:
        raise ValueError("use a frame rate from 5 through 30")
    echo_cancel = clean_flag(body.get("echo_cancel"), "echo cancellation")
    noise_suppression = clean_flag(body.get("noise_suppression"), "noise suppression")
    treats = clean_flag(body.get("treats"), "treat dispenser")
    scent = clean_flag(body.get("scent"), "scent")
    return nickname, width, height, frame_rate, echo_cancel, noise_suppression, treats, scent


def hub_zone_name():
    configured = (load_config().get("timezone") or "").strip()
    if configured:
        return configured
    tzinfo = datetime.now().astimezone().tzinfo
    return getattr(tzinfo, "key", "") or "Etc/UTC"


def hub_zone():
    try:
        return ZoneInfo(hub_zone_name())
    except ZoneInfoNotFoundError:
        log.warning("unknown timezone %s", hub_zone_name())
        return ZoneInfo("Etc/UTC")


def hub_now():
    return datetime.now(hub_zone())


def parse_days(text):
    days = set()
    for part in (text or "").split(","):
        part = part.strip()
        if part == "":
            continue
        days.add(int(part))
    return days


def format_clock(minute):
    if minute >= 1440:
        return "end of day"
    hour = minute // 60
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return "%d:%02d %s" % (hour12, minute % 60, suffix)


def format_when(moment):
    local = moment.astimezone(hub_zone())
    clock = format_clock(local.hour * 60 + local.minute)
    return local.strftime("%A, %B ") + str(local.day) + local.strftime(", %Y") + " at " + clock + " " + (local.tzname() or "")


def clock_to_min(value, label):
    text = (value or "").strip()
    match = re.fullmatch(r"(\d{2}):(\d{2})", text)
    if match is None:
        raise ValueError(label + " needs a time like 08:00")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(label + " needs a time like 08:00")
    return hour * 60 + minute


def intervals_starting(rule, day, zone):
    if rule["repeat"] == "weekly":
        if day.weekday() not in parse_days(rule["days"]):
            return []
    elif rule["repeat"] == "once":
        if day.isoformat() != (rule.get("on_date") or ""):
            return []
    else:
        return []
    start_min = int(rule["start_min"])
    end_min = int(rule["end_min"])
    start = datetime(day.year, day.month, day.day, start_min // 60, start_min % 60, tzinfo=zone)
    if end_min >= 1440:
        end = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(days=1)
    elif end_min > start_min:
        end = datetime(day.year, day.month, day.day, end_min // 60, end_min % 60, tzinfo=zone)
    else:
        next_day = day + timedelta(days=1)
        end = datetime(next_day.year, next_day.month, next_day.day, end_min // 60, end_min % 60, tzinfo=zone)
    return [(start, end)]


def rule_covers(rule, moment):
    zone = moment.tzinfo
    day = moment.date()
    for delta in (0, -1):
        for start, end in intervals_starting(rule, day + timedelta(days=delta), zone):
            if start <= moment < end:
                return True
    return False


def rules_open(rules, moment):
    if not rules:
        return True
    allows = [rule for rule in rules if rule["kind"] == "allow"]
    denies = [rule for rule in rules if rule["kind"] == "deny"]
    if allows and not any(rule_covers(rule, moment) for rule in allows):
        return False
    if any(rule_covers(rule, moment) for rule in denies):
        return False
    return True


def next_open_at(rules, moment):
    if rules_open(rules, moment):
        return moment
    zone = moment.tzinfo
    start_day = moment.date() - timedelta(days=1)
    candidates = []
    for offset in range(62):
        day = start_day + timedelta(days=offset)
        for rule in rules:
            for begin, end in intervals_starting(rule, day, zone):
                if begin > moment:
                    candidates.append(begin)
                if end > moment:
                    candidates.append(end)
    for candidate in sorted(set(candidates)):
        if rules_open(rules, candidate):
            return candidate
    return None


def closed_message(name, rules, moment):
    nxt = next_open_at(rules, moment)
    if nxt is None:
        return name + " is currently outside its operating windows. No operating window is scheduled."
    return name + " is currently outside its operating windows. Check back at the start of the next one, " + format_when(nxt) + "."


def window_label(rule):
    action = "Can be used" if rule["kind"] == "allow" else "Cannot be used"
    span = format_clock(int(rule["start_min"])) + "–" + format_clock(int(rule["end_min"]))
    if rule["repeat"] == "weekly":
        names = ", ".join(WEEKDAYS[day] for day in sorted(parse_days(rule["days"])))
        return action + " · every week · " + names + " · " + span
    return action + " · " + (rule.get("on_date") or "") + " · " + span


def load_hour_rules(device_id):
    with db_lock:
        rows = db.execute(
            "SELECT id, device_id, kind, repeat, days, start_min, end_min, on_date FROM hours WHERE device_id = ? ORDER BY id",
            (device_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def usage_rows(device_id, feature):
    now_ts = int(time.time())
    with db_lock:
        return db.execute(
            "SELECT at FROM feature_uses WHERE device_id = ? AND feature = ? AND at >= ? ORDER BY at",
            (device_id, feature, now_ts - 86400),
        ).fetchall()


def quota_message(feature, maximum, rows):
    label = "Treat" if feature == "treat" else "Scent"
    used = len(rows)
    if maximum <= 0:
        return label + " is limited to 0 times in 24 hours."
    if used < maximum:
        return ""
    available = int(rows[0]["at"]) + 86400
    when = format_when(datetime.fromtimestamp(available, hub_zone()))
    return label + " has already been used " + str(maximum) + " times in the last 24 hours. The next one is available at " + when + "."


def camera_access(device_id):
    profile = device_profile(device_id)
    rules = load_hour_rules(device_id)
    now = hub_now()
    open_now = rules_open(rules, now)
    name = profile["nickname"] or device_id
    message = "" if open_now else closed_message(name, rules, now)
    treat_rows = usage_rows(device_id, "treat")
    scent_rows = usage_rows(device_id, "scent")
    treat_block = "" if open_now else message
    scent_block = "" if open_now else message
    if open_now:
        treat_block = quota_message("treat", profile["treat_daily_max"], treat_rows)
        scent_block = quota_message("scent", profile["scent_daily_max"], scent_rows)
    return {
        "open": open_now,
        "schedule_message": message,
        "treat_daily_max": profile["treat_daily_max"],
        "scent_daily_max": profile["scent_daily_max"],
        "treat_used": len(treat_rows),
        "scent_used": len(scent_rows),
        "treat_blocked": treat_block,
        "scent_blocked": scent_block,
    }


def reserve_feature(device_id, feature, maximum, username):
    now_ts = int(time.time())
    with db_lock:
        db.execute("DELETE FROM feature_uses WHERE at < ?", (now_ts - 2 * 86400,))
        rows = db.execute(
            "SELECT at FROM feature_uses WHERE device_id = ? AND feature = ? AND at >= ? ORDER BY at",
            (device_id, feature, now_ts - 86400),
        ).fetchall()
        message = quota_message(feature, maximum, rows)
        if message:
            db.commit()
            return message
        db.execute(
            "INSERT INTO feature_uses (device_id, feature, at, username) VALUES (?, ?, ?, ?)",
            (device_id, feature, now_ts, username or ""),
        )
        db.commit()
    return ""


def clean_daily_max(value, label):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(label + " must be a whole number")
    if number < 0 or number > 100:
        raise ValueError(label + " must be from 0 through 100")
    return number


def known_device(device_id):
    if device_id in devices:
        return True
    with db_lock:
        row = db.execute("SELECT device_id FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    return row is not None


def clean_window(body):
    device_id = (body.get("device_id") or "").strip()
    if not device_id or not known_device(device_id):
        raise ValueError("that camera is not on the hub")
    kind = (body.get("kind") or "").strip()
    if kind not in ("allow", "deny"):
        raise ValueError("choose whether the camera can be used or cannot be used")
    repeat = (body.get("repeat") or "").strip()
    if repeat not in ("weekly", "once"):
        raise ValueError("choose every week or one day")
    start_min = clock_to_min(body.get("start"), "Start")
    end_min = clock_to_min(body.get("end"), "End")
    if start_min == end_min:
        raise ValueError("the start and end need to be different. Use an earlier end to cross midnight")
    if end_min == 23 * 60 + 59 and end_min > start_min:
        end_min = 1440
    days = ""
    on_date = ""
    if repeat == "weekly":
        raw_days = body.get("days") or []
        if not isinstance(raw_days, list) or not raw_days:
            raise ValueError("choose at least one day")
        picked = []
        for item in raw_days:
            try:
                day = int(item)
            except (TypeError, ValueError):
                raise ValueError("choose a real day of the week")
            if day < 0 or day > 6 or day in picked:
                raise ValueError("choose a real day of the week")
            picked.append(day)
        days = ",".join(str(day) for day in sorted(picked))
    else:
        on_date = (body.get("on_date") or "").strip()
        try:
            datetime.strptime(on_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("choose the date for this window")
    return device_id, kind, repeat, days, start_min, end_min, on_date


def clean_flag(value, label):
    if isinstance(value, bool):
        return value
    if value in (1, 0):
        return bool(value)
    text = str(value if value is not None else "").strip().lower()
    if text in ("1", "true", "on", "yes"):
        return True
    if text in ("0", "false", "off", "no"):
        return False
    raise ValueError(label + " must be on or off")


def device_snapshot(user):
    allowed = None if user["role"] == "admin" else set(assigned_devices(user["id"]))
    items = []
    seen = set()
    for device_id, info in devices.items():
        seen.add(device_id)
        if allowed is not None and device_id not in allowed:
            continue
        items.append(device_record(device_id, info))
    extras = saved_device_ids() if allowed is None else list(allowed)
    for device_id in extras:
        if device_id not in seen:
            items.append(device_record(device_id))
    items.sort(key=lambda item: (item["nickname"] or item["device_id"]).lower())
    if user["role"] != "admin":
        for item in items:
            item.pop("last_ip", None)
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


def screen_path(kind):
    if kind not in ("ready", "closed"):
        raise ValueError("choose the ready picture or the out of service picture")
    return os.path.join(BASE, "screen-%s.jpg" % kind)


def screen_state():
    rev = 0
    state = {}
    for kind in ("ready", "closed"):
        path = screen_path(kind)
        present = os.path.isfile(path)
        state[kind] = present
        if present:
            rev = max(rev, int(os.path.getmtime(path)))
    state["rev"] = rev
    return state


def save_jpeg(kind, data):
    if len(data) < 100 or len(data) > 1024 * 1024:
        raise ValueError("use a JPEG no larger than 1 MB")
    if not data.startswith(b"\xff\xd8\xff") or b"\xff\xd9" not in data:
        raise ValueError("that file is not a JPEG")
    path = screen_path(kind)
    temporary = path + ".tmp"
    with open(temporary, "wb") as fh:
        fh.write(data)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


async def push_screen():
    payload = screen_state()
    payload["type"] = "screen"
    for info in list(devices.values()):
        ws = info.get("ws")
        if ws is None or ws.closed:
            continue
        await send_json(ws, payload)
    for sockets in list(user_sockets.values()):
        for ws in list(sockets):
            if ws.closed:
                continue
            await send_json(ws, payload)


async def push_service():
    for device_id, info in list(devices.items()):
        ws = info.get("ws")
        if ws is None or ws.closed:
            continue
        await send_json(ws, {"type": "service", "open": bool(camera_access(device_id)["open"])})


async def push_devices():
    dead = []
    for user_id, sockets in list(user_sockets.items()):
        user = user_by_id(user_id)
        if user is None:
            continue
        payload = {"type": "devices", "devices": device_snapshot(user), "screen": screen_state()}
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
    actor = user_by_id(call["user_id"])
    record_audit(
        call.get("username") or (actor["username"] if actor else ""),
        call.get("role") or (actor["role"] if actor else ""),
        "Call ended",
        device_id=call["device_id"],
        detail=reason,
        remote=call.get("remote") or "",
    )
    message = {"type": "hangup", "call_id": call_id, "reason": reason}
    await send_json(call.get("user_ws"), message)
    if info:
        await send_json(info.get("ws"), message)
    await push_devices()


def read_html(name):
    path = os.path.join(BASE, name)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def page(name, content_type):
    text = branded_html(name) if name.endswith(".html") else read_html(name)
    return web.Response(
        text=text,
        content_type=content_type,
        headers={"Cache-Control": "no-store"},
    )


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
    return web.json_response({"iceServers": await ice_servers(audience)})


async def device_page(_request):
    return web.Response(
        text=branded_html("device.html"),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def login_page(_request):
    return page("login.html", "text/html")


async def calls_page(_request):
    return page("calls.html", "text/html")


async def users_page(request):
    user = request_user(request)
    if user is None:
        raise web.HTTPFound("/")
    if user["role"] != "admin":
        raise web.HTTPFound("/calls")
    return page("users.html", "text/html")


async def settings_page(request):
    user = request_user(request)
    if user is None:
        raise web.HTTPFound("/")
    if user["role"] != "admin":
        raise web.HTTPFound("/calls")
    return page("settings.html", "text/html")


async def devices_page(request):
    user = request_user(request)
    if user is None:
        raise web.HTTPFound("/")
    if user["role"] != "admin":
        raise web.HTTPFound("/calls")
    return page("devices.html", "text/html")


async def schedule_page(request):
    user = request_user(request)
    if user is None:
        raise web.HTTPFound("/")
    if user["role"] != "admin":
        raise web.HTTPFound("/calls")
    return page("schedule.html", "text/html")


async def audit_page(request):
    user = request_user(request)
    if user is None:
        raise web.HTTPFound("/")
    if user["role"] != "admin":
        raise web.HTTPFound("/calls")
    return page("audit.html", "text/html")


async def app_css(_request):
    return page("app.css", "text/css")


async def common_js(_request):
    return page("common.js", "text/javascript")


async def login(request):
    data = await request.post()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if too_many_logins(client_ip(request)):
        await asyncio.sleep(2)
        if username:
            record_audit(username, "", "Sign-in failed", detail="too many attempts")
        raise web.HTTPFound("/?error=1")
    row = user_by_name(username)
    password_ok = row is not None and await check_password_async(password, row["password"])
    if not password_ok:
        note_login_failure(client_ip(request))
        if username:
            record_audit(username, "", "Sign-in failed", detail="wrong username or password")
        raise web.HTTPFound("/?error=1")
    if not account_open(row):
        record_audit(row["username"], row["role"], "Sign-in failed", detail="account is off or expired")
        raise web.HTTPFound("/?error=2")
    note_login_success(client_ip(request))
    token = secrets.token_urlsafe(32)
    expires = int(time.time()) + 14 * 24 * 3600
    with db_lock:
        db.execute(
            "INSERT INTO sessions (token, user_id, expires) VALUES (?, ?, ?)",
            (token, row["id"], expires),
        )
        db.commit()
    record_audit(row["username"], row["role"], "Signed in")
    response = web.HTTPFound("/calls")
    response.set_cookie(
        "sid", token, max_age=14 * 24 * 3600, httponly=True, secure=True, samesite="Lax"
    )
    return response


async def logout(request):
    user = request_user(request)
    token = request.cookies.get("sid")
    if token:
        with db_lock:
            db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            db.commit()
    if user is not None:
        record_audit(user["username"], user["role"], "Signed out")
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
        rows = db.execute(
            "SELECT id, username, role, enabled, expires_at, email, source FROM users ORDER BY username"
        ).fetchall()
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
                    "enabled": bool(row["enabled"]),
                    "expires_at": row["expires_at"],
                    "expired": row["expires_at"] is not None and int(row["expires_at"]) <= int(time.time()),
                    "email": row["email"] or "",
                    "source": row["source"] or "",
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
    if role not in ("user", "admin"):
        raise web.HTTPBadRequest(text="role must be user or admin")
    if not username or len(username) > 40 or any(ord(ch) < 32 for ch in username):
        raise web.HTTPBadRequest(text="username must be 1 to 40 characters")
    if len(password) < 6:
        raise web.HTTPBadRequest(text="password must be at least 6 characters")
    device_ids = []
    if role == "user":
        raw_devices = body.get("devices")
        if raw_devices is None and body.get("device_id"):
            raw_devices = [body.get("device_id")]
        if raw_devices is not None:
            try:
                device_ids = clean_device_ids(raw_devices)
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc))
    try:
        created = add_user(username, await hash_password_async(password), role, already_hashed=True)
    except sqlite3.IntegrityError:
        raise web.HTTPConflict(text="that username already exists")
    if device_ids:
        replace_assignments(created["id"], device_ids)
    record_audit(
        user["username"],
        user["role"],
        "Created account",
        subject=username,
        detail="role " + role + (", cameras " + ", ".join(device_ids) if device_ids else ""),
    )
    await push_devices()
    return web.json_response({"ok": True})


async def api_delete_user(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    username = (body.get("username") or "").strip()
    target = user_by_name(username)
    if target is None:
        raise web.HTTPBadRequest(text="that user does not exist")
    if target["id"] == user["id"]:
        raise web.HTTPBadRequest(text="you cannot delete the account you are signed in with")
    if target["role"] == "admin" and not other_open_admins(target["id"]):
        raise web.HTTPBadRequest(text="keep at least one admin that can sign in")
    remove_account_rows(target["id"])
    await close_user_sockets(target["id"])
    record_audit(user["username"], user["role"], "Deleted account", subject=username)
    log.info("deleted account %s", username)
    return web.json_response({"ok": True})


async def api_guest(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    try:
        device_ids = clean_device_ids([body.get("device_id")])
        expires_at = end_of_house_day(body.get("expires_on"))
        email = clean_email(body.get("email"), "Email")
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    if len(device_ids) != 1:
        raise web.HTTPBadRequest(text="choose a camera")
    device_id = device_ids[0]
    if device_id not in saved_device_ids() and device_id not in devices:
        raise web.HTTPBadRequest(text="choose a camera that has connected")
    password = guest_code(8)
    stored = await hash_password_async(password)
    created = None
    username = ""
    for _ in range(12):
        username = "guest" + guest_code(4)
        try:
            with db_lock:
                db.execute(
                    """
                    INSERT INTO users (username, password, role, enabled, expires_at, email, source)
                    VALUES (?, ?, 'user', 1, ?, ?, 'wizard')
                    """,
                    (username, stored, expires_at, email),
                )
                db.commit()
                created = db.execute(
                    "SELECT id, username FROM users WHERE username = ?", (username,)
                ).fetchone()
            break
        except sqlite3.IntegrityError:
            with db_lock:
                db.rollback()
            created = None
    if created is None:
        raise web.HTTPConflict(text="a guest username could not be chosen")
    replace_assignments(created["id"], [device_id])
    profile = device_profile(device_id)
    camera = profile["nickname"] or device_id
    when = format_when(datetime.fromtimestamp(expires_at, hub_zone()))
    cfg = load_config()
    instructions = guest_instructions(cfg, username, password, camera, when)
    email_status = "not requested"
    if email:
        if not mail_ready(cfg):
            email_status = "not configured"
        else:
            sent = await deliver_mail(email, "Your " + instance_name(cfg) + " sign-in", instructions)
            email_status = "sent" if sent else "failed"
    detail = "camera " + camera + ", ends " + when
    if email_status == "sent":
        detail += ", emailed"
    elif email_status == "not configured":
        detail += ", mail is not configured"
    elif email_status == "failed":
        detail += ", email failed"
    record_audit(user["username"], user["role"], "Created guest account", subject=username, detail=detail)
    await push_devices()
    return web.json_response(
        {
            "ok": True,
            "username": username,
            "password": password,
            "camera": camera,
            "expires_at": expires_at,
            "email": email,
            "email_status": email_status,
            "instructions": instructions,
            "title": instance_name(cfg) + " guest sign-in",
        }
    )


async def api_assign(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    username = (body.get("username") or "").strip()
    target = user_by_name(username)
    if target is None:
        raise web.HTTPBadRequest(text="that user does not exist")
    if "devices" in body:
        try:
            device_ids = clean_device_ids(body.get("devices"))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        replace_assignments(target["id"], device_ids)
        log.info("access for %s set to %s", username, ",".join(device_ids) or "(none)")
        detail = ", ".join(device_ids) or "no cameras"
    else:
        device_id = (body.get("device_id") or "").strip()
        if not device_id:
            raise web.HTTPBadRequest(text="devices are required")
        try:
            device_ids = clean_device_ids([device_id])
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        with db_lock:
            db.execute(
                "INSERT OR IGNORE INTO assignments (user_id, device_id) VALUES (?, ?)",
                (target["id"], device_ids[0]),
            )
            db.commit()
        detail = "added " + device_ids[0]
    record_audit(
        user["username"],
        user["role"],
        "Changed camera access",
        subject=username,
        detail=detail,
    )
    await push_devices()
    return web.json_response({"ok": True})


async def close_user_sockets(user_id):
    sockets = list(user_sockets.get(user_id, ()))
    for ws in sockets:
        if not ws.closed:
            await ws.close()


async def api_account(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    username = (body.get("username") or "").strip()
    target = user_by_name(username)
    if target is None:
        raise web.HTTPBadRequest(text="that user does not exist")
    try:
        enabled = clean_flag(body.get("enabled"), "enabled") if "enabled" in body else bool(target["enabled"])
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    if "expires_at" in body:
        raw_expires = body.get("expires_at")
        if raw_expires in (None, ""):
            expires_at = None
        else:
            try:
                expires_at = int(raw_expires)
            except (TypeError, ValueError):
                raise web.HTTPBadRequest(text="expiration must be a time")
            if expires_at < 0 or expires_at > 4000000000:
                raise web.HTTPBadRequest(text="expiration is not a real time")
    else:
        expires_at = target["expires_at"]
    if target["id"] == user["id"] and not enabled:
        raise web.HTTPBadRequest(text="you cannot turn off the account you are signed in with")
    if target["id"] == user["id"] and expires_at is not None and int(expires_at) <= int(time.time()):
        raise web.HTTPBadRequest(text="choose a future time, or leave expiration empty")
    still_open = enabled and (expires_at is None or int(expires_at) > int(time.time()))
    if target["role"] == "admin" and not still_open and not other_open_admins(target["id"]):
        raise web.HTTPBadRequest(text="keep at least one admin that can sign in")
    with db_lock:
        db.execute(
            "UPDATE users SET enabled = ?, expires_at = ? WHERE id = ?",
            (int(enabled), expires_at, target["id"]),
        )
        if not still_open:
            db.execute("DELETE FROM sessions WHERE user_id = ?", (target["id"],))
        db.commit()
    if not still_open:
        await close_user_sockets(target["id"])
    log.info("account %s enabled %s expires %s", username, enabled, expires_at or "never")
    if expires_at is None:
        when = "no expiration"
    else:
        when = "expires " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(expires_at)))
    record_audit(
        user["username"],
        user["role"],
        "Updated account",
        subject=username,
        detail=("on" if enabled else "off") + ", " + when,
    )
    return web.json_response({"ok": True})


def last4(value):
    text = value or ""
    if len(text) < 4:
        return ""
    return text[-4:]


def clean_secret(value, label):
    text = (value or "").strip()
    if not text:
        return ""
    if any(ch.isspace() or ord(ch) < 32 for ch in text):
        raise ValueError(label + " cannot include spaces")
    if len(text) > 4000:
        raise ValueError(label + " is too long")
    return text


def clean_key_id(value):
    text = (value or "").strip()
    if not text:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", text):
        raise ValueError("the Cloudflare key id should be the id from the TURN key, without the token")
    return text


def clean_metered_domain(value):
    text = (value or "").strip().lower()
    text = re.sub(r"^https?://", "", text).strip("/")
    if "/" in text:
        text = text.split("/", 1)[0]
    return clean_public_host(text)


def cloudflared_path():
    for candidate in ("/usr/local/bin/cloudflared", "/usr/bin/cloudflared"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil_which("cloudflared")


def tunnel_run_mode(value):
    return "service" if value == "service" else "standalone"


def tunnel_service_name(run_mode):
    if tunnel_run_mode(run_mode) == "service":
        return "cloudflared.service"
    return "petchatz-tunnel.service"


def unit_active(name):
    completed = subprocess.run(
        ["systemctl", "is-active", "--quiet", name],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return completed.returncode == 0


def stop_unit(name):
    subprocess.run(
        ["systemctl", "disable", "--now", name],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def unit_main_pid(name):
    completed = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", name],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    try:
        return int((completed.stdout or "0").strip() or "0")
    except ValueError:
        return 0


def cloudflared_processes():
    found = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % name, "r", encoding="utf-8", errors="replace") as fh:
                comm = fh.read().strip()
        except OSError:
            continue
        if comm == "cloudflared":
            found.append(int(name))
    return found


def process_parent(pid):
    try:
        with open("/proc/%s/stat" % pid, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        fields = text[text.rfind(")") + 2:].split()
        return int(fields[1])
    except (OSError, ValueError, IndexError):
        return 0


def cloudflared_keep(main_pid):
    keep = set()
    if main_pid:
        keep.add(main_pid)
    changed = True
    while changed:
        changed = False
        for pid in cloudflared_processes():
            if pid in keep:
                continue
            if process_parent(pid) in keep:
                keep.add(pid)
                changed = True
    return keep


def stop_extra_cloudflared(main_pid):
    keep = cloudflared_keep(main_pid)
    extras = [pid for pid in cloudflared_processes() if pid not in keep]
    for pid in extras:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if not extras:
        return
    time.sleep(1)
    keep = cloudflared_keep(main_pid)
    for pid in cloudflared_processes():
        if pid not in keep:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def write_secret_file(path, text):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def write_tunnel_token(token):
    write_secret_file(os.path.join(BASE, "tunnel.token"), token + "\n")
    write_secret_file(os.path.join(BASE, "tunnel.env"), "TUNNEL_TOKEN=%s\n" % token)


def token_file_supported(binary):
    completed = subprocess.run(
        [binary, "tunnel", "run", "--help"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return "--token-file" in ((completed.stdout or "") + (completed.stderr or ""))


def tunnel_unit(binary, use_file):
    token_path = os.path.join(BASE, "tunnel.token")
    if use_file:
        start = "%s --no-autoupdate tunnel --protocol http2 run --token-file %s" % (binary, token_path)
        environment = ""
    else:
        start = "%s --no-autoupdate tunnel --protocol http2 run" % binary
        environment = "EnvironmentFile=%s\n" % os.path.join(BASE, "tunnel.env")
    return """[Unit]
Description=Cloudflare Tunnel
After=network-online.target

[Service]
TimeoutStartSec=0
%sExecStart=%s
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
""" % (environment, start)


def apply_tunnel(mode, token, run_mode):
    run_mode = tunnel_run_mode(run_mode)
    managed = os.path.join(BASE, "cloudflared-managed")
    binary = cloudflared_path()
    if mode != "tunnel" or not token:
        stop_unit("petchatz-tunnel.service")
        stop_unit("cloudflared.service")
        if os.path.isfile(managed):
            os.remove(managed)
        stop_extra_cloudflared(0)
        return "The page tunnel is off."
    if not binary:
        return "The tunnel token is saved. Install cloudflared on the hub before it can start."
    write_tunnel_token(token)
    use_file = token_file_supported(binary)
    chosen = tunnel_service_name(run_mode)
    other = "cloudflared.service" if chosen == "petchatz-tunnel.service" else "petchatz-tunnel.service"
    with open("/etc/systemd/system/" + chosen, "w", encoding="utf-8") as fh:
        fh.write(tunnel_unit(binary, use_file))
    stop_unit(other)
    if run_mode == "service":
        write_secret_file(managed, "service\n")
    elif os.path.isfile(managed):
        os.remove(managed)
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True, timeout=20, check=False)
    completed = subprocess.run(
        ["systemctl", "enable", "--now", chosen],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    subprocess.run(["systemctl", "restart", chosen], capture_output=True, text=True, timeout=30, check=False)
    if not unit_active(chosen):
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        log.warning("tunnel did not start: %s", (detail[-1] if detail else "no output")[:200])
        return "The tunnel token is saved, but cloudflared did not stay running."
    stop_extra_cloudflared(unit_main_pid(chosen))
    if len(cloudflared_processes()) != 1:
        log.warning("cloudflared still has %s processes", len(cloudflared_processes()))
        return "The tunnel was updated, but more than one cloudflared is still running."
    if run_mode == "service":
        return "The Cloudflare Tunnel is running as a service."
    return "The Cloudflare Tunnel is running standalone."


def tunnel_note(cfg):
    mode = cfg.get("access_mode") or "hostname"
    token = cfg.get("tunnel_token") or ""
    run_mode = tunnel_run_mode(cfg.get("tunnel_run"))
    if mode != "tunnel":
        return "The page tunnel is off."
    if not token:
        return "Tunnel mode is on. Add a token when you have one."
    if not cloudflared_path():
        return "The tunnel token is saved. Install cloudflared on the hub before it can start."
    count = len(cloudflared_processes())
    if count > 1:
        return "More than one cloudflared is running. Save on Settings to keep only the one you picked."
    chosen = tunnel_service_name(run_mode)
    if unit_active(chosen) and count == 1:
        if run_mode == "service":
            return "The Cloudflare Tunnel is running as a service."
        return "The Cloudflare Tunnel is running standalone."
    if run_mode != "service" and unit_active("cloudflared.service"):
        return "cloudflared.service is running. Choose As a service and save to update it."
    if run_mode == "service" and unit_active("petchatz-tunnel.service"):
        return "Standalone cloudflared is running. Choose Standalone and save to update it."
    return "The tunnel token is saved, but cloudflared is not running."


def settings_view(cfg):
    return {
        "public_host": cfg.get("public_host") or "",
        "lan_ip": cfg.get("lan_ip") or "",
        "access_mode": cfg.get("access_mode") or "hostname",
        "turn_local": cfg.get("turn_local", True) is not False,
        "metered_enabled": bool(cfg.get("metered_enabled")),
        "metered_domain": cfg.get("metered_domain") or "",
        "metered_api_key_set": bool(cfg.get("metered_api_key")),
        "metered_api_key_last4": last4(cfg.get("metered_api_key")),
        "cloudflare_turn_enabled": bool(cfg.get("cloudflare_turn_enabled")),
        "cloudflare_turn_key_id": cfg.get("cloudflare_turn_key_id") or "",
        "cloudflare_turn_token_set": bool(cfg.get("cloudflare_turn_token")),
        "cloudflare_turn_token_last4": last4(cfg.get("cloudflare_turn_token")),
        "tunnel_token_set": bool(cfg.get("tunnel_token")),
        "tunnel_token_last4": last4(cfg.get("tunnel_token")),
        "tunnel_run": tunnel_run_mode(cfg.get("tunnel_run")),
        "cloudflared_installed": bool(cloudflared_path()),
        "tunnel_status": tunnel_note(cfg),
        "smtp_host": cfg.get("smtp_host") or "",
        "smtp_port": smtp_port_value(cfg),
        "smtp_security": cfg.get("smtp_security") or "starttls",
        "smtp_user": cfg.get("smtp_user") or "",
        "smtp_password_set": bool(cfg.get("smtp_password")),
        "smtp_password_last4": last4(cfg.get("smtp_password")),
        "smtp_from": cfg.get("smtp_from") or "",
        "audit_email": cfg.get("audit_email") or "off",
        "audit_email_to": cfg.get("audit_email_to") or "",
        "mail_ready": mail_ready(cfg),
        "instance_name": instance_name(cfg),
        "guest_template": guest_template_text(cfg),
        "guest_template_default": DEFAULT_GUEST_TEMPLATE,
    }


def keep_secret(body, field, clear_field, current):
    if body.get(clear_field):
        return ""
    if field in body and (body.get(field) or "").strip():
        return body.get(field)
    return current


async def api_settings(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    return web.json_response(settings_view(load_config()))


async def api_settings_save(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    cfg = load_config()
    if "access_mode" in body:
        mode = (body.get("access_mode") or "hostname").strip()
    else:
        mode = cfg.get("access_mode") or "hostname"
    if mode not in ("hostname", "tunnel"):
        raise web.HTTPBadRequest(text="choose a web address or a tunnel")
    try:
        public_host = clean_public_host(body.get("public_host")) if "public_host" in body else (cfg.get("public_host") or "")
        metered_domain = clean_metered_domain(body.get("metered_domain")) if "metered_domain" in body else (cfg.get("metered_domain") or "")
        key_id = clean_key_id(body.get("cloudflare_turn_key_id")) if "cloudflare_turn_key_id" in body else (cfg.get("cloudflare_turn_key_id") or "")
        metered_key = clean_secret(body.get("metered_api_key"), "Metered API key") if (body.get("metered_api_key") or "").strip() else ""
        cf_token = clean_secret(body.get("cloudflare_turn_token"), "Cloudflare token") if (body.get("cloudflare_turn_token") or "").strip() else ""
        tunnel_token = clean_secret(body.get("tunnel_token"), "Tunnel token") if (body.get("tunnel_token") or "").strip() else ""
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    previous_mode = cfg.get("access_mode") or "hostname"
    previous_token = cfg.get("tunnel_token") or ""
    previous_run = tunnel_run_mode(cfg.get("tunnel_run"))
    run_mode = tunnel_run_mode(body.get("tunnel_run")) if "tunnel_run" in body else previous_run
    cfg["public_host"] = public_host
    cfg["access_mode"] = mode
    cfg["tunnel_run"] = run_mode
    if "turn_local" in body:
        cfg["turn_local"] = bool(body.get("turn_local"))
    if "metered_enabled" in body:
        cfg["metered_enabled"] = bool(body.get("metered_enabled"))
    if "metered_domain" in body:
        cfg["metered_domain"] = metered_domain
    cfg["metered_api_key"] = keep_secret(body, "metered_api_key", "metered_api_key_clear", cfg.get("metered_api_key") or "")
    if metered_key:
        cfg["metered_api_key"] = metered_key
    if "cloudflare_turn_enabled" in body:
        cfg["cloudflare_turn_enabled"] = bool(body.get("cloudflare_turn_enabled"))
    if "cloudflare_turn_key_id" in body:
        cfg["cloudflare_turn_key_id"] = key_id
    cfg["cloudflare_turn_token"] = keep_secret(body, "cloudflare_turn_token", "cloudflare_turn_token_clear", cfg.get("cloudflare_turn_token") or "")
    if cf_token:
        cfg["cloudflare_turn_token"] = cf_token
    cfg["tunnel_token"] = keep_secret(body, "tunnel_token", "tunnel_token_clear", cfg.get("tunnel_token") or "")
    if tunnel_token:
        cfg["tunnel_token"] = tunnel_token
    touch_access = any(
        key in body
        for key in (
            "access_mode",
            "public_host",
            "tunnel_run",
            "tunnel_token",
            "tunnel_token_clear",
            "turn_local",
            "metered_enabled",
            "metered_domain",
            "metered_api_key",
            "metered_api_key_clear",
            "cloudflare_turn_enabled",
            "cloudflare_turn_key_id",
            "cloudflare_turn_token",
            "cloudflare_turn_token_clear",
        )
    )
    touch_mail = any(
        key in body
        for key in (
            "smtp_host",
            "smtp_port",
            "smtp_security",
            "smtp_user",
            "smtp_password",
            "smtp_password_clear",
            "smtp_from",
            "audit_email",
            "audit_email_to",
        )
    )
    touch_brand = any(key in body for key in ("instance_name", "guest_template"))
    if touch_mail:
        try:
            smtp_host = clean_public_host(body.get("smtp_host")) if "smtp_host" in body else (cfg.get("smtp_host") or "")
            security = (body.get("smtp_security") or cfg.get("smtp_security") or "starttls").strip()
            if security not in ("starttls", "ssl", "none"):
                raise ValueError("choose STARTTLS, SSL, or no encryption")
            raw_port = body.get("smtp_port")
            if raw_port in (None, ""):
                smtp_port = int(cfg.get("smtp_port") or (465 if security == "ssl" else 587))
            else:
                smtp_port = int(raw_port)
            if not 1 <= smtp_port <= 65535:
                raise ValueError("the mail port must be from 1 through 65535")
            smtp_user = (body.get("smtp_user") if "smtp_user" in body else (cfg.get("smtp_user") or ""))
            smtp_user = str(smtp_user).strip()
            if len(smtp_user) > 200 or any(ch.isspace() or ord(ch) < 32 for ch in smtp_user):
                raise ValueError("the mail username is not valid")
            smtp_password = keep_secret(body, "smtp_password", "smtp_password_clear", cfg.get("smtp_password") or "")
            if (body.get("smtp_password") or "").strip():
                smtp_password = clean_smtp_password(body.get("smtp_password"))
            smtp_from = clean_email(body.get("smtp_from") if "smtp_from" in body else cfg.get("smtp_from"), "From")
            if smtp_host and not smtp_from:
                raise ValueError("add a From address for the mail server")
            audit_email = (body.get("audit_email") if "audit_email" in body else (cfg.get("audit_email") or "off"))
            audit_email = str(audit_email or "off").strip()
            if audit_email not in ("off", "errors", "all"):
                raise ValueError("choose no activity mail, errors only, or every activity")
            audit_email_to = clean_email(
                body.get("audit_email_to") if "audit_email_to" in body else cfg.get("audit_email_to"),
                "Activity mail",
            )
            if audit_email != "off" and not audit_email_to:
                raise ValueError("add an address for activity mail")
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc) or "mail settings are not valid")
        cfg["smtp_host"] = smtp_host
        cfg["smtp_port"] = smtp_port
        cfg["smtp_security"] = security
        cfg["smtp_user"] = smtp_user
        cfg["smtp_password"] = smtp_password
        cfg["smtp_from"] = smtp_from
        cfg["audit_email"] = audit_email
        cfg["audit_email_to"] = audit_email_to
    if touch_brand:
        try:
            if "instance_name" in body:
                cfg["instance_name"] = clean_instance_name(body.get("instance_name"))
            if "guest_template" in body:
                cfg["guest_template"] = clean_guest_template(body.get("guest_template"))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
    save_config(cfg)
    _relay_cache.clear()
    note = ""
    token_now = cfg.get("tunnel_token") or ""
    changed = (previous_mode, previous_token, previous_run) != (mode, token_now, run_mode)
    chosen_name = tunnel_service_name(run_mode)
    other_name = "cloudflared.service" if chosen_name == "petchatz-tunnel.service" else "petchatz-tunnel.service"
    too_many = mode == "tunnel" and bool(token_now) and len(cloudflared_processes()) > 1
    needs_start = (
        mode == "tunnel"
        and bool(token_now)
        and not unit_active(chosen_name)
        and not unit_active(other_name)
    )
    if touch_access and (changed or needs_start or too_many):
        try:
            note = await asyncio.get_running_loop().run_in_executor(None, apply_tunnel, mode, token_now, run_mode)
        except (OSError, subprocess.TimeoutExpired):
            log.exception("tunnel update failed")
            note = "The settings were saved, but the tunnel could not be changed."
    if touch_brand and not touch_access and not touch_mail:
        detail = "name %s" % instance_name(cfg)
        if "guest_template" in body:
            detail += ", guest instructions"
    elif touch_mail and not touch_access:
        detail = "mail %s, activity %s" % (cfg.get("smtp_host") or "off", cfg.get("audit_email") or "off")
    else:
        detail = "%s %s, local %s, metered %s, cloudflare %s" % (
            mode,
            public_host or "(no name)",
            "on" if cfg.get("turn_local", True) is not False else "off",
            "on" if cfg.get("metered_enabled") else "off",
            "on" if cfg.get("cloudflare_turn_enabled") else "off",
        )
        if touch_mail:
            detail += ", mail %s, activity %s" % (cfg.get("smtp_host") or "off", cfg.get("audit_email") or "off")
        if touch_brand:
            detail += ", name %s" % instance_name(cfg)
    log.info("settings %s", detail)
    record_audit(user["username"], user["role"], "Saved settings", detail=detail)
    view = settings_view(cfg)
    view["ok"] = True
    view["notice"] = note
    return web.json_response(view)


def task_view():
    with db_lock:
        rows = db.execute(
            "SELECT id, title, enabled, hour, minute, last_run, last_result FROM tasks ORDER BY title"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "enabled": bool(row["enabled"]),
            "hour": int(row["hour"]),
            "minute": int(row["minute"]),
            "last_run": row["last_run"],
            "last_result": row["last_result"] or "",
            "timezone": hub_zone_name(),
        }
        for row in rows
    ]


async def api_tasks(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    return web.json_response({"tasks": task_view()})


async def api_task_save(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    task_id = (body.get("id") or "").strip()
    with db_lock:
        row = db.execute("SELECT id, title FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise web.HTTPBadRequest(text="that task does not exist")
    try:
        hour = int(body.get("hour"))
        minute = int(body.get("minute"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="choose a time of day")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise web.HTTPBadRequest(text="choose a time of day")
    enabled = 1 if body.get("enabled") else 0
    with db_lock:
        db.execute(
            "UPDATE tasks SET enabled = ?, hour = ?, minute = ?, armed_at = ? WHERE id = ?",
            (enabled, hour, minute, int(time.time()), task_id),
        )
        db.commit()
    when = "%02d:%02d" % (hour, minute)
    record_audit(
        user["username"],
        user["role"],
        "Saved a task",
        subject=row["title"],
        detail=("on" if enabled else "off") + " at " + when + " " + hub_zone_name(),
    )
    return web.json_response({"ok": True, "tasks": task_view()})


async def api_task_run(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    task_id = (body.get("id") or "").strip()
    with db_lock:
        row = db.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise web.HTTPBadRequest(text="that task does not exist")
    try:
        result = await run_task(task_id, user["username"], user["role"])
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    return web.json_response({"ok": True, "result": result, "tasks": task_view()})


async def api_mail_test(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    cfg = load_config()
    if not mail_ready(cfg):
        raise web.HTTPBadRequest(text="save a mail server and a From address first")
    recipient = (cfg.get("audit_email_to") or cfg.get("smtp_from") or "").strip()
    name = instance_name(cfg)
    sent = await deliver_mail(
        recipient,
        name + " mail test",
        "This is a test from the " + name + " hub. Activity mail uses this same server.",
    )
    if not sent:
        raise web.HTTPBadRequest(text="the test message was not sent")
    record_audit(user["username"], user["role"], "Sent a mail test", subject=recipient)
    return web.json_response({"ok": True, "notice": "Sent a test message to " + recipient})


def install_cloudflared():
    machine = os.uname().machine
    arch = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}.get(machine)
    if not arch:
        raise ValueError("this hub's processor does not have a published cloudflared build")
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-%s" % arch
    destination = "/usr/local/bin/cloudflared"
    temporary = destination + ".tmp"
    request = urllib.request.Request(url, headers={"User-Agent": "petchatz-hub"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if len(data) < 1000000 or not data.startswith(b"\x7fELF"):
        raise ValueError("the cloudflared download was not a program")
    with open(temporary, "wb") as fh:
        fh.write(data)
    os.chmod(temporary, 0o755)
    os.replace(temporary, destination)


async def api_tunnel_install(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    if cloudflared_path():
        return web.json_response({"ok": True, "notice": "cloudflared is already installed."})
    try:
        await asyncio.get_running_loop().run_in_executor(None, install_cloudflared)
    except (OSError, ValueError, aiohttp.ClientError) as exc:
        log.warning("cloudflared install failed: %s", exc)
        raise web.HTTPBadRequest(text=str(exc) or "cloudflared could not be installed")
    record_audit(user["username"], user["role"], "Installed cloudflared")
    cfg = load_config()
    notice = "cloudflared is installed."
    if (cfg.get("access_mode") or "") == "tunnel" and cfg.get("tunnel_token"):
        try:
            notice = await asyncio.get_running_loop().run_in_executor(
                None, apply_tunnel, "tunnel", cfg.get("tunnel_token") or "", tunnel_run_mode(cfg.get("tunnel_run"))
            )
        except (OSError, subprocess.TimeoutExpired):
            notice = "cloudflared is installed, but the tunnel did not start."
    return web.json_response({"ok": True, "notice": notice, "cloudflared_installed": True, "tunnel_status": tunnel_note(cfg)})


async def api_device_save(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        raise web.HTTPBadRequest(text="device_id is required")
    try:
        nickname, width, height, frame_rate, echo_cancel, noise_suppression, treats, scent = clean_capture(body)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    with db_lock:
        db.execute(
            """
            INSERT INTO devices (
                device_id, nickname, width, height, frame_rate, echo_cancel, noise_suppression, treats_enabled, scent_enabled
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                nickname = excluded.nickname,
                width = excluded.width,
                height = excluded.height,
                frame_rate = excluded.frame_rate,
                echo_cancel = excluded.echo_cancel,
                noise_suppression = excluded.noise_suppression,
                treats_enabled = excluded.treats_enabled,
                scent_enabled = excluded.scent_enabled
            """,
            (
                device_id,
                nickname,
                width,
                height,
                frame_rate,
                int(echo_cancel),
                int(noise_suppression),
                int(treats),
                int(scent),
            ),
        )
        db.commit()
    log.info(
        "camera %s set to %sx%s @ %s echo %s noise %s treats %s scent %s",
        device_id,
        width,
        height,
        frame_rate,
        echo_cancel,
        noise_suppression,
        treats,
        scent,
    )
    record_audit(
        user["username"],
        user["role"],
        "Saved camera settings",
        device_id=device_id,
        nickname=nickname,
        detail="%sx%s @ %s, echo %s, noise %s, treats %s, scent %s"
        % (
            width,
            height,
            frame_rate,
            "on" if echo_cancel else "off",
            "on" if noise_suppression else "off",
            "on" if treats else "off",
            "on" if scent else "off",
        ),
    )
    info = devices.get(device_id)
    if info is not None:
        await send_json(info.get("ws"), capture_payload(device_id))
    await push_devices()
    return web.json_response({"ok": True})


async def api_device_remove(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        raise web.HTTPBadRequest(text="device_id is required")
    nickname = device_profile(device_id)["nickname"]
    info = devices.get(device_id)
    if info and info.get("call_id"):
        await end_call(info["call_id"], "device removed")
    with db_lock:
        db.execute("DELETE FROM assignments WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM hours WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM feature_uses WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
        db.commit()
    still_online = info is not None and info.get("ws") is not None and not info["ws"].closed
    if not still_online:
        devices.pop(device_id, None)
    log.info("device %s removed online %s", device_id, still_online)
    record_audit(
        user["username"],
        user["role"],
        "Removed a camera",
        device_id=device_id,
        nickname=nickname,
        detail="still connected" if still_online else "offline",
    )
    await push_devices()
    return web.json_response({"ok": True, "still_online": still_online})


async def api_treat(request):
    user = request_user(request)
    if user is None:
        raise web.HTTPUnauthorized()
    body = await request.json()
    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        raise web.HTTPBadRequest(text="device_id is required")
    if not can_use_device(user, device_id):
        raise web.HTTPForbidden(text="you cannot use that camera")
    if not device_profile(device_id)["treats"]:
        raise web.HTTPBadRequest(text="treats are turned off for this camera")
    access = camera_access(device_id)
    if not access["open"]:
        record_audit(user["username"], user["role"], "Treat refused", device_id=device_id, detail="outside operating hours")
        raise web.HTTPBadRequest(text=access["schedule_message"])
    info = devices.get(device_id)
    if info is None or info.get("ws") is None or info["ws"].closed:
        raise web.HTTPBadRequest(text="that camera is offline")
    now = time.time()
    if now - treat_sent_at.get(device_id, 0) < 3:
        raise web.HTTPBadRequest(text="wait a moment before another treat")
    limited = reserve_feature(device_id, "treat", access["treat_daily_max"], user["username"])
    if limited:
        record_audit(user["username"], user["role"], "Treat refused", device_id=device_id, detail="daily limit")
        raise web.HTTPBadRequest(text=limited)
    treat_sent_at[device_id] = now
    await send_json(info.get("ws"), {"type": "treat"})
    log.info("treat %s by %s", device_id, user["username"])
    record_audit(user["username"], user["role"], "Dispensed a treat", device_id=device_id)
    await push_devices()
    return web.json_response({"ok": True})


async def api_scent(request):
    user = request_user(request)
    if user is None:
        raise web.HTTPUnauthorized()
    body = await request.json()
    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        raise web.HTTPBadRequest(text="device_id is required")
    if not can_use_device(user, device_id):
        raise web.HTTPForbidden(text="you cannot use that camera")
    if not device_profile(device_id)["scent"]:
        raise web.HTTPBadRequest(text="scent is turned off for this camera")
    access = camera_access(device_id)
    if not access["open"]:
        record_audit(user["username"], user["role"], "Scent refused", device_id=device_id, detail="outside operating hours")
        raise web.HTTPBadRequest(text=access["schedule_message"])
    info = devices.get(device_id)
    if info is None or info.get("ws") is None or info["ws"].closed:
        raise web.HTTPBadRequest(text="that camera is offline")
    now = time.time()
    if now - scent_sent_at.get(device_id, 0) < SCENT_SECONDS:
        raise web.HTTPBadRequest(text="wait a moment before another scent")
    limited = reserve_feature(device_id, "scent", access["scent_daily_max"], user["username"])
    if limited:
        record_audit(user["username"], user["role"], "Scent refused", device_id=device_id, detail="daily limit")
        raise web.HTTPBadRequest(text=limited)
    scent_sent_at[device_id] = now
    await send_json(info.get("ws"), {"type": "scent", "seconds": SCENT_SECONDS})
    log.info("scent %s by %s", device_id, user["username"])
    record_audit(user["username"], user["role"], "Released scent", device_id=device_id, detail="%s seconds" % SCENT_SECONDS)
    await push_devices()
    return web.json_response({"ok": True})


DEVICE_SETUP = r"""
import os, re, shutil, subprocess, sys, time
hub = sys.argv[1]
override_path = "/etc/systemd/system/petchatz-ws.service.d/override.conf"
override = "[Service]\nWatchdogSec=20s\nFailureAction=none\nStartLimitAction=none\nTimeoutStopSec=10s\n"
os.makedirs("/etc/systemd/system/petchatz-ws.service.d", exist_ok=True)
current = ""
if os.path.exists(override_path):
    current = open(override_path, "r").read()
wrote = current != override
if wrote:
    handle = open(override_path, "w")
    handle.write(override)
    handle.close()
shown = subprocess.check_output(
    ["systemctl", "show", "petchatz-ws.service", "-p", "FailureAction", "-p", "StartLimitAction"],
    universal_newlines=True,
)
needs_apply = wrote or ("FailureAction=none" not in shown) or ("StartLimitAction=none" not in shown)
if needs_apply:
    subprocess.check_call(["systemctl", "daemon-reload"])
    subprocess.call(["systemctl", "restart", "petchatz-ws.service"])
    ready = False
    for _ in range(20):
        if subprocess.call(["systemctl", "is-active", "--quiet", "petchatz-ws.service"]) == 0:
            ready = True
            break
        time.sleep(1)
    if not ready:
        sys.stderr.write("service did not return\n")
        sys.exit(3)
    print("watchdog updated")
else:
    print("watchdog already set")
js_path = "/var/www/http/scripts/deviceSettingsMenu.js"
js = open(js_path, "r").read()
lines = js.splitlines(True)
found = False
rebuilt = []
for line in lines:
    if (not found) and line.lstrip().startswith("var petchatz_server"):
        rebuilt.append('var petchatz_server = "%s";\n' % hub)
        found = True
    else:
        rebuilt.append(line)
if not found:
    sys.stderr.write("server line missing\n")
    sys.exit(2)
updated = "".join(rebuilt)
if updated != js:
    backup = js_path + ".bak"
    if not os.path.exists(backup):
        shutil.copy(js_path, backup)
    handle = open(js_path, "w")
    handle.write(updated)
    handle.close()
    print("server updated")
else:
    print("server already set")
if updated == js and not needs_apply:
    print("screen left as it is")
    sys.exit(0)
import base64, json, socket, urllib.request
try:
    targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5).read().decode())
    page = None
    for item in targets:
        if item.get("type") == "page":
            page = item
            break
    if page is None:
        raise RuntimeError("no page")
    path = page["webSocketDebuggerUrl"].split(":9222", 1)[1]
    sock = socket.create_connection(("127.0.0.1", 9222), 5)
    key = base64.b64encode(os.urandom(16)).decode()
    req = "GET %s HTTP/1.1\r\nHost: 127.0.0.1:9222\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, key)
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    payload = json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "location.reload(true)", "returnByValue": True}}).encode()
    mask = os.urandom(4)
    frame = bytearray([0x81, 0x80 | len(payload)])
    frame.extend(mask)
    frame.extend(bytearray(payload[i] ^ mask[i % 4] for i in range(len(payload))))
    sock.sendall(frame)
    sock.close()
    print("screen reloaded")
except Exception:
    print("screen was not reloaded")
"""


def private_ipv4(value):
    text = (value or "").strip()
    parts = text.split(".")
    if len(parts) != 4 or any(not part.isdigit() for part in parts):
        raise ValueError("enter an IPv4 address on the home network")
    nums = [int(part) for part in parts]
    if any(num > 255 for num in nums):
        raise ValueError("enter an IPv4 address on the home network")
    private = (
        nums[0] == 10
        or (nums[0] == 192 and nums[1] == 168)
        or (nums[0] == 172 and 16 <= nums[1] <= 31)
    )
    if not private:
        raise ValueError("use the camera's address on the home network")
    return ".".join(str(num) for num in nums)


def deploy_failure(completed):
    text = ((completed.stderr or "") + "\n" + (completed.stdout or "")).lower()
    if "permission denied" in text or "authentication" in text:
        return "that camera did not accept the empty root password"
    if "timed out" in text or "no route" in text or "connection refused" in text or "connection closed" in text:
        return "that camera did not answer. Check that it is on and the address is right."
    if completed.returncode == 2:
        return "the camera menu does not have a server line to change"
    if completed.returncode == 3:
        return "the camera service did not come back after the watchdog change"
    return "the camera did not finish setup"


def deploy_device(ip, hub_ip):
    if not os.path.isfile("/usr/bin/sshpass") and not shutil_which("sshpass"):
        raise ValueError("the hub does not have sshpass installed")
    known = os.path.join(BASE, "known_hosts")
    env = os.environ.copy()
    env["SSHPASS"] = ""
    completed = subprocess.run(
        [
            "sshpass", "-e", "ssh",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            "-o", "ConnectTimeout=50",
            "-o", "UserKnownHostsFile=%s" % known,
            "root@%s" % ip,
            "python3", "-", hub_ip,
        ],
        input=DEVICE_SETUP.replace("\r\n", "\n"),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        log.warning("device deploy %s failed: %s", ip, (completed.stderr or "")[:400])
        raise ValueError(deploy_failure(completed))
    detail = " ".join(line.strip() for line in (completed.stdout or "").splitlines() if line.strip())
    log.info("device deploy %s: %s", ip, detail)
    return detail or "camera updated"


def shutil_which(name):
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


async def api_device_deploy(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    try:
        ip = private_ipv4(body.get("ip"))
    except ValueError as exc:
        record_audit(user["username"], user["role"], "Camera setup failed", detail=str(exc))
        raise web.HTTPBadRequest(text=str(exc))
    hub_ip = (load_config().get("lan_ip") or "").strip()
    try:
        private_ipv4(hub_ip)
    except ValueError:
        record_audit(user["username"], user["role"], "Camera setup failed", detail="hub has no home-network address")
        raise web.HTTPBadRequest(text="the hub does not have a home-network address saved")
    try:
        detail = await asyncio.get_running_loop().run_in_executor(None, deploy_device, ip, hub_ip)
    except subprocess.TimeoutExpired:
        log.warning("device deploy %s timed out", ip)
        record_audit(user["username"], user["role"], "Camera setup failed", subject=ip, detail="timed out")
        raise web.HTTPGatewayTimeout(text="the camera did not finish within two minutes")
    except ValueError as exc:
        record_audit(user["username"], user["role"], "Camera setup failed", subject=ip, detail=str(exc))
        raise web.HTTPBadRequest(text=str(exc))
    record_audit(user["username"], user["role"], "Deployed camera setup", subject=ip, detail=detail)
    return web.json_response({"ok": True, "detail": detail})


async def api_password(request):
    user = request_user(request)
    if user is None:
        raise web.HTTPUnauthorized()
    if user["role"] != "admin":
        raise web.HTTPForbidden(text="only an admin can change a password")
    body = await request.json()
    new_password = body.get("new") or ""
    if len(new_password) < 6:
        raise web.HTTPBadRequest(text="new password must be at least 6 characters")
    target_name = (body.get("username") or "").strip()
    drop_sessions = False
    close_id = None
    if target_name and target_name != user["username"]:
        target = user_by_name(target_name)
        if target is None:
            raise web.HTTPBadRequest(text="that user does not exist")
        if target["role"] != "user":
            raise web.HTTPForbidden(text="change an admin password from My password")
        user_id = target["id"]
        drop_sessions = True
        close_id = target["id"]
        log.info("password set for %s", target_name)
        audit_action = "Set a password"
        audit_subject = target_name
    else:
        current = user_by_name(user["username"])
        if not await check_password_async(body.get("old") or "", current["password"]):
            record_audit(
                user["username"],
                user["role"],
                "Password change refused",
                detail="current password was wrong",
            )
            raise web.HTTPForbidden(text="current password is wrong")
        user_id = user["id"]
        audit_action = "Changed own password"
        audit_subject = ""
    stored = await hash_password_async(new_password)
    with db_lock:
        db.execute(
            "UPDATE users SET password = ? WHERE id = ?",
            (stored, user_id),
        )
        if drop_sessions:
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        db.commit()
    if close_id is not None:
        await close_user_sockets(close_id)
    record_audit(user["username"], user["role"], audit_action, subject=audit_subject)
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
                "screen": screen_state(),
                "iceServers": await ice_servers(audience),
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
    peer = clean_ip(request.remote or "")
    try:
        device_id = await device_loop(ws, peer)
    finally:
        if device_id and devices.get(device_id, {}).get("ws") is ws:
            call_id = devices[device_id].get("call_id")
            devices[device_id]["ws"] = None
            devices[device_id]["call_id"] = None
            if call_id:
                await end_call(call_id, "device disconnected")
            await push_devices()
    return ws


async def device_loop(ws, peer):
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
            remember_device(device_id)
            note_device_ip(device_id, peer)
            await send_json(
                ws,
                {
                    "type": "hello",
                    "iceServers": await ice_servers("lan"),
                    "capture": capture_payload(device_id),
                    "open": bool(camera_access(device_id)["open"]),
                    "screen": screen_state(),
                },
            )
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
        current = user_by_id(user["id"])
        if not account_open(current):
            record_audit(
                user["username"],
                user["role"],
                "Signed out",
                detail="account is off or expired",
            )
            await ws.close()
            break
        user = current
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
        record_audit(user["username"], user["role"], "Call refused", device_id=device_id, detail="not allowed")
        await send_json(ws, {"type": "error", "message": "you cannot call that device"})
        return
    access = camera_access(device_id)
    if not access["open"]:
        record_audit(user["username"], user["role"], "Call refused", device_id=device_id, detail="outside operating hours")
        await send_json(ws, {"type": "error", "message": access["schedule_message"]})
        return
    info = devices.get(device_id)
    if info is None or info.get("ws") is None or info["ws"].closed:
        record_audit(user["username"], user["role"], "Call refused", device_id=device_id, detail="offline")
        await send_json(ws, {"type": "error", "message": "that device is offline"})
        return
    if info.get("call_id"):
        record_audit(user["username"], user["role"], "Call refused", device_id=device_id, detail="already in a call")
        await send_json(ws, {"type": "error", "message": "that device is already in a call"})
        return
    call_id = uuid.uuid4().hex[:12]
    calls[call_id] = {
        "device_id": device_id,
        "user_id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "user_ws": ws,
        "remote": client_addr.get(),
    }
    record_audit(user["username"], user["role"], "Started a call", device_id=device_id)
    info["call_id"] = call_id
    log.info("call %s user %s device %s", call_id, user["username"], device_id)
    await send_json(ws, {"type": "ringing", "call_id": call_id, "device_id": device_id})
    await send_json(info["ws"], {"type": "ring", "call_id": call_id})
    await push_devices()


async def api_hours(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    cameras = device_snapshot(user)
    for item in cameras:
        rules = load_hour_rules(item["device_id"])
        item["windows"] = [
            {
                "id": rule["id"],
                "kind": rule["kind"],
                "repeat": rule["repeat"],
                "days": sorted(parse_days(rule["days"])),
                "start_min": rule["start_min"],
                "end_min": rule["end_min"],
                "on_date": rule["on_date"],
                "label": window_label(rule),
            }
            for rule in rules
        ]
    return web.json_response(
        {"timezone": hub_zone_name(), "zones": list(HOUSE_ZONES), "cameras": cameras}
    )


async def api_hours_zone(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    name = (body.get("timezone") or "").strip()
    if name not in HOUSE_ZONES:
        raise web.HTTPBadRequest(text="choose a time zone from the list")
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise web.HTTPBadRequest(text="that time zone is not available on the hub")
    cfg = load_config()
    cfg["timezone"] = name
    save_config(cfg)
    record_audit(user["username"], user["role"], "Set the house time zone", detail=name)
    await push_devices()
    await push_service()
    return web.json_response({"ok": True, "timezone": name})


async def api_hours_add(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    try:
        device_id, kind, repeat, days, start_min, end_min, on_date = clean_window(body)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    with db_lock:
        cursor = db.execute(
            """
            INSERT INTO hours (device_id, kind, repeat, days, start_min, end_min, on_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (device_id, kind, repeat, days, start_min, end_min, on_date),
        )
        db.commit()
        rule_id = cursor.lastrowid
    rule = {
        "kind": kind,
        "repeat": repeat,
        "days": days,
        "start_min": start_min,
        "end_min": end_min,
        "on_date": on_date,
    }
    record_audit(
        user["username"],
        user["role"],
        "Added an operating window",
        device_id=device_id,
        detail=window_label(rule),
    )
    await push_devices()
    await push_service()
    return web.json_response({"ok": True, "id": rule_id})


async def api_hours_remove(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    try:
        rule_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="that window was not found")
    with db_lock:
        row = db.execute(
            "SELECT id, device_id, kind, repeat, days, start_min, end_min, on_date FROM hours WHERE id = ?",
            (rule_id,),
        ).fetchone()
        if row is None:
            raise web.HTTPBadRequest(text="that window was not found")
        db.execute("DELETE FROM hours WHERE id = ?", (rule_id,))
        db.commit()
    record_audit(
        user["username"],
        user["role"],
        "Removed an operating window",
        device_id=row["device_id"],
        detail=window_label(dict(row)),
    )
    await push_devices()
    await push_service()
    return web.json_response({"ok": True})


async def api_hours_limits(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    device_id = (body.get("device_id") or "").strip()
    if not known_device(device_id):
        raise web.HTTPBadRequest(text="that camera is not on the hub")
    try:
        treat_max = clean_daily_max(body.get("treat_daily_max"), "Treat limit")
        scent_max = clean_daily_max(body.get("scent_daily_max"), "Scent limit")
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    with db_lock:
        db.execute(
            "UPDATE devices SET treat_daily_max = ?, scent_daily_max = ? WHERE device_id = ?",
            (treat_max, scent_max, device_id),
        )
        db.commit()
    record_audit(
        user["username"],
        user["role"],
        "Set daily limits",
        device_id=device_id,
        detail="treats %s, scent %s" % (treat_max, scent_max),
    )
    await push_devices()
    return web.json_response({"ok": True})


async def hours_loop():
    previous = None
    while True:
        try:
            current = {}
            for device_id in saved_device_ids():
                status = camera_access(device_id)
                current[device_id] = (
                    status["open"],
                    status["schedule_message"],
                    status["treat_blocked"],
                    status["scent_blocked"],
                )
            if previous is not None and current != previous:
                await push_devices()
                opened = {device_id: values[0] for device_id, values in current.items()}
                was_open = {device_id: values[0] for device_id, values in previous.items()}
                if opened != was_open:
                    await push_service()
            previous = current
        except Exception:
            log.exception("hours check failed")
        await asyncio.sleep(30)


async def hours_watch(_app):
    task = asyncio.create_task(hours_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def task_watch(_app):
    task = asyncio.create_task(task_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def api_audit(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    username = (request.query.get("username") or "").strip()
    device = (request.query.get("device") or "").strip()
    try:
        limit = int(request.query.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 200))
    try:
        before = int(request.query.get("before") or 0)
    except (TypeError, ValueError):
        before = 0
    clauses = []
    args = []
    if before > 0:
        clauses.append("id < ?")
        args.append(before)
    if username:
        pattern = like_pattern(username)
        clauses.append("(username LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\')")
        args.extend([pattern, pattern])
    if device:
        pattern = like_pattern(device)
        clauses.append(
            """(
                device_id LIKE ? ESCAPE '\\'
                OR nickname LIKE ? ESCAPE '\\'
                OR subject LIKE ? ESCAPE '\\'
                OR device_id IN (
                    SELECT device_id FROM devices WHERE nickname LIKE ? ESCAPE '\\'
                )
            )"""
        )
        args.extend([pattern, pattern, pattern, pattern])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with db_lock:
        rows = db.execute(
            "SELECT id, at, username, role, action, device_id, nickname, subject, detail, remote "
            "FROM audit" + where + " ORDER BY id DESC LIMIT ?",
            args + [limit],
        ).fetchall()
    return web.json_response({"entries": [dict(row) for row in rows]})


async def api_screen(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    return web.json_response(screen_state())


async def api_screen_save(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    kind = request.query.get("which") or ""
    try:
        screen_path(kind)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "image":
        raise web.HTTPBadRequest(text="choose a JPEG")
    chunks = bytearray()
    while True:
        piece = await field.read_chunk(65536)
        if not piece:
            break
        chunks.extend(piece)
        if len(chunks) > 1024 * 1024:
            raise web.HTTPBadRequest(text="use a JPEG no larger than 1 MB")
    try:
        save_jpeg(kind, bytes(chunks))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    label = "ready" if kind == "ready" else "out of service"
    record_audit(user["username"], user["role"], "Saved the %s screen picture" % label)
    await push_screen()
    state = screen_state()
    state["ok"] = True
    return web.json_response(state)


async def api_screen_remove(request):
    user = request_user(request)
    if user is None or user["role"] != "admin":
        raise web.HTTPForbidden()
    body = await request.json()
    kind = body.get("which") or ""
    try:
        path = screen_path(kind)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    if os.path.isfile(path):
        os.remove(path)
    label = "ready" if kind == "ready" else "out of service"
    record_audit(user["username"], user["role"], "Removed the %s screen picture" % label)
    await push_screen()
    state = screen_state()
    state["ok"] = True
    return web.json_response(state)


async def screen_image(request):
    kind = request.match_info["which"]
    try:
        path = screen_path(kind)
    except ValueError:
        raise web.HTTPNotFound()
    if not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


def make_app():
    app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[remember_client])
    app.cleanup_ctx.append(hours_watch)
    app.cleanup_ctx.append(task_watch)
    app.add_routes(
        [
            web.get("/ping.json", ping),
            web.get("/ice.json", ice),
            web.get("/petchatzDevice2.html", device_page),
            web.get("/", login_page),
            web.get("/calls", calls_page),
            web.get("/users", users_page),
            web.get("/devices", devices_page),
            web.get("/settings", settings_page),
            web.get("/audit", audit_page),
            web.get("/schedule", schedule_page),
            web.get("/app.css", app_css),
            web.get("/common.js", common_js),
            web.post("/login", login),
            web.post("/logout", logout),
            web.get("/api/session", api_session),
            web.get("/api/devices", api_devices),
            web.get("/api/users", api_users),
            web.post("/api/users", api_create_user),
            web.post("/api/users/delete", api_delete_user),
            web.post("/api/guest", api_guest),
            web.post("/api/assign", api_assign),
            web.post("/api/account", api_account),
            web.post("/api/password", api_password),
            web.post("/api/device", api_device_save),
            web.post("/api/device/remove", api_device_remove),
            web.post("/api/device/deploy", api_device_deploy),
            web.post("/api/treat", api_treat),
            web.post("/api/scent", api_scent),
            web.get("/screen/{which}.jpg", screen_image),
            web.get("/api/screen", api_screen),
            web.post("/api/screen", api_screen_save),
            web.post("/api/screen/remove", api_screen_remove),
            web.get("/api/settings", api_settings),
            web.post("/api/settings", api_settings_save),
            web.post("/api/mail/test", api_mail_test),
            web.get("/api/tasks", api_tasks),
            web.post("/api/tasks", api_task_save),
            web.post("/api/tasks/run", api_task_run),
            web.post("/api/tunnel/install", api_tunnel_install),
            web.get("/api/audit", api_audit),
            web.get("/api/hours", api_hours),
            web.post("/api/hours", api_hours_add),
            web.post("/api/hours/remove", api_hours_remove),
            web.post("/api/hours/limits", api_hours_limits),
            web.post("/api/hours/timezone", api_hours_zone),
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
