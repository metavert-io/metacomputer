"""The input test's web server, run in the test VM (tests/test_input_vm.py): serves this folder on 127.0.0.1:PORT and
records what the pages send: POST /save -> OUT (the textarea's text, exactly), POST /log -> one JSON line in OUT.log."""
import http.server
import json
import sys
import time

PORT, OUT = int(sys.argv[1]), sys.argv[2]


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self):
        n = min(int(self.headers.get("Content-Length") or 0), 100000)
        data = self.rfile.read(n)
        if self.path == "/log":
            with open(OUT + ".log", "a", encoding="utf-8") as f:
                f.write(json.dumps({"port": PORT, "t": round(time.time(), 3), "ev": data.decode("utf-8", "replace")}) + "\n")
        elif self.path == "/save":
            with open(OUT, "wb") as f:
                f.write(data)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
