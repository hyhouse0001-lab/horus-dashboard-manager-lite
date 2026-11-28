"""Isolated upstream Blink receiver with an owner-only ingress setup and a fixed HA action."""
import base64
import hashlib
import html
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path("/data/horus-blink")
SCRIPT = "script.horus_doorbell_live_view"
HA = "http://supervisor/core/api"
INGRESS_PROXY = "172.30.32.2"
DUMMY = "http://127.0.0.1/api/webhook/horus-internal"
MAX_AGE = 30
DUPLICATE_SECONDS = 8

def epoch(value):
    if not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d.timestamp() if d.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None

def eligible(event, now):
    if not isinstance(event, dict):
        return False
    if (event.get("schema_version") != 1 or event.get("source") != "blink_live_events"
            or event.get("classification") != "doorbell_press" or event.get("event_type") != "button_press"):
        return False
    key = event.get("event_id")
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        return False
    name = event.get("camera_name")
    # This household has one Blink doorbell. Some ring messages omit camera_name.
    if name is not None and (not isinstance(name, str) or name.strip().casefold() not in ("", "front door bell")):
        return False
    received = epoch(event.get("received_at"))
    if received is None or not -5 <= now - received <= MAX_AGE:
        return False
    for field in ("provider_timestamp", "created_at"):
        if event.get(field):
            parsed = epoch(event[field])
            if parsed is not None and not -5 <= now - parsed <= MAX_AGE:
                return False
            if field == "provider_timestamp" and parsed is None:
                return False
    return True

def ha_request(path, data=None):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise OSError("HA token unavailable")
    request = urllib.request.Request(HA + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="GET" if data is None else "POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read(65536))

def deliver(event, now=None, request=ha_request):
    now = time.time() if now is None else now
    if not eligible(event, now):
        return False
    state = request("/states/" + SCRIPT)
    if state.get("entity_id") != SCRIPT or state.get("state") not in ("on", "off"):
        raise OSError("Existing HORUS doorbell script unavailable")
    last = epoch(state.get("attributes", {}).get("last_triggered"))
    if state["state"] == "on" or (last is not None and -5 <= now - last < DUPLICATE_SECONDS):
        return False
    request("/services/script/turn_on", {"entity_id": SCRIPT})
    return True

class DirectDelivery:
    """Upstream worker calls this instead of a network webhook. No external listener."""
    def __init__(self, url, timeout_seconds=5):
        if url != DUMMY:
            raise ValueError("Only fixed internal bridge is allowed")
    def send(self, event):
        if deliver(event):
            print("HORUS front doorbell press delivered to existing script", flush=True)

def listen():
    from receiver.blink_live_events import ha_delivery, daemon
    ha_delivery.HomeAssistantWebhook = DirectDelivery
    os.environ["BLINK_LIVE_EVENTS_HA_WEBHOOK_URL"] = DUMMY
    daemon.listen(DATA)

def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

class Setup:
    def __init__(self):
        self.lock = threading.Lock()
        self.csrf = secrets.token_urlsafe(32)
        self.pending = None
        self.worker = None
        self.status = "Awaiting owner authorization"

    def ready(self):
        try:
            fcm = json.loads((DATA / "fcm-state.json").read_text())
            auth = json.loads((DATA / "blink-auth.json").read_text())
            return fcm.get("status") == "ready" and all(auth.get(k) for k in ("tier", "account_id", "client_id"))
        except (OSError, ValueError):
            return False

    def start(self):
        if not self.ready():
            return
        if self.worker and self.worker.poll() is None:
            return
        self.worker = subprocess.Popen([sys.executable, __file__, "listen"])
        self.status = "Receiver process started; physical event test still required"

    def stop(self):
        if self.worker and self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait(timeout=3)
        self.worker = None

    def begin(self):
        from receiver.blink_live_events import blink_account as b
        from receiver.blink_live_events.fcm_registration import register_fcm
        self.pending = None
        self.stop()
        if not (DATA / "fcm-state.json").exists():
            register_fcm(None, DATA)
        hardware = str(uuid.uuid4()).upper()
        state = b64(secrets.token_bytes(32))
        verifier = b64(secrets.token_bytes(64))
        challenge = b64(hashlib.sha256(verifier.encode("ascii")).digest())
        url = b._authorization_url(hardware, state, challenge)
        self.pending = (hardware, state, verifier, time.monotonic() + 900, url)
        self.status = "Complete Blink sign-in in your browser"

    def complete(self, callback):
        from receiver.blink_live_events import blink_account as b
        from receiver.blink_live_events.state import atomic_json
        pending, self.pending = self.pending, None
        if pending is None or time.monotonic() > pending[3]:
            raise ValueError("Sign-in expired")
        hardware, state, verifier, _, _ = pending
        code = b._callback_code(callback, state)
        status, _, response = b._request("POST", b.OAUTH_TOKEN_URL,
            headers={"Accept": "application/json"}, form={
                "grant_type": "authorization_code", "redirect_uri": b.OAUTH_REDIRECT_URI,
                "code": code, "code_verifier": verifier, "client_id": b.OAUTH_CLIENT_ID})
        if status != 200:
            raise ValueError("Blink token exchange rejected")
        auth = {"format": 2, "hardware_id": hardware, "oauth_client_id": b.OAUTH_CLIENT_ID,
                **b._token_fields(b._json_object(response, "oauth_token_exchange"))}
        atomic_json(DATA / b.AUTH_STATE_NAME, auth)
        b.associate_fcm_token(DATA)
        self.start()

    def view(self):
        if self.worker is not None and self.worker.poll() is not None:
            self.status = "Receiver stopped; see app logs or select Restart receiver"
        sign_in = ""
        if self.pending is not None and time.monotonic() < self.pending[3]:
            sign_in = ('<p><a target="_blank" rel="noopener noreferrer" href="' +
                       html.escape(self.pending[4], quote=True) + '">Open Blink secure sign-in</a></p>'
                       '<label>Full callback URL from Blink <input name="callback" type="password" '
                       'maxlength="2048" autocomplete="off"></label>'
                       '<button name="op" value="complete">Finish authorization</button>')
        return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
                'content="width=device-width,initial-scale=1"><title>HORUS Blink Events</title></head>'
                '<body><h1>HORUS Blink Events</h1><p>' + html.escape(self.status) +
                '</p><p>Alexa remains unchanged. Only a recent physical doorbell press can call '
                'the existing camera overlay script. Sign in on Blink’s own website; never enter '
                'your password or verification code here.</p><form method="post" action="">'
                '<input type="hidden" name="csrf" value="' + self.csrf + '">' +
                '<button name="op" value="begin">Authorize / reauthorize Blink</button> '
                '<button name="op" value="start">Restart receiver</button> '
                + sign_in + '</form><p>Refresh this page to view status. Test a real press and separate motion.</p></body></html>')

