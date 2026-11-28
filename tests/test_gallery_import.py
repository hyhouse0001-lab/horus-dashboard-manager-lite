import io, zipfile, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"horus_dashboard_manager"))
import gallery_import as g
import asset_manager as assets

class GalleryImportTests(unittest.TestCase):
    def test_exact_archive_and_no_overwrite(self):
        name="living-gallery/art/test/full.webp"
        raw=b"fake test bytes"
        b=io.BytesIO()
        with zipfile.ZipFile(b,"w") as z: z.writestr(name,raw)
        with tempfile.TemporaryDirectory() as temp, patch.object(g,"ROOT",Path(temp)), patch.object(g,"EXPECTED",{name:assets.digest(raw)}):
            self.assertEqual(g.install(b.getvalue()),1)
            self.assertEqual(g.count(),1)
            (Path(temp)/name).write_bytes(b"different")
            with self.assertRaises(assets.AssetError): g.install(b.getvalue())
            self.assertEqual((Path(temp)/name).read_bytes(),b"different")
    def test_unapproved_archive_rejected(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,"w") as z: z.writestr("../bad",b"x")
        with self.assertRaises(assets.AssetError): g.validate(b.getvalue())
    def test_bad_csrf_rejected(self):
        with self.assertRaises(assets.AssetError): g.parse_form("text/plain",b"x","secret")
    def test_binary_upload_roundtrip(self):
        raw=b"PK\x03\x04\x00\xff\r\n\x90"
        form=(b'--horus-test\r\nContent-Disposition: form-data; name="csrf"\r\n\r\nsecret\r\n'
              b'--horus-test\r\nContent-Disposition: form-data; name="artwork"; filename="art.zip"\r\n'
              b'Content-Type: application/zip\r\n\r\n'+raw+b'\r\n--horus-test--\r\n')
        self.assertEqual(g.parse_form("multipart/form-data; boundary=horus-test",form,"secret"),raw)
