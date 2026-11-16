"""HORUS Dashboard Manager: single-dashboard, human-approved GitHub bridge.

Allowed target: storage-mode Lovelace dashboard fuller-family-test ONLY.
GitHub repository is private. No tokens are returned to HTTP clients or logs.
Requires an administrator to click Apply; no remote command execution.
"""
import base64
import datetime
import difflib
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

PATH = "fuller-family-test"
PRIVATE_REPO = "hyhouse0001-lab/horus-home-os"
BRANCH = "main"
SNAPSHOT_PATH = "dashboard_sync/fuller-family-test.current.json"
PROPOSAL_PATH = "dashboard_sync/fuller-family-test.proposal.json"
BACKUPS_DIR = "/data/horus_dashboard_backups"
STATE_PATH = "/data/horus_dashboard_state.json"
MAX_CONFIG_BYTES = 850_000
MAX_RESPONSE = 1_200_000
CSRF = secrets.token_urlsafe(32)
LOCK = threading.RLock()
LOG = logging.getLogger("horus-dashboard-manager")


class BridgeError(Exception):
    """Safe, short error suitable for an authenticated ingress page."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def check_config(config, restoring=False):
    if not isinstance(config, dict) or not isinstance(config.get("views"), list) or not config["views"]:
        raise BridgeError("Configuration must contain at least one dashboard view")
    if len(canonical(config).encode("utf-8")) > MAX_CONFIG_BYTES:
        raise BridgeError("Dashboard configuration is too large")
    # Until explicitly reviewed, prevent direct unlock/disarm shortcuts.
    disallowed = {"lock.unlock", "alarm_control_panel.alarm_disarm"}
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if not restoring and key in ("service", "perform_action") and isinstance(value, str) and value in disallowed:
                    raise BridgeError("Unlock/disarm shortcuts require separate security review")
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(config)
    return config


def ha_call(command, *, restore_original=False, **args):
    if command not in ("lovelace/config", "lovelace/config/save"):
        raise BridgeError("Unsupported Home Assistant operation")
    if args.get("url_path") != PATH:
        raise BridgeError("Only Fuller Family Test may be accessed")
    if command == "lovelace/config" and set(args) != {"url_path"}:
        raise BridgeError("Invalid dashboard read request")
    if command == "lovelace/config/save" and set(args) != {"url_path", "config"}:
        raise BridgeError("Invalid dashboard save request")
    if command == "lovelace/config/save":
        check_config(args["config"], restoring=restore_original)
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise BridgeError("Supervisor token unavailable")
    from websockets.sync.client import connect
    with connect("ws://supervisor/core/websocket",
                 open_timeout=8, close_timeout=3, max_size=MAX_RESPONSE) as ws:
        greeting = json.loads(ws.recv(timeout=8))
        if greeting.get("type") != "auth_required":
            raise BridgeError("Unexpected authentication challenge")
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(ws.recv(timeout=8)).get("type") != "auth_ok":
            raise BridgeError("Home Assistant authentication rejected")
        ws.send(json.dumps({"id": 1, "type": command, **args}))
        response = json.loads(ws.recv(timeout=20))
        if response.get("type") != "result" or response.get("id") != 1 or not response.get("success"):
            raise BridgeError("Home Assistant rejected the dashboard operation")
        return response.get("result")


def read_dashboard():
    result = ha_call("lovelace/config", url_path=PATH)
    if not isinstance(result, dict) or not isinstance(result.get("views"), list):
        raise BridgeError("Home Assistant returned an unexpected dashboard format")
    return result


def save_dashboard(config, restoring=False):
    ha_call("lovelace/config/save", restore_original=restoring,
            url_path=PATH, config=check_config(config, restoring=restoring))


def github_token():
    try:
        with open("/data/options.json", encoding="utf-8") as handle:
            value = json.load(handle).get("github_token", "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (OSError, ValueError):
        pass
    raise BridgeError("Configure a GitHub token in the add-on Configuration tab first")


def github_request(path, method="GET", payload=None):
    token = github_token()
    api = "https://api.github.com/repos/" + PRIVATE_REPO + "/contents/" + quote(path, safe="/")
    if method == "GET":
        api += "?ref=" + BRANCH
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = Request(api, data=body, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "HORUS-Dashboard-Manager/0.2",
        "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return None
        raise BridgeError("GitHub request failed (HTTP " + str(exc.code) + ")") from None
    except (URLError, TimeoutError):
        raise BridgeError("GitHub is unreachable from this add-on") from None


def github_get(path):
    record = github_request(path)
    if record is None:
        return None, None
    if not isinstance(record, dict) or record.get("encoding") != "base64":
        raise BridgeError("Unexpected GitHub file response")
    try:
        raw = base64.b64decode(record["content"], validate=False)
        if len(raw) > MAX_RESPONSE:
            raise BridgeError("GitHub file exceeds size limit")
        return json.loads(raw.decode("utf-8")), record["sha"]
    except (KeyError, ValueError, UnicodeDecodeError):
        raise BridgeError("GitHub file is not valid JSON") from None


def github_put(path, document, message):
    current, sha = github_get(path)
    _ = current
    payload = {
        "message": message,
        "branch": BRANCH,
        "content": base64.b64encode(
            (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        ).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    return github_request(path, "PUT", payload)


def snapshot_document(config):
    return {
        "schema_version": 1,
        "dashboard": PATH,
        "base_sha256": digest(config),
        "synced_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "config": config,
    }


def sync_to_github():
    live = read_dashboard()
    github_put(SNAPSHOT_PATH, snapshot_document(live),
               "Sync live Fuller Family Test dashboard for HORUS review")
    return {"views": len(live["views"]), "hash": digest(live)}


def load_proposal():
    document, sha = github_get(PROPOSAL_PATH)
    if document is None:
        raise BridgeError("No proposal in the private GitHub repository yet")
    if not isinstance(document, dict):
        raise BridgeError("Invalid proposal structure")
    if document.get("schema_version") != 1 or document.get("dashboard") != PATH:
        raise BridgeError("Proposal targets the wrong dashboard or version")
    base = document.get("base_sha256", "")
    summary = document.get("summary", "")
    if not isinstance(base, str) or len(base) != 64 or any(ch not in "0123456789abcdef" for ch in base):
        raise BridgeError("Proposal has an invalid base hash")
    if not isinstance(summary, str) or not (1 <= len(summary) <= 500):
        raise BridgeError("Proposal requires a short change summary")
    check_config(document.get("config"))
    return document, sha


def require_fresh(proposal, live):
    if not hmac.compare_digest(proposal["base_sha256"], digest(live)):
        raise BridgeError("Dashboard changed since sync. Sync again before applying.")
    if digest(proposal["config"]) == digest(live):
        raise BridgeError("Proposal is identical to the current dashboard")


def backup(config):
    os.makedirs(BACKUPS_DIR, mode=0o700, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = timestamp + "-" + digest(config)[:12] + ".json"
    file_path = os.path.join(BACKUPS_DIR, name)
    encoded = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return name


def state_read():
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def state_write(info):
    os.makedirs(os.path.dirname(STATE_PATH), mode=0o700, exist_ok=True)
    temporary = STATE_PATH + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, STATE_PATH)


def apply_proposal(requested_sha):
    proposal, sha = load_proposal()
    if not isinstance(requested_sha, str) or not hmac.compare_digest(sha, requested_sha):
        raise BridgeError("Proposal changed since preview. Review the new version first.")
    before = read_dashboard()
    require_fresh(proposal, before)
    prior_hash = digest(before)
    name = backup(before)
    target = proposal["config"]
    try:
        save_dashboard(target)
        actual = read_dashboard()
        if digest(actual) != digest(target):
            raise BridgeError("Saved dashboard did not match the proposed version")
    except Exception:
        # Best-effort repair to the verified pre-apply configuration.
        try:
            save_dashboard(before, restoring=True)
            if digest(read_dashboard()) != prior_hash:
                LOG.error("Rollback verification failed; check dashboard manually")
        except Exception:
            LOG.exception("Automatic dashboard rollback failed")
        raise
    state_write({
        "backup": name,
        "before_sha256": prior_hash,
        "after_sha256": digest(actual),
        "proposal_sha": sha,
        "applied_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "rolled_back": False,
    })
    try:
        github_put(SNAPSHOT_PATH, snapshot_document(actual),
                   "Record applied Fuller Family Test dashboard revision")
        synced = True
    except Exception:
        LOG.warning("Dashboard saved, but post-apply GitHub sync failed")
        synced = False
    return {"views": len(actual["views"]), "backup": name, "synced": synced}


def rollback_last():
    state = state_read()
    name = state.get("backup", "")
    if state.get("rolled_back") or not isinstance(name, str) or not name.endswith(".json"):
        raise BridgeError("There is no active deployment to roll back")
    if os.path.basename(name) != name:
        raise BridgeError("Invalid backup record")
    live = read_dashboard()
    if digest(live) != state.get("after_sha256"):
        raise BridgeError("Dashboard changed after deployment; refusing to overwrite it")
    try:
        with open(os.path.join(BACKUPS_DIR, name), encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        raise BridgeError("Backup is unavailable or damaged") from None
    if digest(previous) != state.get("before_sha256"):
        raise BridgeError("Backup hash verification failed")
    backup(live)
    save_dashboard(previous, restoring=True)
    if digest(read_dashboard()) != digest(previous):
        raise BridgeError("Restored configuration did not verify")
    state["rolled_back"] = True
    state_write(state)
    try:
        github_put(SNAPSHOT_PATH, snapshot_document(previous),
                   "Record rollback of Fuller Family Test dashboard")
    except Exception:
        LOG.warning("Rollback succeeded, but GitHub snapshot sync failed")
    return {"views": len(previous["views"])}


def page(title, content):
    style = """body{font:16px system-ui,sans-serif;max-width:860px;margin:3rem auto;
    padding:1rem;color:#ebf0f7;background:#101722}main{background:#1d293b;border-radius:14px;
    padding:1.5rem}a{color:#a9ceff}button{background:#397fe0;color:white;border:0;border-radius:8px;
    padding:13px 18px;font:inherit;cursor:pointer;margin:7px 0}button:disabled{background:#667281}
    .danger{background:#ac4853}.muted{color:#bdc9d8}form{margin:10px 0}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:55vh;overflow:auto;
    border:1px solid #425167;padding:12px;background:#101722}
    code{overflow-wrap:anywhere}h1{font-size:1.45rem}"""
    body = ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta charset="utf-8"><title>' + html.escape(title) + '</title>'
            '<style>' + style + '</style></head><body><main><h1>' +
            html.escape(title) + '</h1>' + content + '</main></body></html>')
    return body.encode("utf-8")


def form_button(action, label, extra="", danger=False):
    return ('<form action="' + html.escape(action, quote=True) + '" method="post">'
            '<input type="hidden" name="csrf" value="' + html.escape(CSRF, quote=True) + '">'
            + extra + '<button class="' + ("danger" if danger else "") +
            '" type="submit">' + html.escape(label) + '</button></form>')


class Handler(BaseHTTPRequestHandler):
    def allowed(self):
        # Requests must pass through Home Assistant's admin-only ingress proxy.
        if self.client_address[0] != "172.30.32.2":
            self.respond(403, json.dumps({"error": "ingress_required"}).encode("utf-8"), "application/json")
            return False
        return True

    def respond(self, status, body, media="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def show(self, status, title, content):
        self.respond(status, page(title, content))

    def do_GET(self):
        if not self.allowed():
            return
        url = urlsplit(self.path)
        if url.query or url.fragment:
            self.show(400, "Invalid request", "<p>Query parameters are not supported.</p>")
            return
        if url.path == "/health":
            info = {"status": "running", "mode": "single-dashboard", "target": PATH, "version": "0.2.0"}
            self.respond(200, json.dumps(info).encode("utf-8"), "application/json")
        elif url.path == "/test":
            try:
                current = read_dashboard()
                info = {"status": "connected", "dashboard": "Fuller Family Test",
                        "views": len(current["views"]), "mode": "approval-required"}
                self.respond(200, json.dumps(info).encode("utf-8"), "application/json")
            except Exception as exc:
                self.show(503, "Connection failed", "<p>" + html.escape(str(exc)) + "</p>")
        elif url.path == "/":
            try:
                enabled = bool(github_token())
            except BridgeError:
                enabled = False
            state = state_read()
            content = ('<p class="muted">Only target: <strong>Fuller Family Test</strong> ('
                       + html.escape(PATH) + '). No automated deployment.</p>'
                       '<p><a href="test">Test Home Assistant connection</a></p>')
            if not enabled:
                content += ('<p><strong>Setup needed:</strong> Go to the add-on Configuration tab, '
                            'set the GitHub token, save, and restart this add-on.</p>')
            else:
                content += ('<p>Private GitHub connection configured. '
                            'Sync the live dashboard so ChatGPT can inspect it.</p>'
                            + form_button("/sync", "1. Sync current dashboard to private GitHub")
                            + '<p><a href="preview">2. Preview proposed update</a></p>')
                if state.get("backup") and not state.get("rolled_back"):
                    content += form_button("/rollback", "Restore previous dashboard", danger=True)
            content += ('<p class="muted">Every edit requires explicit approval in this interface. '
                        'All other Home Assistant dashboards are excluded.</p>')
            self.show(200, "HORUS Dashboard Manager", content)
        elif url.path == "/preview":
            with LOCK:
                try:
                    proposed, sha = load_proposal()
                    live = read_dashboard()
                    require_fresh(proposed, live)
                    before = json.dumps(live, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
                    after = json.dumps(proposed["config"], ensure_ascii=False, indent=2, sort_keys=True).splitlines()
                    changes = "\n".join(difflib.unified_diff(
                        before, after, fromfile="CURRENT", tofile="PROPOSED", lineterm=""))
                    display_diff = changes[:110_000]
                    if len(changes) > len(display_diff):
                        raise BridgeError("Proposal diff exceeds safe preview size")
                    extra = ('<input type="hidden" name="proposal_sha" value="' +
                             html.escape(sha, quote=True) + '">')
                    content = ('<p><strong>Change:</strong> ' + html.escape(proposed["summary"]) + '</p>'
                               '<p>Current views: ' + str(len(live["views"])) +
                               ' | Proposed views: ' + str(len(proposed["config"]["views"])) + '</p>'
                               '<p>Proposed changes (JSON diff):</p><pre>' +
                               html.escape(display_diff) + '</pre>' +
                               form_button("/apply", "Approve and apply this dashboard update", extra) +
                               '<p><a href="./">Cancel</a></p>')
                    self.show(200, "Review Fuller Family Test update", content)
                except Exception as exc:
                    self.show(409, "Not ready to apply",
                              "<p>" + html.escape(str(exc)) + '</p><p><a href="./">Back</a></p>')
        else:
            self.show(404, "Not found", "<p>That action is not available.</p>")

    def do_POST(self):
        if not self.allowed():
            return
        route = urlsplit(self.path)
        if route.path not in ("/sync", "/apply", "/rollback") or route.query:
            self.show(405, "Not allowed", "<p>No other write operations are supported.</p>")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 4096:
                raise BridgeError("Invalid request length")
            data = parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
            supplied = data.get("csrf", [""])[0]
            if not hmac.compare_digest(CSRF, supplied):
                raise BridgeError("Approval form expired. Reopen the manager.")
            with LOCK:
                if route.path == "/sync":
                    result = sync_to_github()
                    title = "Dashboard synced"
                    message = ("<p>Current dashboard safely copied to the private GitHub repository. "
                               "Views: " + str(result["views"]) + ".</p>"
                               "<p>ChatGPT can now read the snapshot and prepare a proposal.</p>")
                elif route.path == "/apply":
                    sha = data.get("proposal_sha", [""])[0]
                    result = apply_proposal(sha)
                    title = "Dashboard updated"
                    message = ("<p>Fuller Family Test saved and verified with " +
                               str(result["views"]) + " view(s). Automatic backup created.</p>")
                    if not result["synced"]:
                        message += "<p>GitHub post-deployment sync failed. Use Sync current dashboard.</p>"
                else:
                    result = rollback_last()
                    title = "Dashboard restored"
                    message = ("<p>Last deployed dashboard restored and verified. Views: " +
                               str(result["views"]) + ".</p>")
            LOG.info("%s succeeded (Fuller Family Test only)", route.path)
            self.show(200, title, message + '<p><a href="./">Back to dashboard manager</a></p>')
        except Exception as exc:
            LOG.warning("Action %s failed: %s", route.path, type(exc).__name__)
            detail = str(exc) if isinstance(exc, BridgeError) else "Unexpected operation failure; inspect add-on logs"
            self.show(409, "Action failed", "<p>" + html.escape(detail) +
                      '</p><p><a href="./">Back to manager</a></p>')

    def do_PUT(self):
        self.show(405, "Not allowed", "<p>Only approved UI actions are supported.</p>")

    do_PATCH = do_PUT
    do_DELETE = do_PUT


if __name__ == "__main__":
    LOG.setLevel(logging.INFO)
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
