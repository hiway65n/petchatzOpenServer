import base64
import json
import os
import socket
import urllib.request

targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5).read().decode())
page = None
for item in targets:
    if item.get("type") == "page":
        page = item
        break
if page is None:
    raise SystemExit("no page")
print(page.get("url"))
path = page["webSocketDebuggerUrl"].split(":9222", 1)[1]
sock = socket.create_connection(("127.0.0.1", 9222), 5)
key = base64.b64encode(os.urandom(16)).decode()
req = "GET %s HTTP/1.1\r\nHost: 127.0.0.1:9222\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, key)
sock.sendall(req.encode())
buf = b""
while b"\r\n\r\n" not in buf:
    buf += sock.recv(4096)
print(buf.split(b"\r\n", 1)[0])
payload = json.dumps({
    "id": 1,
    "method": "Runtime.evaluate",
    "params": {"expression": "location.reload(true)", "returnByValue": True},
}).encode()
mask = os.urandom(4)
frame = bytearray([0x81, 0x80 | len(payload)])
frame.extend(mask)
frame.extend(bytearray(payload[i] ^ mask[i % 4] for i in range(len(payload))))
sock.sendall(frame)
sock.settimeout(5)
try:
    print(sock.recv(1024)[:200])
except Exception as exc:
    print(type(exc).__name__)
sock.close()
print("reload sent")
