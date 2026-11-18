#!/usr/bin/env python3
"""Fixture web server for the benchmark, run INSIDE the VM (loopback only).

Serves a folder of invented pages and logs every request (method, path, query,
form fields, referer) as one JSON line, so a task's check can read what the
browser really sent instead of trusting the agent's report.

  server.py ROOT LOG [PORT]

Endpoints ending in one of ACCEPT (e.g. /submit, /answer) take a GET or POST
and answer with a plain "received" page; everything else is a static file.
"""
import json
import os
import sys
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT, LOG = sys.argv[1], sys.argv[2]
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8000
ACCEPT = ("/submit", "/answer", "/reserve", "/pick", "/ship", "/code")
THANKS = (b"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Received</title></head>"
          b"<body><h1>Thank you</h1><p>Your submission was received.</p>"
          b"<p><a href='/'>Back to the start page</a></p></body></html>")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, *_):
        pass

    def record(self, form=None):
        u = urllib.parse.urlsplit(self.path)
        rec = {"t": round(time.time(), 3), "method": self.command, "path": u.path,
               "query": urllib.parse.parse_qs(u.query, keep_blank_values=True),
               "form": form or {}, "referer": self.headers.get("Referer", ""),
               "agent": (self.headers.get("User-Agent", "") or "")[:80]}
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return u.path

    def thanks(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(THANKS)))
        self.end_headers()
        self.wfile.write(THANKS)

    def do_GET(self):
        path = self.record()
        if path.endswith(ACCEPT):
            return self.thanks()
        return super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, 1 << 20)).decode("utf-8", "replace")
        self.record(urllib.parse.parse_qs(body, keep_blank_values=True))
        return self.thanks()


if __name__ == "__main__":
    os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
