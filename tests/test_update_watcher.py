import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "horus_update_watcher"))
import watcher

class UpdateWatcherTests(unittest.TestCase):
    def test_strict_version(self):
        self.assertEqual(watcher.parse_version("0.2.4"), (0,2,4))
        for value in ("v0.2.4", "0.2.4-beta", "../bad", "1.2", "0.3.4\n"):
            with self.assertRaises(ValueError):
                watcher.parse_version(value)

    def test_manifest_version(self):
        self.assertEqual(watcher.version_from_manifest("name: HORUS\nversion: 0.2.4\nslug: test\n"), "0.2.4")

    def test_ci_must_match_current_commit_and_version(self):
        import json
        sha = "a" * 40
        for status, conclusion, head, version, expected in (
            ("completed", "success", sha, "0.3.1", True),
            ("in_progress", None, sha, "0.3.1", False),
            ("completed", "failure", sha, "0.3.1", False),
            ("completed", "success", "b" * 40, "0.3.1", False),
            ("completed", "success", sha, "0.3.0", False),
        ):
            head_response = json.dumps({"sha":sha}).encode()
            run_response = json.dumps({"workflow_runs":[{"status":status,"conclusion":conclusion,
                "head_branch":"main","head_sha":head}]}).encode()
            with patch.object(watcher, "fetch", side_effect=[head_response, run_response,
                    ("version: " + version).encode()]):
                self.assertEqual(watcher.ci_passed("0.3.1"), expected)

    def test_supervisor_rejects_install_and_other_commands(self):
        for method,path,payload in (("POST","/store/addons/a_horus_dashboard_manager/update",{}),
                                     ("POST","/addons/a_horus_dashboard_manager/restart",{}),
                                     ("POST","/store/reload",{"extra":True}),
                                     ("GET","/addons/core_ssh/info",None)):
            with self.assertRaises(ValueError):
                watcher.supervisor(method,path,payload)

    def test_rejects_other_domains(self):
        with self.assertRaises(ValueError):
            watcher.fetch("https://example.com/attack")

    def test_finds_only_manager(self):
        rows = {"addons":[{"slug":"a0123456_horus_dashboard_manager","name":watcher.NAME},
                          {"slug":"core_ssh","name":"Terminal & SSH"}]}
        info = {"slug":"a0123456_horus_dashboard_manager","name":watcher.NAME,
                "url":watcher.REPO_URL,"version":"0.2.3"}
        def fake(m,p,*args,**kwargs):
            if p == "/addons":return rows
            if p == "/addons/a0123456_horus_dashboard_manager/info":return info
            raise AssertionError(p)
        with patch.object(watcher,"supervisor",side_effect=fake):
            self.assertEqual(watcher.discover()[0],"a0123456_horus_dashboard_manager")

    def test_refresh_only_never_installs(self):
        info = {"name":watcher.NAME,"slug":"a0123456_horus_dashboard_manager",
                "url":watcher.REPO_URL,"version":"0.3.0","version_latest":"0.3.0"}
        refreshed = {**info,"version_latest":"0.3.1"}
        with patch.object(watcher,"discover",side_effect=[(info["slug"],info),(info["slug"],refreshed)]), \
             patch.object(watcher,"supervisor") as sup:
            self.assertEqual(watcher.update_if_needed("0.3.1"),("available","0.3.1"))
            sup.assert_called_once_with("POST","/store/reload",{})

    def test_already_available_does_not_reload(self):
        info = {"version":"0.3.0","version_latest":"0.3.1"}
        with patch.object(watcher,"discover",return_value=("a_horus_dashboard_manager",info)), \
             patch.object(watcher,"supervisor") as sup:
            self.assertEqual(watcher.update_if_needed("0.3.1"),("available","0.3.1"))
            sup.assert_not_called()

    def test_stale_store_not_claimed_available(self):
        info = {"version":"0.3.0","version_latest":"0.3.0"}
        with patch.object(watcher,"discover",return_value=("a_horus_dashboard_manager",info)), \
             patch.object(watcher,"supervisor"):
            with self.assertRaises(RuntimeError):
                watcher.update_if_needed("0.3.1")

if __name__ == "__main__":
    unittest.main()
