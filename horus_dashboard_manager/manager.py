"""HORUS read-only dashboard inventory for Home Assistant ingress.

No dashboard mutations, no public listener port, no long-lived token in files.
Supervisor WS proxy must be tested on HAOS before installation approval.
"""
import json
import os
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PATH = "fuller-family-test"
MAX_RESPONSE = 200_000


def ha_call(command, **args):
    """One request per authenticated HA websocket; secrets never enter responses."""
    if command not in ("lovelace/dashboards/list", "lovelace/config"):
        raise ValueError("Dashboard command not allowed")
    if command == "lovelace/dashboards/list" and args:
        raise ValueError("Inventory command accepts no arguments")
    if command == "lovelace/config" and args != {"url_path": PATH}:
        raise ValueError("Only approved test dashboard may be read")
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("Supervisor authorization unavailable")
    from websockets.sync.client import connect
    endpoint = "ws://supervisor/core/websocket"
    with connect(endpoint, open_timeout=5, close_timeout=2, max_size=MAX_RESPONSE) as ws:
        hello = json.loads(ws.recv(timeout=5))
        if hello.get("type") != "auth_required":
            raise RuntimeError("Unexpected Home Assistant authentication challenge")
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(ws.recv(timeout=5)).get("type") != "auth_ok":
            raise RuntimeError("Home Assistant authentication failed")
        ws.send(json.dumps({"id": 1, "type": command, **args}))
        result = json.loads(ws.recv(timeout=10))
        if result.get("type") != "result" or result.get("id") != 1 or not result.get("success"):
            raise RuntimeError("Home Assistant dashboard read unavailable")
        return result.get("result")


def read_dashboard(path):
    if path != PATH:
        raise ValueError("Only the approved test dashboard may be read")
    return ha_call("lovelace/config", url_path=path)


def list_dashboards():
    return ha_call("lovelace/dashboards/list")


class Handler(BaseHTTPRequestHandler):
    """Ingress-gated handler: do not expose HA data to sibling containers."""
    def _ingress_allowed(self):
        return self.client_address[0] == "172.30.32.2"

    def _reject_untrusted(self):
        if not self._ingress_allowed():
            self._reply(403, {"error": "ingress_required"})
            return True
        return False

    def _reply(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_RESPONSE:
            status, body = 413, b'{"error":"response_too_large"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self._reject_untrusted():
            return
        path = urlsplit(self.path)
        if path.query or path.fragment:
            self._reply(400, {"error": "query_not_allowed"})
        elif path.path in ("/", "/health"):
            self._reply(200, {"name": "HORUS Dashboard Manager", "version": "0.2.0", "mode": "read-only", "dashboard_writes": "disabled", "backend": "home-assistant-supervisor-websocket"})
        elif path.path == "/dashboards":
            try:
                self._reply(200, {"dashboards": list_dashboards()})
            except Exception as exc:
                logging.error("Dashboard inventory failed: %s", type(exc).__name__)
                self._reply(503, {"error": "dashboard_inventory_unavailable"})
        elif path.path == "/dashboard/fuller-family-test":
            try:
                self._reply(200, {"path": PATH, "config": read_dashboard(PATH)})
            except Exception as exc:
                logging.error("Test dashboard read failed: %s", type(exc).__name__)
                self._reply(503, {"error": "test_dashboard_unavailable"})
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self):
        if self._reject_untrusted():
            return
        self._reply(405, {"error": "writes_disabled"})

    def do_PUT(self):
        if self._reject_untrusted():
            return
        self._reply(405, {"error": "writes_disabled"})

    def do_PATCH(self):
        if self._reject_untrusted():
            return
        self._reply(405, {"error": "writes_disabled"})

    def do_DELETE(self):
        if self._reject_untrusted():
            return
        self._reply(405, {"error": "writes_disabled"})


def main():
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()


if __name__ == "__main__":
    main()
