"""Autonomous, bounded museum open-access curator. No user credentials or scraping."""
import json, logging, re, time, unicodedata, hashlib, threading
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import asset_manager as assets
ROOT = Path("/homeassistant/www/horus/curated-gallery")
LOG = logging.getLogger("horus-gallery")
ARTISTS = [
  "Ernie Barnes",
  "Archibald Motley",
  "Romare Bearden",
  "Frank Morrison",
  "Kerry James Marshall",
  "Kadir Nelson",
  "Henry Taylor",
  "Annie Lee",
  "Paul Goodnight",
  "B. Robert Moore",
  "Heitor dos Prazeres",
  "Palmer Hayden",
  "Sargent Claude Johnson",
  "Loïs Mailou Jones",
  "John Wilson",
  "Hughie Lee-Smith",
  "Charles White",
  "Edward Mitchell Bannister",
  "Walter Henry Williams",
  "Norman Lewis",
  "William H. Johnson",
  "Jacob Lawrence",
  "Aaron Douglas",
  "Ellis Wilson",
  "Benny Andrews",
  "John Biggers",
  "Elizabeth Catlett",
  "Hale Woodruff",
  "Horace Pippin",
  "Eldzier Cortor",
  "Cándido Bidó",
  "Rafael Tufiño",
  "Myrna Báez",
  "Raelis Vasquez",
  "Samuel Lind",
  "Barkley L. Hendricks",
  "Lynette Yiadom-Boakye",
  "Jordan Casteel",
  "Amoako Boafo",
  "Titus Kaphar"
]
ALIASES = {"archibald motley": {"archibald john motley jr","archibald j motley jr","archibald motley jr","archibald john motley"}, "william h johnson":{"william henry johnson"},"lois mailou jones":{"lois jones"}}
API_HOSTS = {"api.artic.edu", "collectionapi.metmuseum.org", "openaccess-api.clevelandart.org"}
IMAGE_HOSTS = {"www.artic.edu","iiif.artic.edu","images.metmuseum.org","openaccess-cdn.clevelandart.org"}
MAX_IMAGE=4_000_000
MAX_WORKS=120
PER_ARTIST=4
INTERVAL=86400
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None
OPENER=build_opener(NoRedirect())
def normalize(s):
    return " ".join(re.findall(r"[a-z0-9]+",unicodedata.normalize("NFKD",str(s)).encode("ascii","ignore").decode().lower()))
def matches(name, artist):
    n=normalize(name)
    a=normalize(artist)
    return n==a or n in ALIASES.get(a,set())
def safe_url(url,hosts):
    p=urlsplit(url)
    return p.scheme=="https" and p.hostname in hosts and not p.username and not p.password and p.port in (None,443)
def get(url,hosts,limit):
    if not safe_url(url,hosts):
        raise ValueError("unapproved endpoint")
    req=Request(url,headers={"User-Agent":"HORUS-Living-Gallery/1.0 (private museum display)","Accept":"*/*"})
    with OPENER.open(req,timeout=12) as r:
        b=r.read(limit+1)
    if len(b)>limit: raise ValueError("response too large")
    return b
def obj(url):
    return json.loads(get(url,API_HOSTS,2_000_000))
def query(url,**params):
    return obj(url+"?"+urlencode(params))
def text(s,maxlen=400):
    return str(s or "").strip()[:maxlen]
def medium_ok(s):
    n=normalize(s)
    return any(w in n.split() for w in ("painting","paintings","print","prints","drawing","drawings","watercolor","collage","lithograph","woodcut","etching","screenprint","pastel"))
def record(source,id,artist,title,year,medium,credit,link,image):
    if not re.fullmatch(r"[0-9]{1,20}",str(id)) or not safe_url(image,IMAGE_HOSTS) or not title or not medium_ok(medium):
        return None
    return {"id":source+"-"+str(id),"artist":text(artist),"title":text(title),"year":text(year),"medium":text(medium),"credit":text(credit),"source":{"aic":"Art Institute of Chicago","cma":"Cleveland Museum of Art","met":"The Metropolitan Museum of Art"}[source],"source_url":link,"rights":"Public domain / CC0","remote":image}
def aic(artist):
    fields="id,title,artist_title,date_display,artwork_type_title,medium_display,credit_line,is_public_domain,image_id"
    for x in query("https://api.artic.edu/api/v1/artworks/search",q=artist,fields=fields,limit=24).get("data",[]):
        if x.get("is_public_domain") is not True or not matches(x.get("artist_title"),artist): continue
        img=x.get("image_id") or ""
        if not re.fullmatch(r"[a-fA-F0-9-]{36}",img): continue
        yield record("aic",x["id"],x["artist_title"],x.get("title"),x.get("date_display"),x.get("artwork_type_title"),x.get("credit_line"),"https://www.artic.edu/artworks/"+str(x["id"]),"https://www.artic.edu/iiif/2/"+img+"/full/843,/0/default.jpg")
