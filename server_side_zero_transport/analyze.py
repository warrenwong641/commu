#!/usr/bin/env python3
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    run = json.load(f)
print(json.dumps({
    "http_status": run["status"],
    "server_local_elapsed_ms": run["elapsed_ms"],
    "modeled_uplink_ms": 0,
    "modeled_downlink_ms": 0,
}, indent=2))

