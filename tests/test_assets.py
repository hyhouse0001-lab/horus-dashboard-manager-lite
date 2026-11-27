import base64
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "horus_dashboard_manager"))
import asset_manager as a
import asset_bridge as bridge

class AssetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        (root / "www").mkdir()
        self.addCleanup(patch.stopall)
        patch.object(a, "ROOT", root / "www/horus").start()
        patch.object(a, "JOURNALS", root / "journal").start()
        self.rows = [{"id": "other", "url": "/hacsfiles/other.js", "type": "module"}]
        self.raw = b"customElements.define('horus-demo', class extends HTMLElement {});"
        self.name = "horus-demo.js"
        self.m = {"ref": "a" * 40, "files": [{"path": self.name, "size": len(self.raw),
                 "sha256": a.digest(self.raw), "register": True}]}
        self.calls = []
    def resource(self, action, **kw):
        self.calls.append((action, kw))
        if action == "list":
            return [dict(r) for r in self.rows]
        if action == "create":
            self.rows.append({"id": "new", "url": kw["url"], "type": kw["res_type"]})
        if action == "update":
            r = next(r for r in self.rows if r["id"] == kw["resource_id"])
            r.update(url=kw["url"], type=kw["res_type"])
        if action == "delete":
            self.rows[:] = [r for r in self.rows if r["id"] != kw["resource_id"]]
    def fetch(self, path, *, ref):
        self.assertEqual(ref, self.m["ref"])
        self.assertEqual(path, "home_assistant/www/horus/" + self.name)
        return {"type": "file", "encoding": "base64", "content": base64.b64encode(self.raw).decode()}
    def install(self):
        return a.install(self.m, a.plan_hash(self.m, self.resource), self.fetch, self.resource)
    def test_install_register_verify_and_rollback(self):
        tx = self.install()
        a.verify(self.m, self.resource)
        self.assertEqual(a.read(self.name), self.raw)
        self.assertEqual(a.pending(), [tx])
        a.finish(tx)
        self.assertEqual(a.pending(), [])
        a.restore(tx, self.resource)
        self.assertIsNone(a.read(self.name))
        self.assertEqual(len(self.rows), 1)
    def test_replace_preserves_previous_bytes_and_registration(self):
        a.atomic(a.path(self.name), b"previous")
        old = {"id": "old", "url": "/local/horus/" + self.name + "?v=old", "type": "module"}
        self.rows.append(dict(old))
        tx = self.install()
        a.finish(tx)
        a.restore(tx, self.resource)
        self.assertEqual(a.read(self.name), b"previous")
        self.assertEqual(self.rows[-1], old)
    def test_hash_mismatch_never_writes(self):
        with self.assertRaises(a.AssetError):
            a.install(self.m, a.plan_hash(self.m, self.resource),
                      lambda *args, **kw: {"type":"file","encoding":"base64","content":"YQ=="}, self.resource)
        self.assertIsNone(a.read(self.name))
        self.assertEqual(len(self.rows), 1)
    def test_stale_preview_rejected(self):
        before = a.plan_hash(self.m, self.resource)
        a.atomic(a.path(self.name), b"edit")
        with self.assertRaises(a.AssetError):
            a.install(self.m, before, self.fetch, self.resource)
        self.assertEqual(a.read(self.name), b"edit")
    def test_rollback_refuses_external_edit(self):
        tx = self.install()
        a.finish(tx)
        a.atomic(a.path(self.name), b"external")
        with self.assertRaises(a.AssetError):
            a.restore(tx, self.resource)
        self.assertEqual(a.read(self.name), b"external")
    def test_manifest_rejects_path_escape_and_non_asset(self):
        for bad in ("../config.yaml", "/tmp/a.js", "x/../../a.js", "x.py", "a%2fb.js", "x\\\\a.js", ".storage/a.js"):
            with self.subTest(bad=bad), self.assertRaises(a.AssetError):
                a.relative(bad)
    def test_symlink_rejected(self):
        a.ROOT.mkdir()
        other = pathlib.Path(self.tmp.name) / "elsewhere"
        other.write_bytes(b"do not overwrite")
        (a.ROOT / self.name).symlink_to(other)
        with self.assertRaises(a.AssetError):
            a.read(self.name)
    def test_duplicate_resource_rejected(self):
        r = {"id": "a", "url": "/local/horus/" + self.name, "type": "module"}
        self.rows += [r, dict(r, id="b")]
        with self.assertRaises(a.AssetError):
            a.plan(self.m, self.resource)
    def test_failed_registry_mutation_restores_files(self):
        def fail(operation, **kw):
            if operation == "create":
                raise a.AssetError("simulated failure")
            return self.resource(operation, **kw)
        with self.assertRaises(a.AssetError):
            a.install(self.m, a.plan_hash(self.m, fail), self.fetch, fail)
        self.assertIsNone(a.read(self.name))
        self.assertEqual(a.pending(), [])
    def test_changed_custom_card_requires_manifest(self):
        live = {"views":[{"path":"home","cards":[]}]}
        proposal = {"config":{"views":[{"path":"home","cards":[{"type":"custom:horus-demo"}]}]}}
        with patch.object(bridge, "resource", self.resource), self.assertRaises(a.AssetError):
            bridge.prepare(proposal, live)
        proposal["assets"] = self.m
        with patch.object(bridge, "resource", self.resource):
            digest, display = bridge.prepare(proposal, live)
        self.assertEqual(len(digest), 64)
        self.assertIn(self.name, display)
    def test_unchanged_legacy_custom_card_does_not_block_recovery(self):
        live = {"views":[{"path":"old","cards":[{"type":"custom:horus-demo"}]},{"path":"home","cards":[]}]}
        proposal = {"config":{"views":[live["views"][0],{"path":"home","cards":[{"type":"markdown"}]}]}}
        self.assertEqual(bridge.prepare(proposal, live), ("", ""))
