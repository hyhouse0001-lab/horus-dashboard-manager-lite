"""Private owner upload: exact five approved artwork bytes, no generic extraction."""
import io
import zipfile
from pathlib import Path
from email.parser import BytesParser
from email.policy import default
import hmac
import asset_manager as assets

ROOT = Path("/homeassistant/www")
EXPECTED = {"living-gallery/art/bearden-up-at-mintons/full.webp":"02d777896c14e50a5bc39f269a65a06871c065d3cb739c056070172e42c9ece1","living-gallery/art/bearden-quilting-time/full.webp":"85fd2b3e45e75611929f94108bac5753d19e4a281a53305a2ac7f66cc5dde086","living-gallery/art/motley-tongues/full.webp":"0554a68b16f2a707bcca29cdf5f800580df83c8b2e901b09f1f528c168562f69","living-gallery/art/motley-blues/full.webp":"12682071c0add2d7186ead0e25e83c060be518c521d70ee7118f37bf5a968093","living-gallery/art/motley-jazz-singers/full.webp":"5f21a7bc02a42995540d12b828a2166c4bf68db92f15b61a2de814b3107fcc03"}
MAX_BYTES = 3_000_000

def parse_form(content_type, body, csrf):
    if not content_type.startswith("multipart/form-data;") or "\r" in content_type or "\n" in content_type or len(content_type)>512:
        raise assets.AssetError("Invalid artwork upload")
    msg = BytesParser(policy=default).parsebytes(("Content-Type: "+content_type+"\r\nMIME-Version: 1.0\r\n\r\n").encode()+body)
    if not msg.is_multipart():
        raise assets.AssetError("Invalid multipart form")
    fields = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name not in ("csrf", "artwork") or name in fields or part.is_multipart():
            raise assets.AssetError("Invalid upload fields")
        fields[name] = part.get_payload(decode=True)
    if not hmac.compare_digest(fields.get("csrf", b""), csrf.encode()):
        raise assets.AssetError("Approval form expired")
    return fields.get("artwork", b"")

def validate(raw):
    if not raw or len(raw)>MAX_BYTES:
        raise assets.AssetError("Invalid artwork archive size")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries=z.infolist()
            if len(entries)!=len(EXPECTED) or set(z.namelist())!=set(EXPECTED):
                raise assets.AssetError("Upload the exact Collection 001 five-image ZIP")
            result={}
            for item in entries:
                if item.file_size>950000 or item.flag_bits & 1:
                    raise assets.AssetError("Invalid artwork entry")
                data=z.read(item)
                if assets.digest(data)!=EXPECTED[item.filename]:
                    raise assets.AssetError("Artwork checksum differs from approved collection")
                result[item.filename]=data
            return result
    except (zipfile.BadZipFile,RuntimeError,NotImplementedError):
        raise assets.AssetError("Invalid artwork ZIP") from None

def target(name):
    p=ROOT/name
    if not ROOT.is_dir() or any(x.is_symlink() for x in (p,*p.parents)):
        raise assets.AssetError("Artwork destination unavailable or symlinked")
    return p

def install(raw):
    entries=validate(raw)
    pending=[]
    for name,data in entries.items():
        p=target(name)
        if p.exists():
            if not p.is_file() or p.stat().st_nlink!=1 or p.stat().st_size>950000 or assets.digest(p.read_bytes())!=EXPECTED[name]:
                raise assets.AssetError("Different existing artwork preserved; import stopped")
        else:
            pending.append((p,data))
    for p,data in pending:
        assets.atomic(p,data)
    if count()!=len(EXPECTED):
        raise assets.AssetError("Artwork verification failed")
    return len(EXPECTED)

def count():
    total=0
    for name,sha in EXPECTED.items():
        p=target(name)
        if p.is_file() and p.stat().st_size<=950000 and assets.digest(p.read_bytes())==sha:
            total+=1
    return total
