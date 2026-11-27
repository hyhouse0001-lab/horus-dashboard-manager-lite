import base64
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "horus_dashboard_manager"))
import manager
import asset_manager as a
import asset_bridge as bridge

class AssetApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = pathlib.Path(self.temp.name)
        (p / "www").mkdir()
        self.addCleanup(patch.stopall)
        for mod, key, value in [(a, "ROOT", p / "www/horus"), (a, "JOURNALS", p / "journals"),
                                (manager, "BACKUPS_DIR", str(p / "dashboard_backups")),
                                (manager, "STATE_PATH", str(p / "state.json"))]:
            patch.object(mod, key, value).start()
        self.before = {"views": [{"path": "home", "cards": []}]}
        self.after = {"views": [{"path": "home", "cards": [{"type": "custom:horus-demo"}]}]}
        self.live = self.before
        self.rows = []
        self.data = b"demo bytes"
        self.manifest = {"ref":"1"*40, "files":[{"path":"horus-demo.js","register":True,
                         "size":len(self.data),"sha256":a.digest(self.data)}]}
        self.proposal = {"schema_version":1, "dashboard":manager.PATH,"base_sha256":manager.digest(self.before),
                         "summary":"demo", "config":self.after, "assets":self.manifest}
        patch.object(manager,"load_proposal",side_effect=lambda:(self.proposal,"proposal")).start()
        patch.object(manager,"read_dashboard",side_effect=lambda:self.live).start()
        self.save = patch.object(manager,"save_dashboard",side_effect=self.save_dashboard).start()
        patch.object(manager,"github_put").start()
        patch.object(manager,"github_request",return_value={"type":"file","encoding":"base64",
                     "content":base64.b64encode(self.data).decode()}).start()
        patch.object(bridge,"resource",side_effect=self.resource).start()
    def save_dashboard(self, config, restoring=False):
        self.live = config
    def resource(self, action, **kw):
        if action == "list":
            return list(self.rows)
        if action == "create":
            self.rows.append({"id":"1","url":kw["url"],"type":kw["res_type"]})
        elif action == "delete":
            self.rows[:] = [r for r in self.rows if r["id"] != kw["resource_id"]]
        else:
            self.rows[0] = {"id":kw["resource_id"], "url":kw["url"],"type":kw["res_type"]}
    def preview(self):
        return bridge.prepare(self.proposal,self.live)[0]
    def test_assets_are_installed_before_dashboard_write_and_restored(self):
        def check(config, restoring=False):
            if not restoring:
                a.verify(self.manifest, bridge.resource)
            self.live = config
        self.save.side_effect = check
        manager.apply_proposal("proposal",self.preview())
        self.assertEqual(self.live,self.after)
        self.assertEqual(a.pending(),[])
        manager.rollback_last()
        self.assertEqual(self.live,self.before)
        self.assertIsNone(a.read("horus-demo.js"))
        self.assertEqual(self.rows,[])
    def test_asset_only_install_never_writes_dashboard(self):
        self.proposal["config"] = self.before
        manager.apply_proposal("proposal",self.preview())
        self.save.assert_not_called()
        a.verify(self.manifest,bridge.resource)
        with self.assertRaises(a.AssetError):
            self.preview()
    def test_unapproved_asset_plan_never_writes(self):
        with self.assertRaises(manager.BridgeError):
            manager.apply_proposal("proposal","not-previewed")
        self.save.assert_not_called()
        self.assertIsNone(a.read("horus-demo.js"))
    def test_failed_dashboard_save_restores_assets(self):
        def fail(config, restoring=False):
            if not restoring:
                raise RuntimeError("simulated failure")
            self.live = config
        self.save.side_effect=fail
        with self.assertRaises(RuntimeError):
            manager.apply_proposal("proposal",self.preview())
        self.assertEqual(self.live,self.before)
        self.assertEqual(a.pending(),[])
        self.assertIsNone(a.read("horus-demo.js"))
    def test_recovery_restores_interrupted_install(self):
        name = manager.backup(self.before)
        context = {"backup":name,"before_sha256":manager.digest(self.before),
                   "after_sha256":manager.digest(self.after),"proposal_sha":"proposal"}
        tx = a.install(self.manifest,self.preview(),manager.github_request,bridge.resource,context)
        self.live=self.after
        self.assertEqual(a.pending(),[tx])
        manager.recover_assets()
        self.assertEqual(self.live,self.before)
        self.assertEqual(a.pending(),[])
        self.assertIsNone(a.read("horus-demo.js"))
    def test_rollback_checks_asset_conflict_before_dashboard_write(self):
        manager.apply_proposal("proposal",self.preview())
        a.atomic(a.path("horus-demo.js"),b"external change")
        self.save.reset_mock()
        with self.assertRaises(a.AssetError):
            manager.rollback_last()
        self.save.assert_not_called()
        self.assertEqual(self.live,self.after)
