"""A minimal, stdlib-only fake decision-ledger server for tests — no real
network dependency, no mocking framework, just http.server."""
import http.server
import json
import threading


class FakeCollector:
    def __init__(self):
        self.received: list[tuple[str, dict]] = []
        self.up = True
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                if not outer.up:
                    self.send_response(503)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                outer.received.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if self.path == "/check":
                    resp = {"decision": "allow", "matched_rules": []}
                else:
                    resp = {"event_id": body.get("event_id"), "event_hash": "a" * 64, "seq": 1}
                self.wfile.write(json.dumps(resp).encode())

            def log_message(self, *args):
                pass  # silence default request logging to stderr

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def shutdown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
