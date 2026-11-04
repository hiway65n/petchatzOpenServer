"""Run on the hub. Print ICE urls only, never passwords."""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

ctx = ssl._create_unverified_context()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


print("ping", get("https://127.0.0.1/ping.json")[0])
print("ice-anon", get("https://127.0.0.1/ice.json")[0])

cfg = json.load(open("/opt/petchatz-hub/config.json", encoding="utf-8"))
lan = cfg["lan_ip"]
public = cfg.get("public_host") or lan
body = urllib.parse.urlencode(
    {"username": cfg["admin_user"], "password": cfg["admin_pass"]}
).encode()


class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), NoRedir)
req = urllib.request.Request("https://%s/login" % lan, data=body, method="POST")
try:
    resp = opener.open(req, timeout=8)
    status, hdrs = resp.status, resp.headers
except urllib.error.HTTPError as exc:
    status, hdrs = exc.code, exc.headers
cookie = (hdrs.get("Set-Cookie") or "").split(";", 1)[0]
print("login", status, "cookie", cookie.startswith("sid="))


def urls_for(url, host):
    req = urllib.request.Request(url, headers={"Cookie": cookie, "Host": host})
    with opener.open(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    found = []
    for item in data.get("iceServers", []):
        value = item.get("urls")
        found.extend(value if isinstance(value, list) else [value])
    return found


print("lan", urls_for("https://127.0.0.1/ice.json", lan))
print("public", urls_for("https://127.0.0.1/ice.json", public))
