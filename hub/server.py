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
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import WSMsgType, web

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
                detail TEXT NOT NULL DEFAULT ''
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
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "enabled" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        if "expires_at" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN expires_at INTEGER")
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


def device_profile(device_id):
    with db_lock:
        row = db.execute(
            "SELECT nickname, width, height, frame_rate, echo_cancel, noise_suppression, treats_enabled, scent_enabled, treat_daily_max, scent_daily_max FROM devices WHERE device_id = ?",
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
    }


def like_pattern(value):
    text = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + text + "%"


def record_audit(username, role, action, device_id="", nickname=None, subject="", detail=""):
    try:
        if nickname is None and device_id:
            nickname = device_profile(device_id)["nickname"]
        with db_lock:
            db.execute(
                """
                INSERT INTO audit (at, username, role, action, device_id, nickname, subject, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
    actor = user_by_id(call["user_id"])
    record_audit(
        call.get("username") or (actor["username"] if actor else ""),
        call.get("role") or (actor["role"] if actor else ""),
        "Call ended",
        device_id=call["device_id"],
        detail=reason,
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
    return web.Response(
        text=read_html(name),
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
    return web.json_response({"iceServers": ice_servers(audience)})


async def device_page(_request):
    return web.Response(
        text=read_html("device.html"),
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
    row = user_by_name(username)
    if row is None or not check_password(password, row["password"]):
        if username:
            record_audit(username, "", "Sign-in failed", detail="wrong username or password")
        raise web.HTTPFound("/?error=1")
    if not account_open(row):
        record_audit(row["username"], row["role"], "Sign-in failed", detail="account is off or expired")
        raise web.HTTPFound("/?error=2")
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
            "SELECT id, username, role, enabled, expires_at FROM users ORDER BY username"
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
        created = add_user(username, password, role)
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
    record_audit(
        user["username"],
        user["role"],
        "Saved the external address",
        detail=public_host or "(cleared)",
    )
    return web.json_response({"ok": True, "public_host": public_host})


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
    body = await request.json()
    new_password = body.get("new") or ""
    if len(new_password) < 6:
        raise web.HTTPBadRequest(text="new password must be at least 6 characters")
    target_name = (body.get("username") or "").strip()
    drop_sessions = False
    if target_name and (target_name != user["username"] or user["role"] == "admin"):
        if user["role"] != "admin":
            raise web.HTTPForbidden(text="only an admin can change another password")
        target = user_by_name(target_name)
        if target is None:
            raise web.HTTPBadRequest(text="that user does not exist")
        user_id = target["id"]
        drop_sessions = target_name != user["username"]
        log.info("password set for %s", target_name)
        audit_action = "Set a password"
        audit_subject = target_name
    else:
        current = user_by_name(user["username"])
        if not check_password(body.get("old") or "", current["password"]):
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
    with db_lock:
        db.execute(
            "UPDATE users SET password = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        if drop_sessions:
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        db.commit()
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
            remember_device(device_id)
            await send_json(
                ws,
                {
                    "type": "hello",
                    "iceServers": ice_servers("lan"),
                    "capture": capture_payload(device_id),
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
            "SELECT id, at, username, role, action, device_id, nickname, subject, detail "
            "FROM audit" + where + " ORDER BY id DESC LIMIT ?",
            args + [limit],
        ).fetchall()
    return web.json_response({"entries": [dict(row) for row in rows]})


def make_app():
    app = web.Application()
    app.cleanup_ctx.append(hours_watch)
    app.add_routes(
        [
            web.get("/ping.json", ping),
            web.get("/ice.json", ice),
            web.get("/petchatzDevice2.html", device_page),
            web.get("/", login_page),
            web.get("/calls", calls_page),
            web.get("/users", users_page),
            web.get("/devices", devices_page),
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
            web.post("/api/assign", api_assign),
            web.post("/api/account", api_account),
            web.post("/api/password", api_password),
            web.post("/api/device", api_device_save),
            web.post("/api/device/remove", api_device_remove),
            web.post("/api/device/deploy", api_device_deploy),
            web.post("/api/treat", api_treat),
            web.post("/api/scent", api_scent),
            web.get("/api/settings", api_settings),
            web.post("/api/settings", api_settings_save),
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
