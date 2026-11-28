import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "horus_dashboard_manager"))
import nanit_install

class NanitStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "custom_components").mkdir()
        self.cfg = self.root / "configuration.yaml"
        self.original = b"default_config:\n"
        self.cfg.write_bytes(self.original)
        self.backup = self.root / "backup"
        self.files = {"manifest.json": b'{"domain":"horus_nanit_insights"}',
                      "model.py": b"x = 1\n", "__init__.py": b"x = 2\n",
                      "configuration.fragment.yaml": b"horus_nanit_insights:\n  baby_name: Test\n"}

    def stage(self):
        with patch.object(nanit_install, "download", return_value=dict(self.files)):
            return nanit_install.stage(None, self.root, self.backup)

    def test_stage_preserves_original_and_backs_up(self):
        self.stage()
        self.assertEqual(self.backup.read_bytes(), self.original)
        self.assertTrue(self.cfg.read_bytes().startswith(self.original))
        self.assertTrue((self.root / "custom_components/horus_nanit_insights/model.py").is_file())
        with self.assertRaises(ValueError):
            self.stage()

    def test_wrong_fragment_never_writes(self):
        self.files["configuration.fragment.yaml"] = b"unsafe: true\n"
        with self.assertRaises(ValueError):
            self.stage()
        self.assertEqual(self.cfg.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())

    def test_invalid_python_never_writes(self):
        self.files["model.py"] = b"bad syntax !!!"
        with self.assertRaises(SyntaxError):
            self.stage()
        self.assertFalse(self.backup.exists())

    def test_duplicate_configuration_rejected(self):
        self.cfg.write_bytes(self.original + self.files["configuration.fragment.yaml"])
        with self.assertRaises(ValueError):
            self.stage()

    def test_missing_or_wrong_pin_fails(self):
        with self.assertRaises(ValueError):
            nanit_install.download(lambda *a, **kw: None)

if __name__ == "__main__":
    unittest.main()