def cma(artist):
    for x in query("https://openaccess-api.clevelandart.org/api/artworks",artists=artist,cc0=1,has_image=1,limit=24).get("data",[]):
        creators=x.get("creators") or []
        names=[str(y.get("description","")).split(" (")[0] for y in creators if y.get("role") in ("artist","Artist","painter","Painter","printmaker","Printmaker",None)]
        if x.get("share_license_status")!="CC0" or not any(matches(n,artist) for n in names): continue
        img=((x.get("images") or {}).get("web") or {}).get("url","")
        yield record("cma",x["id"],artist,x.get("title"),x.get("creation_date"),x.get("type"),x.get("creditline"),"https://www.clevelandart.org/art/"+str(x.get("accession_number","")),img)
def met(artist):
    result=query("https://collectionapi.metmuseum.org/public/collection/v1.1/search",q=artist,hasImages="true",artistOrCulture="true",limit=16)
    for id in (result.get("objectIDs") or [])[:12]:
        if not isinstance(id,int): continue
        x=obj("https://collectionapi.metmuseum.org/public/collection/v1/objects/"+str(id))
        if x.get("isPublicDomain") is not True or not matches(x.get("artistDisplayName"),artist): continue
        yield record("met",id,x["artistDisplayName"],x.get("title"),x.get("objectDate"),x.get("classification") or x.get("objectName"),x.get("creditLine"),"https://www.metmuseum.org/art/collection/search/"+str(id),x.get("primaryImageSmall") or x.get("primaryImage") or "")
def path(name):
    if not re.fullmatch(r"(?:[a-z]+-[0-9]+\.jpg|catalog\.json)",name): raise ValueError("bad local name")
    p=ROOT/name
    if not ROOT.parent.is_dir() or any(x.is_symlink() for x in (p,*p.parents)): raise ValueError("unsafe gallery path")
    return p
def existing():
    p=path("catalog.json")
    if not p.is_file() or p.stat().st_size>300000: return []
    try:
        rows=json.loads(p.read_bytes()).get("works",[])
        return [r for r in rows if re.fullmatch(r"[a-z]+-[0-9]+",r["id"]) and path(r["id"]+".jpg").is_file() and path(r["id"]+".jpg").stat().st_size<=MAX_IMAGE and hashlib.sha256(path(r["id"]+".jpg").read_bytes()).hexdigest()==r.get("sha256")]
    except (ValueError,KeyError,TypeError,OSError): return []
def jpeg(raw):
    return len(raw)>1024 and raw[:3]==b"\xff\xd8\xff" and raw.endswith(b"\xff\xd9")
def refresh():
    rows=existing()
    ids={r["id"] for r in rows}
    seen={(normalize(r["artist"]),normalize(r["title"])) for r in rows}
    counts={}
    for r in rows: counts[normalize(r["artist"])]=counts.get(normalize(r["artist"]),0)+1
    # Source preference: high relevance from the 40-person roster; no random unrelated artists.
    for artist in ARTISTS:
        if len(rows)>=MAX_WORKS: break
        key=normalize(artist)
        for source in (aic,cma,met):
            if counts.get(key,0)>=PER_ARTIST or len(rows)>=MAX_WORKS: break
            try:
                for r in source(artist):
                    if not r or r["id"] in ids or (normalize(r["artist"]),normalize(r["title"])) in seen: continue
                    raw=get(r.pop("remote"),IMAGE_HOSTS,MAX_IMAGE)
                    if not jpeg(raw): continue
                    r["artist"]=artist
                    r["image"]="/local/horus/curated-gallery/"+r["id"]+".jpg"
                    r["sha256"]=hashlib.sha256(raw).hexdigest()
                    assets.atomic(path(r["id"]+".jpg"),raw)
                    rows.append(r);ids.add(r["id"]);seen.add((key,normalize(r["title"])));counts[key]=counts.get(key,0)+1
                    publish(rows)
                    if counts[key]>=PER_ARTIST or len(rows)>=MAX_WORKS: break
            except Exception as e: LOG.info("Gallery source unavailable for %s: %s",artist,type(e).__name__)
    publish(rows)
    LOG.info("Autonomous gallery refresh completed: %d cached works",len(rows))
def publish(rows):
    assets.atomic(path("catalog.json"),json.dumps({"schema":1,"works":rows},ensure_ascii=False).encode())
def run():
    while True:
        try: refresh()
        except Exception as e: LOG.warning("Gallery refresh deferred: %s",type(e).__name__)
        time.sleep(INTERVAL)
def start():
    threading.Thread(target=run,name="horus-gallery-curator",daemon=True).start()
