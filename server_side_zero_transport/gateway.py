#!/usr/bin/env python3
"""Minimal loopback-only HTTP gateway; deliberately has no GPU dependency."""
import argparse
import http.server
import ipaddress
import urllib.error
import urllib.request
from urllib.parse import urlparse


MAX_REQUEST_BYTES = 2 * 1024 * 1024


class Handler(http.server.BaseHTTPRequestHandler):
    upstream = ""

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "invalid Content-Length")
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self.send_error(413, "invalid request size")
            return
        body = self.rfile.read(length)
        headers = {
            "Content-Type": self.headers.get(
                "Content-Type", "application/json"
            )
        }
        authorization = self.headers.get("Authorization")
        if authorization:
            headers["Authorization"] = authorization
        req = urllib.request.Request(
            self.upstream + self.path,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as response:
                payload = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--upstream", required=True)
    args = parser.parse_args()
    try:
        if not ipaddress.ip_address(args.listen).is_loopback:
            raise ValueError
    except ValueError:
        raise SystemExit("gateway listen address must be a literal loopback IP")
    upstream = urlparse(args.upstream)
    try:
        upstream_is_loopback = ipaddress.ip_address(upstream.hostname or "").is_loopback
    except ValueError:
        upstream_is_loopback = False
    if (
        upstream.scheme not in ("http", "https")
        or upstream.username
        or upstream.password
        or not upstream_is_loopback
    ):
        raise SystemExit("gateway upstream must be a credential-free loopback URL")
    Handler.upstream = args.upstream.rstrip("/")
    http.server.ThreadingHTTPServer((args.listen, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