SETUP = Setup()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log callback URLs or form data.

    def respond(self, status, body):
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; base-uri 'none'")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.client_address[0] != INGRESS_PROXY:
            return self.respond(403, "Forbidden")
        with SETUP.lock:
            self.respond(200, SETUP.view())

    def do_POST(self):
        if self.client_address[0] != INGRESS_PROXY:
            return self.respond(403, "Forbidden")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError()
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError()
            self.connection.settimeout(10)
            fields = urllib.parse.parse_qs(self.rfile.read(length).decode(), strict_parsing=True)
            if not secrets.compare_digest(fields.get("csrf", [""])[0], SETUP.csrf):
                return self.respond(403, "Forbidden")
            op = fields.get("op", [""])[0]
            with SETUP.lock:
                try:
                    if op == "begin":
                        SETUP.begin()
                    elif op == "complete":
                        SETUP.complete(fields.get("callback", [""])[0].strip())
                    elif op == "start":
                        SETUP.stop()
                        SETUP.start()
                    else:
                        raise ValueError("Unknown action")
                except Exception as exc:
                    SETUP.status = "Setup failed (" + type(exc).__name__ + "); try authorization again."
                self.respond(200, SETUP.view())
        except (ValueError, OSError):
            self.respond(400, "Invalid request")

def main():
    if sys.argv[1:] == ["listen"]:
        return listen()
    DATA.mkdir(mode=0o700, exist_ok=True)
    if os.getuid() == 0:
        os.chown(DATA, 1000, 1000)
        os.setgroups([])
        os.setgid(1000)
        os.setuid(1000)
    DATA.chmod(0o700)
    SETUP.start()
    server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)
    server.daemon_threads = True
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        SETUP.stop()
        server.server_close()

if __name__ == "__main__":
    main()
