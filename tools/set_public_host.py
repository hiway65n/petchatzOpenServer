"""Set public_host in the hub config. Run on the hub. Prints the name only."""

import json
import os
import sys

path = "/opt/petchatz-hub/config.json"
host = sys.argv[1].strip().lower().rstrip(".")
with open(path, encoding="utf-8") as fh:
    cfg = json.load(fh)
cfg["public_host"] = host
temporary = path + ".tmp"
with open(temporary, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
print(cfg["public_host"])
print(cfg.get("lan_ip", ""))
