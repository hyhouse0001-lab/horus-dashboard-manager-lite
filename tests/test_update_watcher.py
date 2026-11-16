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

    def test_ci_must_be_successful(self):
        for status,conclusion,expected in (("completed","success",True),
                                           ("in_progress",None,False),
                                           ("completed","failure",False)):
            item = {"workflow_runs":[{"status":status,"conclusion":conclusion,"head_branch":"main"}]}
            with patch.object(watcher, "fetch", return_value=__import__("json").dumps(item).encode()):
                self.assertEqual(watcher.ci_passed(), expected)

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

    def test_update_only_one_slug(self):
        info = {"name":watcher.NAME,"slug":"a0123456_horus_dashboard_manager",
                "url":watcher.REPO_URL,"version":"0.2.3","version_latest":"0.2.4"}
        newer = {**info,"version":"0.2.4"}
        count=[0]
        def fake_discover():
            count[0]+=1
            return info["slug"], newer if count[0]>2 else info
        paths=[]
        def fake_sup(m,p,*args,**kwargs):
            paths.append((m,p))
        with patch.object(watcher,"discover",side_effect=fake_discover), \
             patch.object(watcher,"supervisor",side_effect=fake_sup):
            self.assertEqual(watcher.update_if_needed("0.2.4"),("updated","0.2.4"))
        self.assertEqual(paths,[("POST","/store/reload"),
                                ("POST","/store/addons/a0123456_horus_dashboard_manager/update")])

if __name__ == "__main__":
    unittest.main()
