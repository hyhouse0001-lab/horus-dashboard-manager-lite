"""Fixed-target 15-second GitHub watcher. Never updates anything but HORUS Dashboard Manager."""
import json
import logging
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

LOG = logging.getLogger("horus-update-watcher")
REPO = "hyhouse0001-lab/horus-dashboard-manager-lite"
RAW_CONFIG = "https://raw.githubusercontent.com/" + REPO + "/main/horus_dashboard_manager/config.yaml"
CI_URL = "https://api.github.com/repos/" + REPO + "/actions/workflows/manager-ci.yml/runs?branch=main&per_page=1"
TARGET = "horus_dashboard_manager"
NAME = "HORUS Dashboard Manager Lite"
REPO_URL = "https://github.com/" + REPO
INTERVAL = 15
COOLDOWN = 600


def parse_version(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", raw):
        raise ValueError("Invalid version format")
    return tuple(int(v) for v in raw.split("."))


def version_from_manifest(content):
    match = re.search(r"(?m)^version:\s*['\"]?(\d{1,4}\.\d{1,4}\.\d{1,4})['\"]?\s*$", content)
    if match is None:
        raise ValueError("Published version missing")
    parse_version(match[1])
    return match[1]


def fetch(url, method="GET", payload=None, auth=None, timeout=15):
    domains = ("https://raw.githubusercontent.com/" + REPO + "/",
               "https://api.github.com/repos/" + REPO + "/",
               "http://supervisor/")
    if not any(url.startswith(prefix) for prefix in domains):
        raise ValueError("Unapproved fetch destination")
    headers = {"User-Agent": "HORUS-Update-Watcher/0.1", "Accept": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + auth
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    with urlopen(Request(url, headers=headers, method=method, data=body), timeout=timeout) as response:
        data = response.read(400_000)
    if len(data) >= 400_000:
        raise RuntimeError("Response size cap reached")
    return data


def remote_version(now=None):
    # Unique time bucket bypasses GitHub Raw's public CDN cache.
    if now is None:
        now = time.time()
    url = RAW_CONFIG + "?watch=" + str(int(now) // INTERVAL)
    data = fetch(url)
    if len(data) > 16_384:
        raise RuntimeError("GitHub version manifest too large")
    return version_from_manifest(data.decode("utf-8"))


def ci_passed():
    results = json.loads(fetch(CI_URL))
    runs = results.get("workflow_runs") or []
    if not isinstance(runs, list) or not runs:
        return False
    run = runs[0]
    return run.get("status") == "completed" and run.get("conclusion") == "success" and run.get("head_branch") == "main"


def supervisor(method, path, payload=None, timeout=15):
    if not (path.startswith("/addons") or path.startswith("/store/")):
        raise ValueError("Supervisor path not allowed")
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN missing; S6 launcher required")
    data = json.loads(fetch("http://supervisor" + path, method, payload, token, timeout))
    if data.get("result") != "ok":
        raise RuntimeError("Supervisor rejected operation: " + str(data.get("message", "unknown"))[:100])
    return data.get("data")


def discover():
    installed = supervisor("GET", "/addons")
    candidates = installed.get("addons", []) if isinstance(installed, dict) else installed
    if not isinstance(candidates, list):
        raise RuntimeError("Unrecognized Supervisor app listing")
    matches = [row["slug"] for row in candidates if isinstance(row, dict)
               and isinstance(row.get("slug"), str)
               and row["slug"].endswith("_" + TARGET) and row.get("name") == NAME]
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one installed HORUS manager, got " + str(len(matches)))
    slug = matches[0]
    info = supervisor("GET", "/addons/" + quote(slug, safe="") + "/info")
    if not isinstance(info, dict) or info.get("name") != NAME or info.get("slug", slug) != slug:
        raise RuntimeError("Manager identity mismatch")
    if info.get("url") and info["url"].rstrip("/") != REPO_URL:
        raise RuntimeError("Manager repository URL mismatch")
    return slug, info


def update_if_needed(remote):
    slug, info = discover()
    installed = info.get("version")
    if parse_version(remote) <= parse_version(installed):
        return "up_to_date", installed
    LOG.info("Verified %s release newer than installed %s; refreshing store", remote, installed)
    supervisor("POST", "/store/reload", {})
    _, refreshed = discover()
    latest = refreshed.get("version_latest")
    if not latest or parse_version(latest) != parse_version(remote):
        raise RuntimeError("Supervisor store does not have exact verified release")
    LOG.info("Installing %s (backup enabled); app: %s", remote, slug)
    supervisor("POST", "/store/addons/" + quote(slug, safe="") + "/update",
               {"backup": True}, timeout=240)
    _, updated = discover()
    if parse_version(updated.get("version")) < parse_version(remote):
        raise RuntimeError("Supervisor update completed without expected version")
    LOG.info("HORUS Dashboard Manager is now v%s", updated["version"])
    return "updated", updated["version"]


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("HORUS update watcher running: 15-second checks; manager-only allowlist")
    observed = None
    last_ci_attempt = 0
    ci_ok = False
    retry_after = 0
    while True:
        now = time.monotonic()
        try:
            published = remote_version()
            if published != observed:
                LOG.info("GitHub release version: %s", published)
                observed, last_ci_attempt, ci_ok, retry_after = published, 0, False, 0
            if now >= retry_after:
                if not ci_ok and (not last_ci_attempt or now - last_ci_attempt >= 60):
                    last_ci_attempt = now
                    ci_ok = ci_passed()
                    if not ci_ok:
                        LOG.info("Waiting for successful GitHub safety-check workflow")
                if ci_ok:
                    result, installed = update_if_needed(published)
                    retry_after = now + (120 if result == "updated" else 300)
        except (ValueError, TypeError, RuntimeError, KeyError, HTTPError, URLError,
                TimeoutError, OSError) as exc:
            LOG.warning("Update check failed: %s (%s)", type(exc).__name__, str(exc)[:130])
            retry_after = now + COOLDOWN
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
