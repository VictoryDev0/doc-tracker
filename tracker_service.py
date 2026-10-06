"""
doc-tracker: a self-hosted, DocSend-style viewer-analytics service for documents and links.
Single file, Python standard library only (sqlite3 + http.server).

Create trackable links for four kinds of content from the dashboard:
  - url   : wrap any external link (cloud doc, website)            -> click tracking + 302 redirect
  - html  : upload an HTML file                                    -> scroll / section / dwell-time tracking
  - pdf   : upload a PDF                                           -> PDF.js viewer with per-page dwell time
  - img   : upload an image (png/jpg/webp/gif)                     -> viewer page with view/dwell tracking

Each document can have many per-recipient links (token = access + identity). Typically run
behind a reverse proxy (Traefik, nginx, Caddy) that terminates TLS.

Routes:
  GET  /r/<token>            view: serve instrumented html / PDF viewer / redirect (url)
  GET  /r/<token>/file       raw stored file (pdf/image bytes) for the viewer
  GET  /r/<token>/comments   comment pins for the document (JSON)
  POST /r/<token>/comments   add comment / reply / resolve toggle
  GET  /beacon.js            shared client tracker (self-labeling sections/pages)
  GET  /cmt.js               comment overlay injected into HTML documents
  POST /t                    beacon sink (open/beat/end), CORS-enabled for external pages
  GET  /dash?key             management dashboard + link generator + analytics (heatmaps)     [key]
  POST /admin/doc?key        create url-doc {name,target,folder_id}                             [key]
  POST /admin/upload?key     upload html/pdf/img (raw body; X-Doc-Name/X-Filename/X-Doc-Type)   [key]
  POST /admin/update?key     new version of a document (url JSON or raw file body)              [key]
  POST /admin/link?key       {doc_id,name,email,pw} -> shareable link                           [key]
  POST /admin/dellink?key    {token} delete a recipient link and its sessions                   [key]
  GET  /admin/add?key&...    convenience: mint link (name,email,pw,doc)                         [key]
  GET  /api/monitor/activity?key   aggregate activity summary (for external monitors)          [key]
  GET  /api/monitor/links?key      per-link stats (for spreadsheet sync scripts etc.)          [key]
  GET  /api/sessions?key&token=..  per-visit detail rows (HTML fragment, paged)                [key]
  POST /admin/archivedoc?key       {doc_id} archive: links -> 404 "Page not available"         [key]
  POST /admin/restoredoc?key       {doc_id} un-archive                                         [key]
  POST /admin/purgedoc?key         {doc_id,confirm} permanent delete (only archived docs)      [key]
  POST /admin/folder?key           {action:create|rename|delete|assign, ...} one-level folders [key]
  POST /admin/feature?key          feature-request box on the dashboard                        [key]
  GET  /form?k=<slug>        questionnaire page (registered in TRACKER_FORMS)
  POST /api/form             questionnaire autosave / submit
  GET  /form/answers?key     latest questionnaire submissions                                  [key]
  GET  /health

CLI: python tracker_service.py serve | add "Name" email doc_id | list | docs
"""
import json, os, re, sqlite3, sys, time, urllib.request, urllib.parse, secrets, smtplib, html, mimetypes, hashlib, hmac
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone, timedelta, tzinfo

def _load_env(path):
    """Minimal .env loader (no python-dotenv dependency). Existing env vars win."""
    try:
        for ln in open(path):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass
HERE     = os.path.dirname(os.path.abspath(__file__))
_load_env(os.environ.get("TRACKER_ENV_FILE") or os.path.join(HERE, ".env"))

def _env1(name, default=""):
    """First whitespace-separated token of an env var (strips inline comments from .env lines)."""
    return ((os.environ.get(name, default) or default).split() or [default])[0]

DB       = os.environ.get("TRACKER_DB", os.path.join(HERE, "data", "tracker.db"))
DOCDIR   = os.environ.get("TRACKER_DOCS", os.path.join(os.path.dirname(DB) or ".", "docs"))
KEY      = os.environ.get("TRACKER_KEY", "")
PORT     = int(os.environ.get("TRACKER_PORT", "8092"))
BIND     = os.environ.get("TRACKER_BIND", "127.0.0.1")  # 0.0.0.0 inside a container
BASE     = os.environ.get("TRACKER_BASE_URL", "http://localhost:%d" % PORT).rstrip("/")
BRAND    = os.environ.get("TRACKER_BRAND", "Doc Tracker")

# Questionnaires: slug -> (html file, label used in notifications/answer view).
# TRACKER_FORMS="slug:file.html:Label;other-slug:other.html:Other label". Relative files resolve
# against TRACKER_FORMS_DIR (default ./examples). The slug doubles as a light access key, so
# use something unguessable for real forms.
FORMS_DIR = os.environ.get("TRACKER_FORMS_DIR", os.path.join(HERE, "examples"))
def _parse_forms(spec):
    out = {}
    for part in (spec or "").split(";"):
        bits = [b.strip() for b in part.split(":", 2)]
        if len(bits) >= 2 and bits[0] and bits[1]:
            out[bits[0]] = (bits[1], bits[2] if len(bits) > 2 and bits[2] else bits[0])
    return out
FORMS = _parse_forms(os.environ.get("TRACKER_FORMS", "demo:sample_form.html:Sample questionnaire"))

# Notifications. Every channel is optional and silent on failure.
ALERT_TO = os.environ.get("TRACKER_ALERT_EMAIL", "")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY     = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")          # optional bearer token for protected ntfy servers
IPINFO   = os.environ.get("IPINFO_TOKEN", "")
SMTP     = {k: os.environ.get("SMTP_" + k, "") for k in ("HOST","PORT","USER","PASS","FROM")}
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")
# Which document opens trigger a push: "all" = every real open, "named" = only opens of named
# recipient links (suppresses anonymous / direct hits), "off" = opens stay dashboard-only.
NOTIFY_OPENS = _env1("TRACKER_NOTIFY_OPENS", "all").lower()

class _CentralEurope(tzinfo):
    """Europe/Berlin incl. DST, stdlib-only (no tzdata dependency in slim images).
    EU rule: last Sunday of March 01:00 UTC until last Sunday of October 01:00 UTC = UTC+2, else UTC+1."""
    @staticmethod
    def _last_sunday(year, month):
        d = datetime(year, month, 31)                    # naive on purpose; converting would recurse
        return d - timedelta(days=(d.weekday() + 1) % 7)
    def _is_dst(self, dt):
        # Boundaries expressed in standard-time fields (UTC+1); tzinfo.fromutc() calls dst() with those.
        if dt is None: return False
        n = dt.replace(tzinfo=None)
        return self._last_sunday(n.year, 3).replace(hour=2) <= n < self._last_sunday(n.year, 10).replace(hour=2)
    def utcoffset(self, dt): return timedelta(hours=2 if self._is_dst(dt) else 1)
    def dst(self, dt): return timedelta(hours=1 if self._is_dst(dt) else 0)
    def tzname(self, dt): return "CEST" if self._is_dst(dt) else "CET"
CET      = _CentralEurope()
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
os.makedirs(DOCDIR, exist_ok=True)

# ---------------------------------------------------------------- storage ----
def db():
    c = sqlite3.connect(DB, timeout=10); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=5000"); return c

def init():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS documents(
            id TEXT PRIMARY KEY, name TEXT, type TEXT, source TEXT, filename TEXT,
            labels TEXT DEFAULT '{}', created TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS links(
            token TEXT PRIMARY KEY, doc_id TEXT, name TEXT, email TEXT, pw TEXT DEFAULT '', created TEXT)""")
        if "pw" not in {r[1] for r in c.execute("PRAGMA table_info(links)")}:
            c.execute("ALTER TABLE links ADD COLUMN pw TEXT DEFAULT ''")
        c.execute("""CREATE TABLE IF NOT EXISTS sessions(
            sid TEXT PRIMARY KEY, token TEXT, doc_id TEXT, vid TEXT, first_ts TEXT, last_ts TEXT,
            ip TEXT, ua TEXT, device TEXT, os TEXT, browser TEXT, city TEXT, country TEXT, org TEXT,
            tz TEXT, lang TEXT, screen TEXT, vp TEXT, ref TEXT, max_scroll INTEGER DEFAULT 0,
            furthest TEXT, active_ms INTEGER DEFAULT 0, total_ms INTEGER DEFAULT 0,
            dwell TEXT DEFAULT '{}', beats INTEGER DEFAULT 0, kind TEXT DEFAULT 'open')""")
        c.execute("""CREATE TABLE IF NOT EXISTS geo_cache(ip TEXT PRIMARY KEY, data TEXT, ts TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS form_answers(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ip TEXT,
            done INTEGER DEFAULT 0, progress TEXT, payload TEXT)""")
        if "form" not in {r[1] for r in c.execute("PRAGMA table_info(form_answers)")}:
            c.execute("ALTER TABLE form_answers ADD COLUMN form TEXT DEFAULT ''")
        c.execute("""CREATE TABLE IF NOT EXISTS versions(
            doc_id TEXT, version INTEGER, type TEXT, source TEXT, filename TEXT,
            labels TEXT DEFAULT '{}', note TEXT DEFAULT '', created TEXT, PRIMARY KEY(doc_id,version))""")
        scols = {r[1] for r in c.execute("PRAGMA table_info(sessions)")}
        if "doc_id" not in scols: c.execute("ALTER TABLE sessions ADD COLUMN doc_id TEXT")
        if "kind" not in scols: c.execute("ALTER TABLE sessions ADD COLUMN kind TEXT DEFAULT 'open'")
        if "ver" not in scols: c.execute("ALTER TABLE sessions ADD COLUMN ver INTEGER")
        dcols = {r[1] for r in c.execute("PRAGMA table_info(documents)")}
        if "version" not in dcols: c.execute("ALTER TABLE documents ADD COLUMN version INTEGER DEFAULT 1")
        if "updated" not in dcols: c.execute("ALTER TABLE documents ADD COLUMN updated TEXT")
        if "archived" not in dcols: c.execute("ALTER TABLE documents ADD COLUMN archived TEXT")
        if "folder_id" not in dcols: c.execute("ALTER TABLE documents ADD COLUMN folder_id TEXT DEFAULT ''")
        c.execute("""CREATE TABLE IF NOT EXISTS folders(
            id TEXT PRIMARY KEY, name TEXT UNIQUE, pos INTEGER DEFAULT 0, created TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS comments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, token TEXT, author TEXT,
            page TEXT, x REAL, y REAL, text TEXT, parent INTEGER DEFAULT 0,
            resolved INTEGER DEFAULT 0, ts TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS features(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, author TEXT DEFAULT '',
            text TEXT, status TEXT DEFAULT 'open')""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_doc ON comments(doc_id, id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token, last_ts DESC)")
        # backfill versioning: one v1 row per document + stamp existing sessions as v1
        c.execute("UPDATE documents SET version=1 WHERE version IS NULL")
        for d in c.execute("SELECT * FROM documents").fetchall():
            v = d["version"] or 1
            if not c.execute("SELECT 1 FROM versions WHERE doc_id=? AND version=?", (d["id"], v)).fetchone():
                c.execute("INSERT OR IGNORE INTO versions(doc_id,version,type,source,filename,labels,note,created) VALUES(?,?,?,?,?,?,?,?)",
                          (d["id"], v, d["type"], d["source"], d["filename"], d["labels"] or "{}", "", d["created"]))
        c.execute("UPDATE sessions SET ver=1 WHERE ver IS NULL")

# ---------------------------------------------------------------- enrich -----
def parse_ua(ua):
    ua = ua or ""
    o = ("iOS" if re.search(r"iPhone|iPad|iPod", ua) else "Android" if "Android" in ua else "Windows" if "Windows" in ua
         else "macOS" if "Mac OS X" in ua or "Macintosh" in ua else "Linux" if "Linux" in ua else "?")
    d = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android-Phone" if ("Android" in ua and "Mobile" in ua)
         else "Android-Tablet" if "Android" in ua else "Mobile" if "Mobile" in ua else "Desktop")
    b = ("Edge" if "Edg" in ua else "Chrome" if ("Chrome" in ua or "CriOS" in ua) else "Firefox" if "Firefox" in ua
         else "Safari" if "Safari" in ua else "?")
    return d, o, b

_PRIV = re.compile(r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1|fc|fd|169\.254\.)")
def first_ip(x): return (x or "").split(",")[0].strip()

def geo(ip):
    ip = first_ip(ip)
    if not ip or _PRIV.match(ip): return {}
    with db() as c:
        row = c.execute("SELECT data FROM geo_cache WHERE ip=?", (ip,)).fetchone()
        if row:
            try: return json.loads(row["data"])
            except Exception: return {}
    out = {}
    try:
        u = ("https://ipinfo.io/%s?token=%s" % (ip, IPINFO)) if IPINFO else ("https://ipwho.is/%s" % ip)
        with urllib.request.urlopen(u, timeout=6) as r: d = json.loads(r.read().decode())
        if IPINFO:
            out = {"city": d.get("city",""), "country": d.get("country",""), "org": d.get("org",""), "tz": d.get("timezone","")}
        else:
            out = {"city": d.get("city",""), "country": d.get("country_code",""),
                   "org": (d.get("connection") or {}).get("isp",""), "tz": d.get("timezone","")}
    except Exception:
        out = {}
    with db() as c:
        c.execute("INSERT OR REPLACE INTO geo_cache(ip,data,ts) VALUES(?,?,?)", (ip, json.dumps(out), datetime.now(timezone.utc).isoformat()))
    return out

def cet(ts):
    try: return datetime.fromisoformat(ts).astimezone(CET).strftime("%b %d, %H:%M")
    except Exception: return ts or "-"
def rel(ts):
    try: d = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except Exception: return ""
    return "just now" if d < 90 else "%d min ago" % (d//60) if d < 3600 else "%d h ago" % (d//3600) if d < 86400 else "%d d ago" % (d//86400)
def fmt_ms(ms):
    s = int(ms)//1000; return ("%dm %02ds" % (s//60, s % 60)) if s >= 60 else ("%ds" % s)

# ---------------------------------------------------------------- alerts -----
def ntfy(title, body, url):
    """Push via ntfy (public ntfy.sh or a self-hosted server). Silent on failure."""
    if not NTFY: return
    hdr = {"Title": title.encode("ascii","ignore").decode().strip(), "Click": url, "Tags": "eyes", "Priority": "high"}
    if NTFY_TOKEN: hdr["Authorization"] = "Bearer " + NTFY_TOKEN
    try:
        urllib.request.urlopen(urllib.request.Request(NTFY_URL + "/" + NTFY, data=body.encode("utf-8"), headers=hdr), timeout=6)
    except Exception: pass
def telegram(title, body, url=None, btn="Open dashboard"):
    """Push to a Telegram chat. Silent on failure: an alert must never block the beacon
    response (the reader's browser is waiting for it)."""
    if not (TG_TOKEN and TG_CHAT): return
    e = lambda s: html.escape(str(s or ""))
    text = "<b>%s</b>\n%s" % (e(title), e(body))
    payload = {"chat_id": TG_CHAT, "text": text[:4000], "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if url:
        payload["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": btn, "url": url}]]})
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN,
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=6)
    except Exception: pass

def email(subject, body):
    if not (SMTP["HOST"] and ALERT_TO): return
    try:
        m = EmailMessage(); m["Subject"] = subject; m["From"] = SMTP["FROM"] or SMTP["USER"]; m["To"] = ALERT_TO; m.set_content(body)
        p = int(SMTP["PORT"] or "587")
        s = smtplib.SMTP_SSL(SMTP["HOST"], p, timeout=12) if p == 465 else smtplib.SMTP(SMTP["HOST"], p, timeout=12)
        if p != 465: s.starttls()
        if SMTP["USER"]: s.login(SMTP["USER"], SMTP["PASS"])
        s.send_message(m); s.quit()
    except Exception: pass

# ---------------------------------------------------------------- lookups ----
def link_row(token):
    with db() as c: return c.execute("SELECT * FROM links WHERE token=?", (token,)).fetchone()
def doc_row(doc_id):
    with db() as c: return c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
def name_for(token):
    r = link_row(token)
    return r["name"] if r else ("Direct" if not token else "Unknown(" + token[:6] + ")")

# ---------------------------------------------------------------- beacon -----
def handle_beacon(p, ip, ua):
    sid = str(p.get("s") or "")[:40]
    if not sid: return
    token = str(p.get("t") or "")[:64]
    lr = link_row(token); doc_id = lr["doc_id"] if lr else ""
    if token and not lr: return          # link deleted/purged: drop late beacons from open tabs
    if doc_id:
        _ad = doc_row(doc_id)
        if _ad and _ad["archived"]: return   # archived doc: tracking stopped
    kind = p.get("e"); dev, osx, br = parse_ua(ua)
    dwell = {k: int(v) for k, v in (p.get("dwell") or {}).items() if isinstance(v, (int, float))}
    labels = p.get("labels") or {}
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        srow = c.execute("SELECT ver FROM sessions WHERE sid=?", (sid,)).fetchone()
    existing = srow is not None
    if existing:
        ver = srow["ver"] or 1
    else:
        with db() as c:
            dv = c.execute("SELECT version FROM documents WHERE id=?", (doc_id,)).fetchone()
        ver = (dv["version"] if dv and dv["version"] else 1)
    g0 = geo(ip) if (not existing and kind == "open") else {}   # geo() writes its own cache, keep OUT of the txn below
    new = False; opens = 0
    with db() as c:
        if labels and doc_id:
            row2 = c.execute("SELECT labels FROM versions WHERE doc_id=? AND version=?", (doc_id, ver)).fetchone()
            cur = {}
            try: cur = json.loads(row2["labels"]) if row2 else {}
            except Exception: cur = {}
            cur.update({str(k)[:40]: str(v)[:80] for k, v in labels.items()})
            c.execute("UPDATE versions SET labels=? WHERE doc_id=? AND version=?", (json.dumps(cur), doc_id, ver))
            c.execute("UPDATE documents SET labels=? WHERE id=? AND version=?", (json.dumps(cur), doc_id, ver))
        if not existing:
            c.execute("""INSERT OR IGNORE INTO sessions(sid,token,doc_id,ver,vid,first_ts,last_ts,ip,ua,device,os,browser,city,country,org,
                tz,lang,screen,vp,ref,max_scroll,furthest,active_ms,total_ms,dwell,beats,kind)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, token, doc_id, ver, str(p.get("v") or "")[:40], now, now, first_ip(ip), (ua or "")[:400], dev, osx, br,
                 g0.get("city",""), g0.get("country",""), g0.get("org",""), str(p.get("tz") or "")[:60], str(p.get("lang") or "")[:20],
                 str(p.get("scr") or "")[:32], str(p.get("vp") or "")[:20], str(p.get("ref") or "")[:200],
                 int(p.get("maxScroll") or 0), str(p.get("furthest") or "")[:40], int(p.get("activeMs") or 0),
                 int(p.get("totalMs") or 0), json.dumps(dwell), 1, str(kind or "open")))
            new = True
            # Without a token, count per document; otherwise every anonymous visit lands in one bucket.
            opens = c.execute("SELECT COUNT(*) n FROM sessions WHERE token=? AND (?<>'' OR doc_id=?)",
                              (token, token, doc_id)).fetchone()["n"]
        else:
            c.execute("""UPDATE sessions SET last_ts=?, max_scroll=MAX(max_scroll,?), furthest=?, active_ms=MAX(active_ms,?),
                total_ms=MAX(total_ms,?), dwell=?, beats=beats+1 WHERE sid=?""",
                (now, int(p.get("maxScroll") or 0), str(p.get("furthest") or "")[:40], int(p.get("activeMs") or 0),
                 int(p.get("totalMs") or 0), json.dumps(dwell), sid))
    if kind == "open" and new:
        # one ping per visit-burst: skip if this visitor (vid) already opened this
        # link in the last 10 min (multi-page sites fire one "open" per page load)
        vidv = str(p.get("v") or "")
        if vidv:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            with db() as c:
                dup = c.execute("SELECT 1 FROM sessions WHERE token=? AND vid=? AND sid<>? AND first_ts>? LIMIT 1",
                                (token, vidv, sid, cutoff)).fetchone()
            if dup: return
        # Link scanners (mail / security gateways) pre-open every link: no alert for those.
        # Only hard signals count here: at the first beacon there is no reading behaviour yet,
        # so a real reader looks just as "empty" as a scanner at this moment.
        if BOT_UA.search(ua or "") or DC_ORG.search(g0.get("org", "") or ""):
            return
        nm = name_for(token); d = doc_row(doc_id)
        loc = ", ".join(x for x in [g0.get("city",""), g0.get("country","")] if x) or first_ip(ip)
        again = (" (visit #%d)" % opens) if opens > 1 else ""
        # Without a tracker document (external page loading /beacon.js), name the sending site.
        pg = str(p.get("_pg") or "")
        dname = d["name"] if d else (("page " + pg) if pg else (doc_id or "unknown page"))
        title = "📖 %s opened \"%s\"%s" % (nm, dname, again)
        body = "%s · %s\n%s\n%s" % (dev, br, loc, cet(now))
        if NOTIFY_OPENS == "off" or (NOTIFY_OPENS == "named" and not (token and lr)):
            return
        dash = BASE + "/dash?key=" + KEY + "&q=" + urllib.parse.quote((d["name"] if d else "")[:60])
        ntfy(title, body, dash)
        telegram(title, body, dash)
        email(title, nm + "\n" + body + "\n\nDashboard: " + BASE + "/dash?key=" + KEY)

# ---------------------------------------------------------------- visit class --
# Link scanners (mail/chat clients, security gateways) pre-open every link and inflate
# visit counts. Classification happens at read time (no DB column), so rule changes
# apply retroactively.
BOT_UA = re.compile(r"(headlesschrome|phantomjs|puppeteer|playwright|selenium|curl/|wget/|python-requests|"
                    r"python-urllib|go-http-client|java/|okhttp|libwww|httpclient|axios/|node-fetch|"
                    r"bot\b|crawler|spider|slurp|scanner|monitor|preview|fetcher|validator|"
                    r"slackbot|discordbot|telegrambot|whatsapp|facebookexternalhit|twitterbot|linkedinbot|"
                    r"bingpreview|googlebot|applebot|proofpoint|mimecast|barracuda|symantec|forcepoint|"
                    r"microsoft office|ms-office|outlook|skypeuripreview)", re.I)
# Pure server/cloud networks. VPN providers (M247, Datacamp, Mullvad ...) are deliberately NOT
# listed: real readers sit behind those.
DC_ORG = re.compile(r"(amazon|aws|google llc|google cloud|microsoft|azure|digitalocean|hetzner|ovh|linode|"
                    r"vultr|scaleway|oracle cloud|alibaba|tencent|cloudflare|fastly|akamai|"
                    r"proofpoint|mimecast|barracuda|zscaler|forcepoint|netskope)", re.I)

def visit_class(s):
    """'real' = read · 'thin' = opened without interaction · 'bot' = scanner/tool."""
    ua = s["ua"] or ""; org = s["org"] or ""
    act = s["active_ms"] or 0; scr = s["max_scroll"] or 0; bts = s["beats"] or 0
    read = act >= 2000 or scr > 0
    alive = read or bts > 2
    if scr > 0 and act >= 5000: return "real"   # scanners do not fake scrolling plus dwell time
    if BOT_UA.search(ua): return "bot"
    if DC_ORG.search(org) and not alive: return "bot"
    return "real" if read else "thin"

VISIT_TAG = {"bot": "<span class='vtag bot'>Bot</span>",
             "thin": "<span class='vtag thin'>no interaction</span>", "real": ""}

# ---------------------------------------------------------------- summaries --
def read_pct(furthest, ordered):
    if furthest in ordered: return round((ordered.index(furthest) + 1) / len(ordered) * 100)
    return 0
def doc_order(labels):  # section keys in a stable order
    keys = list(labels.keys())
    def kf(k):
        m = re.match(r"p(\d+)$", k)
        return (0, int(m.group(1))) if m else (1, k)
    return sorted(keys, key=kf)

def activity_summary(doc_id=None):
    with db() as c:
        if doc_id:
            sess = c.execute("SELECT * FROM sessions WHERE doc_id=? ORDER BY last_ts DESC", (doc_id,)).fetchall()
            links = c.execute("SELECT COUNT(*) n FROM links WHERE doc_id=?", (doc_id,)).fetchone()["n"]
            docs = 1
        else:
            sess = c.execute("""SELECT s.* FROM sessions s LEFT JOIN documents d ON d.id=s.doc_id
                                WHERE d.archived IS NULL ORDER BY s.last_ts DESC""").fetchall()
            links = c.execute("""SELECT COUNT(*) n FROM links l LEFT JOIN documents d ON d.id=l.doc_id
                                 WHERE d.archived IS NULL""").fetchone()["n"]
            docs = c.execute("SELECT COUNT(*) n FROM documents WHERE archived IS NULL").fetchone()["n"]
    opened = {s["token"] for s in sess if s["token"]}
    last = sess[0] if sess else None
    lv = {}
    if last:
        d = doc_row(last["doc_id"]); labels = {}
        try: labels = json.loads(d["labels"]) if d else {}
        except Exception: labels = {}
        lv = {"rel": rel(last["last_ts"]), "cet": cet(last["first_ts"]), "ts": last["last_ts"], "ua": last["ua"], "kind": "open",
              "device": (last["device"] or "?") + " · " + (last["browser"] or "?"),
              "geo": ", ".join(x for x in [last["city"], last["country"]] if x) or last["ip"], "ip": last["ip"],
              "who": name_for(last["token"]), "doc": d["name"] if d else last["doc_id"],
              "read": read_pct(last["furthest"], doc_order(labels)), "scroll": last["max_scroll"]}
    with db() as c:
        if doc_id:
            crow = c.execute("SELECT COUNT(*) n, MAX(ts) ts FROM comments WHERE doc_id=?", (doc_id,)).fetchone()
            copen = c.execute("SELECT COUNT(*) n FROM comments WHERE doc_id=? AND resolved=0 AND parent=0", (doc_id,)).fetchone()["n"]
        else:
            crow = c.execute("SELECT COUNT(*) n, MAX(ts) ts FROM comments").fetchone()
            copen = c.execute("SELECT COUNT(*) n FROM comments WHERE resolved=0 AND parent=0").fetchone()["n"]
    # "Meaningful" events for alerting: every open (session) + crossed scroll quarters + comments.
    # Heartbeats are excluded on purpose: an idle open tab would otherwise produce endless
    # "new activity". Monotonically increasing, so usable as a delta signal.
    meaningful = len(sess) + sum((s["max_scroll"] or 0) // 25 for s in sess) + crow["n"]
    return {"ok": True, "total_events": sum(s["beats"] for s in sess), "meaningful_events": meaningful,
            "sessions": len(sess),
            "documents": docs, "links_total": links, "unique_recipients_opened": len(opened),
            "last_activity_ts": last["last_ts"] if last else None, "last_visitor": lv,
            "comments_total": crow["n"], "comments_open": copen, "last_comment_ts": crow["ts"],
            "completion": {"label": "opened", "answered": len(opened), "total": links,
                           "pct": round(len(opened)/links*100) if links else 0}}

def comments_for(doc_id):
    """All comment pins of a document (across links, so every reviewer sees the same
    bubbles). Replies hang off their pin via parent."""
    with db() as c:
        rows = c.execute("SELECT * FROM comments WHERE doc_id=? ORDER BY id", (doc_id,)).fetchall()
    return [{"id": r["id"], "author": r["author"], "page": r["page"], "x": r["x"], "y": r["y"],
             "text": r["text"], "parent": r["parent"], "resolved": r["resolved"],
             "ts": r["ts"], "rel": rel(r["ts"])} for r in rows]

def add_comment(doc_id, token, p, ip):
    """New pin / reply / resolve toggle. p = JSON body of the POST."""
    author = (str(p.get("author") or "").strip() or name_for(token))[:60]
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        if p.get("resolve") is not None:
            cid = int(p.get("id") or 0)
            c.execute("UPDATE comments SET resolved=? WHERE id=? AND doc_id=?",
                      (1 if p.get("resolve") else 0, cid, doc_id))
            return {"ok": True, "id": cid}
        text = str(p.get("text") or "").strip()[:4000]
        if not text: return {"ok": False, "error": "text required"}
        parent = int(p.get("parent") or 0)
        page = str(p.get("page") or "")[:40]
        x = max(0.0, min(100.0, float(p.get("x") or 0)))
        y = max(0.0, min(100.0, float(p.get("y") or 0)))
        cur = c.execute("""INSERT INTO comments(doc_id,token,author,page,x,y,text,parent,resolved,ts)
                           VALUES(?,?,?,?,?,?,?,?,0,?)""",
                        (doc_id, token, author, page, x, y, text, parent, now))
        cid = cur.lastrowid
    d = doc_row(doc_id)
    ntfy("💬 Comment from %s" % author,
         "%s: %s\n\"%s\"" % (d["name"] if d else doc_id, page or "page ?", text[:180]),
         BASE + "/dash?key=" + KEY)
    telegram("💬 Comment from %s" % author,
             "%s: %s\n\"%s\"" % (d["name"] if d else doc_id, page or "page ?", text[:180]),
             BASE + "/dash?key=" + KEY + "&q=" + urllib.parse.quote((d["name"] if d else "")[:60]))
    return {"ok": True, "id": cid, "author": author, "ts": now}

def features_list():
    """All feature requests, newest first."""
    with db() as c:
        rows = c.execute("SELECT * FROM features ORDER BY id DESC").fetchall()
    return [{"id": r["id"], "ts": r["ts"], "author": r["author"], "text": r["text"],
             "status": r["status"], "cet": cet(r["ts"]), "rel": rel(r["ts"])} for r in rows]

def add_feature(text, author=""):
    """Store a new feature request and notify."""
    text = str(text or "").strip()[:2000]
    if not text: return None
    author = str(author or "").strip()[:60]
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        cur = c.execute("INSERT INTO features(ts,author,text,status) VALUES(?,?,?, 'open')",
                        (now, author, text))
        fid = cur.lastrowid
    who = (author + ": ") if author else ""
    ntfy("💡 New feature request", who + text[:200], BASE + "/dash?key=" + KEY)
    telegram("💡 New feature request", who + text[:400], BASE + "/dash?key=" + KEY)
    email("💡 Feature request (%s)" % BRAND, who + text)
    return {"id": fid, "ts": now, "author": author, "text": text, "status": "open"}

def set_feature(fid, status=None, delete=False):
    with db() as c:
        if delete:
            c.execute("DELETE FROM features WHERE id=?", (fid,))
        elif status in ("open", "done", "planned"):
            c.execute("UPDATE features SET status=? WHERE id=?", (status, fid))
    return True

def links_summary(doc_id=None):
    """Per-recipient-link stats (for spreadsheet sync scripts and similar consumers)."""
    with db() as c:
        if doc_id:
            links = c.execute("SELECT * FROM links WHERE doc_id=? ORDER BY created", (doc_id,)).fetchall()
        else:
            links = c.execute("""SELECT l.* FROM links l LEFT JOIN documents d ON d.id=l.doc_id
                                 WHERE d.archived IS NULL ORDER BY l.created""").fetchall()
        sess = c.execute("SELECT * FROM sessions").fetchall()
    by_tok = {}
    for s in sess: by_tok.setdefault(s["token"], []).append(s)
    order_cache = {}
    out = []
    for l in links:
        did = l["doc_id"]
        if did not in order_cache:
            d = doc_row(did)
            try: labels = json.loads(d["labels"]) if d else {}
            except Exception: labels = {}
            order_cache[did] = doc_order(labels)
        order = order_cache[did]
        ss = by_tok.get(l["token"], [])
        out.append({"token": l["token"], "doc_id": did, "name": l["name"], "email": l["email"] or "",
                    "opens": len(ss), "last_ts": max((s["last_ts"] for s in ss), default=None),
                    "max_scroll": max((int(s["max_scroll"] or 0) for s in ss), default=0),
                    "read_pct": (max((read_pct(s["furthest"], order) for s in ss), default=0) if order else 0),
                    "active_ms": sum(int(s["active_ms"] or 0) for s in ss)})
    res = {"ok": True, "links": out}
    if doc_id:
        d = doc_row(doc_id)
        if d and d["archived"]: res["archived"] = True
    return res

# ---------------------------------------------------------------- dashboard --
def heat_rows(dwell, labels, order):
    mx = max(dwell.values()) if dwell else 1
    out = []
    for k in order:
        secs = dwell.get(k, 0) // 1000
        frac = (dwell.get(k, 0) / mx) if mx else 0
        # blue heat scale: light -> brand blue
        col = "hsl(216 %d%% %d%%)" % (85, 90 - int(44*frac))
        out.append("<div class=srow><span class=slbl>%s</span><span class=sbar><i style='width:%d%%;background:%s'></i></span><span class=ssec>%s</span></div>"
                    % (html.escape(labels.get(k, k)), max(3, int(frac*100)), col, fmt_ms(dwell.get(k, 0))))
    return "".join(out) or "<div class=muted>no section data</div>"

SESS_INLINE = 10   # visit rows rendered inline on the dashboard (the rest load via /api/sessions)

def sess_row(s, order, labels=None):
    """One visit (= one sessions row) as a detail grid line, plus its own per-visit heatmap.

    Dwell time per section is stored per session in `dwell`, so the expandable heatmap
    below shows exactly this one visit (the heatmaps above are sums)."""
    esc = lambda x: html.escape(str(x or ""))
    loc = ", ".join(x for x in [s["city"], s["country"]] if x) or first_ip(s["ip"] or "")
    scr = int(s["max_scroll"] or 0)
    rd = read_pct(s["furthest"], order) if order else 0
    ext = " · external" if (s["kind"] or "") == "click" else ""
    vc = visit_class(s)
    # A visit is a continuously updated session: first_ts = start, last_ts = last sign of life.
    # Show the last activity first (matches "X ago"), the start only when it differs.
    span = (" · since %s" % cet(s["first_ts"])) if cet(s["first_ts"]) != cet(s["last_ts"]) else ""
    try: dwell = {k: int(v) for k, v in json.loads(s["dwell"] or "{}").items()}
    except Exception: dwell = {}
    dwell = {k: v for k, v in dwell.items() if v > 0}
    # sid comes unvalidated from the client: hashed for the DOM id, never raw in markup/JS
    hid = hashlib.md5(str(s["sid"] or "").encode("utf-8")).hexdigest()[:12]
    chev, heat = "", ""
    if dwell:
        chev = "<span class=auchev>▸</span>"
        heat = ("<div class=ausec id='sec-%s' hidden><div class=hcap>This visit only · time per section</div>%s</div>"
                % (hid, heat_rows(dwell, labels or {}, order or doc_order(dwell))))
    return ("<div class='aur%s%s'%s><span class=aud>%s%s<span class=aurel>%s%s%s</span></span><span class=aum>%s · %s%s</span>"
            "<span class=aum>%s</span><span class=aup>%d%% scrolled</span><span class=aup>%d%% read</span>"
            "<span class=aup>%s active</span><span class=aubar><i style='width:%d%%'></i></span></div>%s"
            % (" hassec" if dwell else "", "" if vc == "real" else " dim",
               (" onclick=\"toggleSec('%s',this)\"" % hid) if dwell else "",
               chev, esc(cet(s["last_ts"])), esc(rel(s["last_ts"])), esc(span), VISIT_TAG[vc],
               esc(s["device"]), esc(s["browser"]), ext,
               esc(loc), scr, rd, fmt_ms(s["active_ms"] or 0), max(2, rd), heat))

def sess_fragment(token, ver=0, offset=0, limit=50):
    """HTML fragment of visit rows for one link token (for the 'Load more' button)."""
    with db() as c:
        if ver:
            ss = c.execute("SELECT * FROM sessions WHERE token=? AND COALESCE(ver,1)=? ORDER BY last_ts DESC LIMIT ? OFFSET ?",
                           (token, ver, limit, offset)).fetchall()
        else:
            ss = c.execute("SELECT * FROM sessions WHERE token=? ORDER BY last_ts DESC LIMIT ? OFFSET ?",
                           (token, limit, offset)).fetchall()
    labels = {}
    lr = link_row(token)
    if lr:
        with db() as c:
            row = c.execute("SELECT labels FROM versions WHERE doc_id=? AND version=?", (lr["doc_id"], ver)).fetchone() if ver else None
        try: labels = json.loads(row["labels"]) if row else {}
        except Exception: labels = {}
        if not labels:
            d = doc_row(lr["doc_id"])
            try: labels = json.loads(d["labels"]) if d else {}
            except Exception: labels = {}
    order = doc_order(labels)
    return "".join(sess_row(s, order, labels) for s in ss)

FOLDER_DOTS = ("#2563eb", "#f59e0b", "#0ea5e9", "#16a34a", "#8b5cf6", "#ec4899", "#64748b")

def dashboard(q=None):
    q = q or {}
    esc = lambda x: html.escape(str(x or ""))
    with db() as c:
        docs = c.execute("SELECT * FROM documents ORDER BY created DESC").fetchall()
        links = c.execute("SELECT * FROM links").fetchall()
        sess = c.execute("SELECT * FROM sessions ORDER BY last_ts DESC").fetchall()
        vers = c.execute("SELECT doc_id,version,labels,created FROM versions").fetchall()
        folders = c.execute("SELECT * FROM folders ORDER BY name COLLATE NOCASE").fetchall()
    fids = {f["id"] for f in folders}
    active = [d for d in docs if not d["archived"]]
    arch = [d for d in docs if d["archived"]]
    sel = (q.get("f", ["all"])[0] or "all")
    if sel not in ("all", "none", "every", "archiv") and sel not in fids: sel = "all"
    sort = (q.get("s", ["akt"])[0] or "akt")
    if sort not in ("akt", "neu"): sort = "akt"
    query = (q.get("q", [""])[0] or "").strip()[:120]
    by_doc_links = {}; [by_doc_links.setdefault(l["doc_id"], []).append(l) for l in links]
    by_tok = {}; [by_tok.setdefault(s["token"], []).append(s) for s in sess]
    vlabels = {}; vmeta = {}
    for v in vers:
        try: vlabels[(v["doc_id"], v["version"])] = json.loads(v["labels"] or "{}")
        except Exception: vlabels[(v["doc_id"], v["version"])] = {}
        vmeta.setdefault(v["doc_id"], {})[v["version"]] = v["created"]

    def fold_of(d):
        fid = d["folder_id"] or ""
        return fid if fid in fids else ""

    # last reader activity per document (sess is last_ts DESC, so the first hit wins)
    last_act = {}
    for s in sess: last_act.setdefault(s["doc_id"], s["last_ts"])
    def by_activity(ds):
        if sort == "neu": return ds                      # DB order = created DESC
        return sorted(ds, key=lambda d: last_act.get(d["id"]) or "", reverse=True)

    # ---- sidebar (folder navigation)
    def sitem(fkey, label, count, cls="", dot=""):
        on = " on" if sel == fkey else ""
        return ("<a class='fitem%s%s' href='/dash?key=%s&f=%s&s=%s'>%s<span class=ftxt>%s</span><span class=n>%d</span></a>"
                % (cls, on, esc(KEY), fkey, sort, dot, esc(label), count))
    # "All documents" = inbox: only documents without a folder. Filed documents show up
    # under their folder (or under "Everything").
    unfiled = [d for d in active if not fold_of(d)]
    side = ["<div class=slbl>Folders</div>", sitem("all", "All documents", len(unfiled))]
    for i, f in enumerate(folders):
        n = sum(1 for d in active if fold_of(d) == f["id"])
        side.append(sitem(f["id"], f["name"], n, dot="<span class=dot style='background:%s'></span>" % FOLDER_DOTS[i % len(FOLDER_DOTS)]))
    side.append(sitem("every", "Everything", len(active), cls=" everyl", dot="<span class=dot style='background:#cbd5e1'></span>"))
    side.append("<button class=newf onclick='newFolder()'>+ New folder</button>")
    side.append(sitem("archiv", "🗄 Archive", len(arch), cls=" archl"))

    def folder_opts(cur):
        o = ["<option value=''%s>No folder</option>" % (" selected" if not cur else "")]
        for f in folders:
            o.append("<option value='%s'%s>%s</option>" % (esc(f["id"]), " selected" if cur == f["id"] else "", esc(f["name"])))
        return "".join(o)

    # ---- search: spans ALL documents (or the archive), not just the open folder.
    fname_of = {f["id"]: f["name"] for f in folders}
    def matches(d):
        hay = [d["name"], d["source"], d["filename"], fname_of.get(d["folder_id"] or "", "")]
        hay += [x for l in by_doc_links.get(d["id"], []) for x in (l["name"], l["email"])]
        blob = " ".join((h or "") for h in hay).lower()
        return all(t in blob for t in query.lower().split())   # all terms (AND)

    # ---- selection + main head
    if query: shown = by_activity([d for d in (arch if sel == "archiv" else active) if matches(d)])
    elif sel == "archiv": shown = []
    elif sel == "every": shown = by_activity(active)
    elif sel in ("all", "none"): shown = by_activity(unfiled)
    else: shown = by_activity([d for d in active if fold_of(d) == sel])
    shown_ids = {d["id"] for d in shown}
    n_links = sum(len(by_doc_links.get(d["id"], [])) for d in shown)
    shown_sess = [s for s in sess if s["doc_id"] in shown_ids]
    n_sess = sum(1 for s in shown_sess if visit_class(s) == "real")
    n_filtered = len(shown_sess) - n_sess
    qs = ("&q=" + urllib.parse.quote(query)) if query else ""
    def sbtn(k, label):
        return ("<a class='hxb%s' href='/dash?key=%s&f=%s&s=%s%s'>%s</a>"
                % (" on" if sort == k else "", esc(KEY), sel, k, qs, label))
    headx = ""
    if query:
        title = "Search: \u201c%s\u201d" % query
        scope = "the archive" if sel == "archiv" else "all documents"
        sub = ("%s in %s · %d links · %s%s" % (
            ("No results" if not shown else "1 result" if len(shown) == 1 else "%d results" % len(shown)),
            scope, n_links, ("1 real visit" if n_sess == 1 else "%d real visits" % n_sess),
            (" (%d bot/no-interaction visits filtered out)" % n_filtered) if n_filtered else ""))
        if not shown:
            sub += ". Searched in document name, target URL, file name, folder and recipients."
        headx = ("<span class=headx>" + sbtn("akt", "↕ Last opened") + sbtn("neu", "Newest first")
                 + "<a class=hxb href='/dash?key=%s&f=%s&s=%s'>✕ Clear search</a></span>"
                 % (esc(KEY), sel, sort))
    elif sel == "archiv":
        title = "Archive"
        sub = "%d documents. Links disabled (visitors see 404 \"Page not available\"), statistics frozen." % len(arch)
    else:
        if sel in ("all", "none"): title = "All documents"
        elif sel == "every": title = "Every document"
        else:
            f = next(x for x in folders if x["id"] == sel)
            title = f["name"]
            headx = ("<span class=headx><button class=hxb data-fid=\"%s\" data-name=\"%s\" onclick=\"renFolder(this)\">✎ Rename</button>"
                     "<button class=\"hxb red\" data-fid=\"%s\" data-name=\"%s\" onclick=\"delFolder(this)\">Dissolve folder</button></span>"
                     % (esc(f["id"]), esc(f["name"]), esc(f["id"]), esc(f["name"])))
        sub = "%d documents · %d links · %s%s" % (
            len(shown), n_links, ("1 real visit" if n_sess == 1 else "%d real visits" % n_sess),
            (" (%d bot/no-interaction visits filtered out)" % n_filtered) if n_filtered else "")
        if sel in ("all", "none"): sub += ". Unfiled only (filed documents are listed in their folder)"
        headx += ("<span class=headx>" + sbtn("akt", "↕ Last opened") + sbtn("neu", "Newest first") + "</span>")

    cards = []
    for d in shown:
        did = d["id"]; curver = d["version"] or 1
        try: selver = int(q.get("v_" + did, [str(curver)])[0])
        except Exception: selver = curver
        allvers = sorted(vmeta.get(did, {curver: d["created"]}).keys())
        if selver not in allvers: selver = curver
        labels = vlabels.get((did, selver), {})
        order = doc_order(labels)
        dls = by_doc_links.get(did, [])
        vsess_all = [s for s in sess if s["doc_id"] == did]
        counts = {}
        for s in vsess_all: counts[s["ver"] or 1] = counts.get(s["ver"] or 1, 0) + 1
        dsess = [s for s in vsess_all if (s["ver"] or 1) == selver]
        # "opened" counts only recipients with real reading behaviour; scanners stay out
        opened = {s["token"] for s in dsess if s["token"] and visit_class(s) == "real"}
        agg = {}
        for s in dsess:
            for k, v in json.loads(s["dwell"] or "{}").items(): agg[k] = agg.get(k, 0) + int(v)
        agg_heat = ("<div class=heatbox><div class=hcap>Document heatmap · version %d · time per section (all readers)</div>%s</div>"
                    % (selver, heat_rows(agg, labels, order or doc_order(agg)))) if agg else ""
        rows = []
        entries = [(l["token"], l["name"], l["email"], l["pw"]) for l in dls]
        for tok, nm, em, pw in entries:
            lk = " 🔒" if pw else ""
            ss = [s for s in by_tok.get(tok, []) if (s["ver"] or 1) == selver]
            link = BASE + "/r/" + tok
            if not ss:
                rows.append("<tr class=no><td><b>%s</b>%s</td><td colspan=5 class=muted>not opened in v%d</td>"
                            "<td class=lk><input value='%s' readonly onclick='this.select()'><button onclick=\"cp('%s',this)\">Copy</button><a class=lkopen href='%s' target=_blank rel=noopener title=\"Open link (counts as a visit)\">↗</a><button class=del onclick=\"delLink('%s',this)\" title=\"Delete recipient\">✕</button></td></tr>"
                            % (esc(nm), lk, selver, esc(link), esc(link), esc(link), esc(tok)))
                continue
            klass = {x["sid"]: visit_class(x) for x in ss}
            n_real = sum(1 for x in ss if klass[x["sid"]] == "real")
            n_bot = sum(1 for x in ss if klass[x["sid"]] == "bot")
            n_thin = len(ss) - n_real - n_bot
            filt = ""
            if n_bot or n_thin:
                parts = ([("%d bot%s" % (n_bot, "" if n_bot == 1 else "s"))] if n_bot else []) + ([("%d without interaction" % n_thin)] if n_thin else [])
                filt = "<div class=filtnote>+ %s</div>" % " · ".join(parts)
            last = ss[0]; act = max(x["active_ms"] for x in ss); scr = max(x["max_scroll"] for x in ss)
            # "first" = very first contact across ALL visits, not the start of the latest one
            first_seen = min(x["first_ts"] for x in ss if x["first_ts"])
            rd = max(read_pct(x["furthest"], order) for x in ss) if order else 0
            per = {}
            for x in ss:
                for k, v in json.loads(x["dwell"] or "{}").items(): per[k] = per.get(k, 0) + int(v)
            loc = ", ".join(x for x in [last["city"], last["country"]] if x) or last["ip"]
            aurows = "".join(sess_row(x, order, labels) for x in ss[:SESS_INLINE])
            more = ""
            if len(ss) > SESS_INLINE:
                more = ("<button class=aumore onclick=\"moreVisits('%s',%d,%d,this)\">Load %d more visits</button>"
                        % (esc(tok), selver, SESS_INLINE, len(ss) - SESS_INLINE))
            rows.append("<tr><td><b>%s</b>%s<div class=mini><div class=hcap>%s</div>%s</div></td><td>%s<br><span class=muted>%s</span></td>"
                        "<td>%s · %s<br><span class=muted>%s</span></td><td class=n><button class=aub onclick=\"toggleAu('%s')\" title=\"Show each visit\">%d× ▾</button>%s</td><td class=n>%d%% scrolled<br>%d%% read</td>"
                        "<td class=n>%s</td><td class=lk><input value='%s' readonly onclick='this.select()'><button onclick=\"cp('%s',this)\">Copy</button><a class=lkopen href='%s' target=_blank rel=noopener title=\"Open link (counts as a visit)\">↗</a><button class=del onclick=\"delLink('%s',this)\" title=\"Delete recipient\">✕</button></td></tr>"
                        % (esc(nm) + lk, (" <span class=muted>&lt;%s&gt;</span>" % esc(em) if em else ""),
                           ("this one visit only" if n_real == 1 else "all %d visits combined" % n_real),
                           heat_rows(per, labels, order or doc_order(per)),
                           esc(cet(last["last_ts"])), esc("%s · first %s" % (rel(last["last_ts"]), cet(first_seen))),
                           esc(last["device"]), esc(last["browser"]),
                           esc(loc), esc(tok), n_real, filt, scr, rd, fmt_ms(act), esc(link), esc(link), esc(link), esc(tok)))
            rows.append("<tr class=aux id='au-%s' hidden><td colspan=7><div class=auh>Every visit · version %d</div><div class=aulist>%s%s</div></td></tr>"
                        % (esc(tok), selver, aurows, more))
        vertabs = ""
        if len(allvers) > 1:
            tabs = []
            for vn in sorted(allvers, reverse=True):
                cls = "vtab on" if vn == selver else "vtab"
                mark = " · current" if vn == curver else ""
                tabs.append("<a class='%s' href='/dash?key=%s&f=%s&s=%s&v_%s=%d#doc-%s'>v%d · %d×%s</a>"
                            % (cls, esc(KEY), sel, sort, did, vn, did, vn, counts.get(vn, 0), mark))
            vertabs = "".join(tabs)
        upd = d["updated"]
        la = last_act.get(did)
        verline = "Version %d%s%s · %s" % (selver, ("" if selver == curver else " (current: v%d)" % curver),
                                            (" · last updated %s" % cet(upd) if upd else ""),
                                            ("last opened %s (%s)" % (rel(la), cet(la)) if la else "never opened"))
        if d["type"] == "url":
            update = ("<div class=upd><input id='uu-%s' placeholder='New target URL (https://…)' style='min-width:300px'>"
                      "<button onclick=\"updateDoc('%s','url')\">↻ Update URL → new version</button><span class=out id='ou-%s'></span></div>" % (did, did, did))
        else:
            acc = {"pdf": "application/pdf,.pdf", "img": "image/*,.png,.jpg,.jpeg,.webp,.gif"}.get(d["type"], ".html,.htm")
            update = ("<div class=upd><input type=file id='uf-%s' accept='%s'>"
                      "<button onclick=\"updateDoc('%s','%s')\">↻ Upload new version</button><span class=out id='ou-%s'></span></div>" % (did, acc, did, d["type"], did))
        ttag = {"url": "🔗 Link", "html": "📄 HTML", "pdf": "📕 PDF", "img": "🖼 Image"}.get(d["type"], d["type"])
        src = ("<a class=srca href=\"%s\" target=_blank rel=noopener>→ %s</a>" % (esc(d["source"]), esc(d["source"]))) if d["type"] == "url" else esc(d["filename"] or "")
        fsel = "<select class=fsel title=\"Assign folder\" onchange=\"setFolder('%s',this.value)\">%s</select>" % (esc(did), folder_opts(fold_of(d)))
        cards.append(DOC_CARD.replace("{{NAME}}", esc(d["name"])).replace("{{TYPE}}", ttag).replace("{{SRC}}", src)
                     .replace("{{DID}}", esc(did)).replace("{{OPENED}}", str(len(opened))).replace("{{LINKS}}", str(len(dls)))
                     .replace("{{FOLDERSEL}}", fsel)
                     .replace("{{AGG}}", agg_heat).replace("{{VERTABS}}", vertabs).replace("{{VERLINE}}", esc(verline)).replace("{{UPDATE}}", update)
                     .replace("{{ROWS}}", "".join(rows) or "<tr><td colspan=7 class=muted>no links yet, create one below</td></tr>"))
    if sel == "archiv":
        for d in arch:
            fname = next((f["name"] for f in folders if f["id"] == (d["folder_id"] or "")), "none")
            nl = len(by_doc_links.get(d["id"], []))
            ns = sum(1 for s in sess if s["doc_id"] == d["id"])
            ttag = {"url": "🔗 Link", "html": "📄 HTML", "pdf": "📕 PDF", "img": "🖼 Image"}.get(d["type"], d["type"])
            cards.append(ARCHIV_CARD.replace("{{NAME}}", esc(d["name"])).replace("{{TYPE}}", ttag)
                         .replace("{{WHEN}}", esc(cet(d["archived"]))).replace("{{FOLDER}}", esc(fname))
                         .replace("{{LINKS}}", str(nl)).replace("{{SESS}}", str(ns)).replace("{{DID}}", esc(d["id"])))
        if not cards:
            cards.append("<div class=card><span class=muted>The archive is empty.</span></div>")
    elif not cards:
        cards.append("<div class=card><span class=muted>No documents in this view. Create a new document above or pick another folder on the left.</span></div>")
    # ---- feedback box: feature requests
    feats = features_list()
    STAT = {"open": ("Open", "#f59e0b", "#fef6e6"), "planned": ("Planned", "#2563eb", "#eef4ff"),
            "done": ("Done", "#16a34a", "#eaf7ee")}
    n_open = sum(1 for f in feats if f["status"] == "open")
    frows = []
    for f in feats:
        lbl, col, bg = STAT.get(f["status"], STAT["open"])
        nxt = {"open": "planned", "planned": "done", "done": "open"}[f["status"]]
        auth = ("<span class=fqauth>%s</span> · " % esc(f["author"])) if f["author"] else ""
        frows.append(
            "<div class='fqitem %s'><div class=fqmain><div class=fqtext>%s</div>"
            "<div class=fqmeta>%s%s · %s</div></div>"
            "<div class=fqactions>"
            "<button class=fqstat style='color:%s;background:%s' onclick=\"featStatus(%d,'%s')\" title=\"Change status\">%s</button>"
            "<button class=fqdel onclick=\"featDel(%d)\" title=\"Delete\">✕</button>"
            "</div></div>"
            % (f["status"], esc(f["text"]), auth, esc(f["rel"] or ""), esc(f["cet"]),
               col, bg, f["id"], nxt, lbl, f["id"]))
    fq_list = "".join(frows) or "<div class=fqempty>No requests yet. Add the first one above.</div>"
    feedback = (FEEDBACK_BOX.replace("{{COUNT}}", str(n_open))
                .replace("{{LIST}}", fq_list))
    return (DASH.replace("{{SIDE}}", "".join(side)).replace("{{TITLE}}", esc(title)).replace("{{SUB}}", esc(sub))
            .replace("{{HEADX}}", headx).replace("{{GOPTS}}", folder_opts(sel if sel in fids else ""))
            .replace("{{GENSTYLE}}", "display:none" if sel == "archiv" else "")
            .replace("{{FEEDBACK}}", feedback)
            .replace("{{SEL}}", esc(sel)).replace("{{SORT}}", esc(sort)).replace("{{QVAL}}", esc(query))
            .replace("{{QCLR}}", ("<a class=srchx href='/dash?key=%s&f=%s&s=%s' title='Clear search'>✕</a>"
                                  % (esc(KEY), esc(sel), esc(sort))) if query else "")
            .replace("{{CARDS}}", "\n".join(cards)).replace("{{KEY}}", esc(KEY)).replace("{{BASE}}", esc(BASE))
            .replace("{{BRAND}}", esc(BRAND)).replace("{{LOGO}}", esc((BRAND or "D")[:1].upper()))
            .replace("{{NOW}}", datetime.now(CET).strftime("%Y-%m-%d %H:%M")))

# ---------------------------------------------------------------- admin ------
def _seed_version(c, did, dtype, source, filename, created):
    c.execute("INSERT OR REPLACE INTO versions(doc_id,version,type,source,filename,labels,note,created) VALUES(?,?,?,?,?,?,?,?)",
              (did, 1, dtype, source, filename, "{}", "", created))

IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
def _doc_ext(dtype, filename):
    if dtype == "pdf": return ".pdf"
    if dtype == "img":
        e = os.path.splitext(filename or "")[1].lower()
        return e if e in IMG_EXTS else ".png"
    return ".html"

def create_url_doc(name, target, folder_id=""):
    did = secrets.token_hex(4); now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        c.execute("INSERT INTO documents(id,name,type,source,filename,labels,version,folder_id,created) VALUES(?,?,?,?,?,?,?,?,?)",
                  (did, name, "url", target, "", "{}", 1, folder_id or "", now))
        _seed_version(c, did, "url", target, "", now)
    return did
def create_file_doc(name, dtype, filename, data, folder_id=""):
    did = secrets.token_hex(4); now = datetime.now(timezone.utc).isoformat()
    ext = _doc_ext(dtype, filename)
    path = os.path.join(DOCDIR, "%s_v1%s" % (did, ext))
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    with open(path, mode) as f: f.write(data)
    with db() as c:
        c.execute("INSERT INTO documents(id,name,type,source,filename,labels,version,folder_id,created) VALUES(?,?,?,?,?,?,?,?,?)",
                  (did, name, dtype, path, filename or (did + ext), "{}", 1, folder_id or "", now))
        _seed_version(c, did, dtype, path, filename or (did + ext), now)
    return did

# ---------------------------------------------------------------- archive / folders --
def archive_doc(doc_id):
    """Archive: links answer 404, beacons are dropped, doc moves to the Archive view."""
    with db() as c:
        if not c.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone(): return False
        c.execute("UPDATE documents SET archived=COALESCE(archived,?) WHERE id=?",
                  (datetime.now(timezone.utc).isoformat(), doc_id))
    return True

def restore_doc(doc_id):
    with db() as c:
        if not c.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone(): return False
        c.execute("UPDATE documents SET archived=NULL WHERE id=?", (doc_id,))
    return True

def purge_doc(doc_id):
    """Permanently delete an ARCHIVED doc: documents/versions/links/sessions rows + files in DOCDIR.
    Returns dict of deleted counts, 'not_archived', or None (unknown doc)."""
    d = doc_row(doc_id)
    if not d: return None
    if not d["archived"]: return "not_archived"
    paths = set()
    with db() as c:
        for v in c.execute("SELECT source FROM versions WHERE doc_id=?", (doc_id,)):
            if v["source"]: paths.add(v["source"])
        if d["source"]: paths.add(d["source"])
        toks = [r["token"] for r in c.execute("SELECT token FROM links WHERE doc_id=?", (doc_id,))]
        n_sess = c.execute("DELETE FROM sessions WHERE doc_id=?", (doc_id,)).rowcount
        for t in toks:
            n_sess += c.execute("DELETE FROM sessions WHERE token=?", (t,)).rowcount
        n_links = c.execute("DELETE FROM links WHERE doc_id=?", (doc_id,)).rowcount
        n_vers = c.execute("DELETE FROM versions WHERE doc_id=?", (doc_id,)).rowcount
        c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    # unlink files ONLY inside DOCDIR (documents registered by path may point elsewhere: never touch those)
    real_docdir = os.path.realpath(DOCDIR)
    n_files = 0
    for p in paths:
        try:
            rp = os.path.realpath(p)
            if rp.startswith(real_docdir + os.sep) and os.path.isfile(rp):
                os.remove(rp); n_files += 1
        except Exception: pass
    try:
        for fn in os.listdir(DOCDIR):     # orphan sweep: <doc_id>_v*.…
            if fn.startswith(doc_id + "_v"):
                try: os.remove(os.path.join(DOCDIR, fn)); n_files += 1
                except Exception: pass
    except Exception: pass
    return {"links": n_links, "sessions": n_sess, "versions": n_vers, "files": n_files}

def folder_admin(p):
    """One-level folders: create/rename/delete/assign. Returns (http_status, payload)."""
    act = (p.get("action") or "").strip()
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        if act == "create":
            nm = (p.get("name") or "").strip()
            if not nm: return 400, {"error": "name required"}
            fid = secrets.token_hex(4)
            try: c.execute("INSERT INTO folders(id,name,pos,created) VALUES(?,?,0,?)", (fid, nm, now))
            except sqlite3.IntegrityError: return 409, {"error": "folder already exists"}
            return 200, {"ok": True, "id": fid}
        if act == "rename":
            fid = (p.get("id") or "").strip(); nm = (p.get("name") or "").strip()
            if not (fid and nm): return 400, {"error": "id+name required"}
            try: cur = c.execute("UPDATE folders SET name=? WHERE id=?", (nm, fid))
            except sqlite3.IntegrityError: return 409, {"error": "folder already exists"}
            return (200, {"ok": True}) if cur.rowcount else (404, {"error": "unknown folder"})
        if act == "delete":
            fid = (p.get("id") or "").strip()
            if not fid: return 400, {"error": "id required"}
            c.execute("UPDATE documents SET folder_id='' WHERE folder_id=?", (fid,))
            cur = c.execute("DELETE FROM folders WHERE id=?", (fid,))
            return (200, {"ok": True}) if cur.rowcount else (404, {"error": "unknown folder"})
        if act == "assign":
            did = (p.get("doc_id") or "").strip(); fid = (p.get("folder_id") or "").strip()
            if not did: return 400, {"error": "doc_id required"}
            if fid and not c.execute("SELECT 1 FROM folders WHERE id=?", (fid,)).fetchone():
                return 404, {"error": "unknown folder"}
            cur = c.execute("UPDATE documents SET folder_id=? WHERE id=?", (fid, did))
            return (200, {"ok": True}) if cur.rowcount else (404, {"error": "unknown document"})
    return 400, {"error": "unknown action"}

def update_doc_file(doc_id, dtype, filename, data):
    with db() as c:
        d = c.execute("SELECT version FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d: return None
    nv = (d["version"] or 1) + 1
    ext = _doc_ext(dtype, filename)
    path = os.path.join(DOCDIR, "%s_v%d%s" % (doc_id, nv, ext))
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    with open(path, mode) as f: f.write(data)
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        c.execute("INSERT OR REPLACE INTO versions(doc_id,version,type,source,filename,labels,note,created) VALUES(?,?,?,?,?,?,?,?)",
                  (doc_id, nv, dtype, path, filename or os.path.basename(path), "{}", "", now))
        c.execute("UPDATE documents SET type=?, source=?, filename=?, version=?, updated=?, labels='{}' WHERE id=?",
                  (dtype, path, filename or os.path.basename(path), nv, now, doc_id))
    return nv
def update_doc_url(doc_id, target):
    with db() as c:
        d = c.execute("SELECT version FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not d: return None
        nv = (d["version"] or 1) + 1
        now = datetime.now(timezone.utc).isoformat()
        c.execute("INSERT OR REPLACE INTO versions(doc_id,version,type,source,filename,labels,note,created) VALUES(?,?,?,?,?,?,?,?)",
                  (doc_id, nv, "url", target, "", "{}", "", now))
        c.execute("UPDATE documents SET type='url', source=?, version=?, updated=? WHERE id=?", (target, nv, now, doc_id))
    return nv
def make_pw(pw):
    if not pw: return ""
    salt = secrets.token_hex(6)
    return salt + "$" + hashlib.sha256((salt + pw).encode()).hexdigest()
def check_pw(pw, stored):
    if not stored: return True
    try: salt, h = stored.split("$", 1)
    except Exception: return False
    return hmac.compare_digest(h, hashlib.sha256((salt + (pw or "")).encode()).hexdigest())
def cookie_sig(token, stored):
    return hmac.new((KEY or "k").encode(), (token + "|" + stored).encode(), hashlib.sha256).hexdigest()[:24]
def cookie_name(token):
    return "u" + hashlib.md5(token.encode()).hexdigest()[:12]

def add_link(doc_id, name, email="", pw=""):
    tok = secrets.token_urlsafe(9)
    with db() as c:
        c.execute("INSERT INTO links(token,doc_id,name,email,pw,created) VALUES(?,?,?,?,?,?)",
                  (tok, doc_id, name, email, make_pw(pw), datetime.now(timezone.utc).isoformat()))
    return tok

# ---------------------------------------------------------------- serve view -
def serve_view(token, want_file):
    lr = link_row(token)
    if not lr: return ("404", None)
    d = doc_row(lr["doc_id"])
    if not d: return ("404", None)
    if d["archived"]: return ("archived", None)
    if d["type"] == "url":
        return ("redirect", d["source"])
    if d["type"] == "pdf":
        if want_file:
            try: return ("bytes", (open(d["source"], "rb").read(), "application/pdf"))
            except Exception: return ("404", None)
        return ("html", PDF_VIEW.replace("{{TOKEN}}", token).replace("{{NAME}}", html.escape(d["name"])))
    if d["type"] == "img":
        if want_file:
            try:
                mime = mimetypes.guess_type(d["source"])[0] or "image/png"
                return ("bytes", (open(d["source"], "rb").read(), mime))
            except Exception: return ("404", None)
        return ("html", IMG_VIEW.replace("{{TOKEN}}", token).replace("{{NAME}}", html.escape(d["name"])))
    # html
    if want_file:
        return ("404", None)
    try:
        txt = open(d["source"], encoding="utf-8", errors="replace").read()
    except Exception:
        return ("404", None)
    if "navigator.sendBeacon" not in txt and "/beacon.js" not in txt:
        inj = '\n<script src="/beacon.js"></script>\n'
        txt = txt.replace("</body>", inj + "</body>") if "</body>" in txt else txt + inj
    if "/cmt.js" not in txt:
        cinj = '\n<script src="/cmt.js"></script>\n'
        txt = txt.replace("</body>", cinj + "</body>") if "</body>" in txt else txt + cinj
    return ("html", txt)

def form_answers_view():
    """Simple admin view: newest questionnaire submissions, latest first."""
    esc = lambda s: html.escape(str(s), quote=False)
    with db() as c:
        rows = c.execute("SELECT * FROM form_answers ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        return "<body style='font-family:sans-serif;padding:40px'><h2>No answers yet.</h2></body>"
    out = ["""<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<body style="font-family:Inter,system-ui,sans-serif;background:#f7f9fc;color:#0f172a;margin:0;padding:28px">
<div style="max-width:820px;margin:0 auto"><h1 style="font-size:22px">📋 Questionnaire answers</h1>"""]
    for r in rows:
        try: a = json.loads(r["payload"] or "{}")
        except Exception: a = {}
        badge = "✅ SUBMITTED" if r["done"] else "📝 Draft"
        try: formlbl = r["form"] or "form"
        except (IndexError, KeyError): formlbl = "form"
        out.append("<div style='background:#fff;border:1px solid #e8ecf3;border-radius:14px;padding:18px;margin:14px 0'>")
        out.append("<div style='font-size:12px;color:#64748b;margin-bottom:10px'><b style='color:%s'>%s</b> · %s · %s · %s · %s answered</div>" % (
            "#16a34a" if r["done"] else "#b45309", badge, esc(formlbl), esc((r["ts"] or "")[:16].replace("T", " ")), esc(r["ip"] or ""), esc(r["progress"] or "?")))
        out.append("<table style='width:100%;border-collapse:collapse;font-size:13.5px'>")
        for k in sorted(a.keys()):
            v = a[k]
            if isinstance(v, list): v = " · ".join(str(x) for x in v)
            elif isinstance(v, dict): v = ", ".join("%s: %s%%" % (kk, vv) for kk, vv in v.items() if str(vv).strip())
            out.append("<tr><td style='padding:5px 10px 5px 0;color:#2563eb;font-weight:700;white-space:nowrap;vertical-align:top'>%s</td><td style='padding:5px 0;border-bottom:1px solid #f1f5f9'>%s</td></tr>" % (esc(k), esc(str(v))))
        out.append("</table></div>")
    out.append("</div></body>")
    return "".join(out)

# ---------------------------------------------------------------- HTTP --------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _ip(self): return self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP") or self.client_address[0]
    def _s(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str): body = body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        if body: self.wfile.write(body)
    def _guard(self, q): return not (KEY and q.get("key", [""])[0] != KEY)
    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or "0"); return self.rfile.read(n) if n else b""
    def _unlocked(self, token, stored):
        want = cookie_sig(token, stored); nm = cookie_name(token)
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            part = part.strip()
            if part.startswith(nm + "="):
                return hmac.compare_digest(part.split("=", 1)[1], want)
        return False

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query); path = u.path
        if path == "/health": return self._s(200, b"ok")
        if path == "/beacon.js": return self._s(200, BEACON, "application/javascript; charset=utf-8")
        if path == "/cmt.js": return self._s(200, CMT_JS, "application/javascript; charset=utf-8")
        m = re.match(r"^/r/([A-Za-z0-9_-]{4,})(/file|/comments)?$", path)
        if m:
            token = m.group(1); suffix = m.group(2) or ""; is_file = suffix == "/file"; lr = link_row(token)
            if not lr: return self._s(404, b"not found")
            dd = doc_row(lr["doc_id"])
            if dd and dd["archived"]:
                if is_file: return self._s(404, b"not found")
                return self._s(404, GONE_PAGE, "text/html; charset=utf-8")
            if lr["pw"] and not self._unlocked(token, lr["pw"]):
                if suffix: return self._s(403, b"locked")
                return self._s(200, PW_GATE.replace("{{TOKEN}}", token).replace("{{ERR}}", ""), "text/html; charset=utf-8")
            if suffix == "/comments":
                return self._s(200, json.dumps({"ok": True, "me": name_for(token),
                                                "comments": comments_for(lr["doc_id"])}, ensure_ascii=False),
                               "application/json; charset=utf-8")
            kind, payload = serve_view(token, is_file)
            if kind == "404": return self._s(404, b"not found")
            if kind == "archived": return self._s(404, GONE_PAGE, "text/html; charset=utf-8")
            if kind == "redirect":
                # log click (e:"click" -> no ntfy; the target page's beacon reports the real
                # open with geo/device) then redirect WITH attribution so external pages
                # that load /beacon.js can send scroll/dwell heatmap data for this link.
                try: handle_beacon({"t": m.group(1), "s": "clk" + secrets.token_hex(6), "e": "click", "furthest": "(extern)"}, self._ip(), self.headers.get("User-Agent",""))
                except Exception: pass
                sep = "&" if "?" in payload else "?"
                return self._s(302, b"", extra={"Location": payload + sep + "r=" + token})
            if kind == "bytes":
                data, mime = payload
                return self._s(200, data, mime)
            return self._s(200, payload, "text/html; charset=utf-8")
        if path == "/form":
            form = FORMS.get(q.get("k", [""])[0] or "")
            if not form: return self._s(403, b"forbidden")
            try:
                page = open(os.path.join(FORMS_DIR, form[0]), encoding="utf-8").read()
            except FileNotFoundError:
                return self._s(404, form[0] + " missing")
            return self._s(200, page, "text/html; charset=utf-8")
        if path == "/form/answers":
            if not self._guard(q): return self._s(403, b"forbidden")
            return self._s(200, form_answers_view(), "text/html; charset=utf-8")
        if path in ("/dash", "/api/monitor/activity", "/api/monitor/links", "/api/sessions", "/admin/add") and not self._guard(q):
            return self._s(403, b"forbidden")
        if path == "/api/monitor/activity":
            return self._s(200, json.dumps(activity_summary(q.get("doc", [""])[0] or None)), "application/json")
        if path == "/api/monitor/links":
            return self._s(200, json.dumps(links_summary(q.get("doc", [""])[0] or None)), "application/json")
        if path == "/api/sessions":
            tok = (q.get("token", [""])[0] or "").strip()
            if not tok: return self._s(400, b"token required")
            def _qi(name, dflt):
                try: return int(q.get(name, [str(dflt)])[0] or str(dflt))
                except Exception: return dflt
            ver = _qi("ver", 0); off = max(0, _qi("offset", 0)); lim = min(200, max(1, _qi("limit", 50)))
            return self._s(200, sess_fragment(tok, ver, off, lim), "text/html; charset=utf-8")
        if path == "/dash": return self._s(200, dashboard(q), "text/html; charset=utf-8")
        if path == "/admin/add":
            nm = (q.get("name", [""])[0] or "").strip()
            if not nm: return self._s(400, b"name required")
            docid = (q.get("doc", [""])[0] or "").strip()
            dd = doc_row(docid)
            if not dd: return self._s(404, b"unknown doc")
            if dd["archived"]: return self._s(409, b"document archived")
            tok = add_link(docid, nm, (q.get("email", [""])[0] or "").strip(), (q.get("pw", [""])[0] or "").strip())
            return self._s(200, BASE + "/r/" + tok)
        return self._s(404, b"not found")

    def do_OPTIONS(self):
        # CORS preflight for cross-origin beacons (external pages that load /beacon.js -> /t)
        if urllib.parse.urlparse(self.path).path == "/t":
            return self._s(204, b"", extra={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "86400"})
        return self._s(404, b"not found")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query); path = u.path
        mc = re.match(r"^/r/([A-Za-z0-9_-]{4,})/comments$", path)
        if mc:
            token = mc.group(1); lr = link_row(token)
            if not lr: return self._s(404, b"not found")
            dd = doc_row(lr["doc_id"])
            if dd and dd["archived"]: return self._s(404, b"gone")
            if lr["pw"] and not self._unlocked(token, lr["pw"]): return self._s(403, b"locked")
            raw = self._body()
            if len(raw) > 100_000: return self._s(413, b"too large")
            try: p = json.loads(raw.decode("utf-8") or "{}")
            except Exception: return self._s(400, b"bad json")
            try:
                res = add_comment(lr["doc_id"], token, p, self._ip())
            except (ValueError, TypeError):
                return self._s(400, b"bad payload")
            return self._s(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
        mm = re.match(r"^/r/([A-Za-z0-9_-]{4,})$", path)
        if mm:
            token = mm.group(1); lr = link_row(token)
            if not lr: return self._s(404, b"not found")
            dd = doc_row(lr["doc_id"])
            if dd and dd["archived"]: return self._s(404, GONE_PAGE, "text/html; charset=utf-8")
            form = urllib.parse.parse_qs(self._body().decode("utf-8", "replace"))
            if check_pw(form.get("pw", [""])[0], lr["pw"]):
                ck = "%s=%s; Path=/; Max-Age=28800; HttpOnly; SameSite=Lax" % (cookie_name(token), cookie_sig(token, lr["pw"]))
                kind, payload = serve_view(token, False)
                if kind == "redirect":
                    try: handle_beacon({"t": token, "s": "clk" + secrets.token_hex(6), "e": "click", "furthest": "(extern)"}, self._ip(), self.headers.get("User-Agent", ""))
                    except Exception: pass
                    sep = "&" if "?" in payload else "?"
                    return self._s(302, b"", extra={"Location": payload + sep + "r=" + token, "Set-Cookie": ck})
                if kind == "html":
                    return self._s(200, payload, "text/html; charset=utf-8", {"Set-Cookie": ck})
                return self._s(404, b"not found")
            return self._s(200, PW_GATE.replace("{{TOKEN}}", token).replace("{{ERR}}", "Wrong password."), "text/html; charset=utf-8")
        if path == "/t":
            try:
                pb = json.loads(self._body().decode("utf-8") or "{}")
                # Which site sent the beacon, for pushes without a tracker document
                pb["_pg"] = urllib.parse.urlparse(self.headers.get("Origin") or self.headers.get("Referer") or "").netloc[:80]
                handle_beacon(pb, self._ip(), self.headers.get("User-Agent",""))
            except Exception: pass
            return self._s(204, b"", extra={"Access-Control-Allow-Origin": "*"})
        if path == "/api/form":
            raw = self._body()
            if len(raw) > 300_000: return self._s(413, b"too large")
            try: p = json.loads(raw.decode("utf-8") or "{}")
            except Exception: return self._s(400, b"bad json")
            form = FORMS.get(p.get("k") or "")
            if not form: return self._s(403, b"forbidden")
            done = 1 if p.get("done") else 0
            answers = p.get("answers") or {}
            if not isinstance(answers, dict): return self._s(400, b"bad answers")
            with db() as c:
                c.execute("INSERT INTO form_answers(ts,ip,done,progress,payload,form) VALUES(?,?,?,?,?,?)",
                          (datetime.now(timezone.utc).isoformat(), self._ip(), done,
                           str(p.get("progress") or ""), json.dumps(answers, ensure_ascii=False), form[1]))
            if done:
                ans_url = BASE + "/form/answers?key=" + KEY
                body = "Questionnaire %s submitted (%s answered).\nAnswers: %s" % (
                    form[1], p.get("progress") or "?", ans_url)
                ntfy("📋 Questionnaire submitted", body, ans_url)
                telegram("📋 %s: questionnaire submitted (%s)" % (form[1], p.get("progress") or "?"),
                         body, ans_url, btn="View answers")
                email("📋 %s: questionnaire submitted (%s)" % (form[1], p.get("progress") or "?"),
                      body + "\n\n" + "\n".join("%s: %s" % (k, json.dumps(v, ensure_ascii=False)) for k, v in sorted(answers.items())))
            return self._s(200, json.dumps({"ok": True}), "application/json")
        if not self._guard(q): return self._s(403, b"forbidden")
        if path == "/admin/doc":
            try: p = json.loads(self._body().decode("utf-8") or "{}")
            except Exception: return self._s(400, b"bad json")
            if not p.get("name") or not p.get("target"): return self._s(400, b"name+target required")
            did = create_url_doc(p["name"].strip(), p["target"].strip(), (p.get("folder_id") or "").strip())
            return self._s(200, json.dumps({"doc_id": did}), "application/json")
        if path == "/admin/upload":
            dtype = self.headers.get("X-Doc-Type", "html").lower()
            name = urllib.parse.unquote(self.headers.get("X-Doc-Name", "Document"))
            fname = urllib.parse.unquote(self.headers.get("X-Filename", ""))
            data = self._body()
            if not data: return self._s(400, b"empty")
            if dtype not in ("pdf", "img"):
                dtype = "html"
                try: data = data.decode("utf-8")
                except Exception: pass
            fid = urllib.parse.unquote(self.headers.get("X-Folder-Id", "") or "").strip()
            did = create_file_doc(name, dtype, fname, data, fid)
            return self._s(200, json.dumps({"doc_id": did}), "application/json")
        if path == "/admin/update":
            if "application/json" in (self.headers.get("Content-Type", "") or ""):
                try: p = json.loads(self._body().decode("utf-8") or "{}")
                except Exception: return self._s(400, b"bad json")
                if not p.get("doc_id") or not p.get("target"): return self._s(400, b"doc_id+target required")
                dd = doc_row(p["doc_id"])
                if dd and dd["archived"]: return self._s(409, b"document archived - restore it first")
                nv = update_doc_url(p["doc_id"], p["target"].strip())
            else:
                did = self.headers.get("X-Doc-Id", "")
                dtype = self.headers.get("X-Doc-Type", "html").lower()
                fname = urllib.parse.unquote(self.headers.get("X-Filename", ""))
                data = self._body()
                if not did or not data: return self._s(400, b"doc_id+file required")
                dd = doc_row(did)
                if dd and dd["archived"]: return self._s(409, b"document archived - restore it first")
                if dtype not in ("pdf", "img"):
                    dtype = "html"
                    try: data = data.decode("utf-8")
                    except Exception: pass
                nv = update_doc_file(did, dtype, fname, data)
            if nv is None: return self._s(404, b"doc not found")
            return self._s(200, json.dumps({"version": nv}), "application/json")
        if path == "/admin/link":
            try: p = json.loads(self._body().decode("utf-8") or "{}")
            except Exception: return self._s(400, b"bad json")
            if not p.get("doc_id") or not p.get("name"): return self._s(400, b"doc_id+name required")
            dd = doc_row(p["doc_id"])
            if dd and dd["archived"]: return self._s(409, b"document archived")
            tok = add_link(p["doc_id"], p["name"].strip(), (p.get("email") or "").strip(), (p.get("pw") or "").strip())
            return self._s(200, json.dumps({"link": BASE + "/r/" + tok, "pw": bool((p.get("pw") or "").strip())}), "application/json")
        if path == "/admin/dellink":
            try: p = json.loads(self._body().decode("utf-8") or "{}")
            except Exception: return self._s(400, b"bad json")
            tok = (p.get("token") or "").strip()
            if not tok: return self._s(400, b"token required")
            with db() as c:
                c.execute("DELETE FROM links WHERE token=?", (tok,))
                c.execute("DELETE FROM sessions WHERE token=?", (tok,))
            return self._s(200, json.dumps({"ok": True}), "application/json")
        if path in ("/admin/archivedoc", "/admin/restoredoc"):
            try: p = json.loads(self._body().decode("utf-8") or "{}")
            except Exception: return self._s(400, b"bad json")
            did = (p.get("doc_id") or "").strip()
            if not did: return self._s(400, b"doc_id required")
            ok = archive_doc(did) if path.endswith("archivedoc") else restore_doc(did)
            if not ok: return self._s(404, b"doc not found")
            return self._s(200, json.dumps({"ok": True}), "application/json")
        if path == "/admin/purgedoc":
            try: p = json.loads(self._body().decode("utf-8") or "{}")
            except Exception: return self._s(400, b"bad json")
            did = (p.get("doc_id") or "").strip()
            if not did: return self._s(400, b"doc_id required")
            if (p.get("confirm") or "") != did: return self._s(400, b"confirm mismatch")
            res = purge_doc(did)
            if res is None: return self._s(404, b"doc not found")
            if res == "not_archived": return self._s(409, b"archive it first")
            return self._s(200, json.dumps({"ok": True, "deleted": res}), "application/json")
        if path == "/admin/folder":
            try: p = json.loads(self._body().decode("utf-8") or "{}")
            except Exception: return self._s(400, b"bad json")
            code, payload = folder_admin(p)
            return self._s(code, json.dumps(payload), "application/json")
        if path == "/admin/feature":
            try: p = json.loads(self._body().decode("utf-8") or "{}")
            except Exception: return self._s(400, b"bad json")
            res = add_feature(p.get("text"), p.get("author"))
            if not res: return self._s(400, b"text required")
            return self._s(200, json.dumps({"ok": True, "feature": res}, ensure_ascii=False), "application/json")
        if path == "/admin/feature/status":
            try: p = json.loads(self._body().decode("utf-8") or "{}")
            except Exception: return self._s(400, b"bad json")
            fid = int(p.get("id") or 0)
            if not fid: return self._s(400, b"id required")
            set_feature(fid, status=p.get("status"), delete=bool(p.get("delete")))
            return self._s(200, json.dumps({"ok": True}), "application/json")
        return self._s(404, b"not found")

# ---------------------------------------------------------------- assets ------
CMT_JS = r"""(function(){"use strict";
var EN=/^en/i.test(document.documentElement.lang||"");function L(de,en){return EN?en:de}
function RL(c){if(!EN||!c.ts)return c.rel;var t=Date.parse(c.ts);if(isNaN(t))return c.rel;var d=Math.max(0,(Date.now()-t)/1000);return d<90?"just now":d<3600?Math.floor(d/60)+" min ago":d<86400?Math.floor(d/3600)+" h ago":Math.floor(d/86400)+" d ago"}
var m=location.pathname.match(/^\/r\/([A-Za-z0-9_-]{4,})/);if(!m)return;
var TOKEN=m[1],API="/r/"+TOKEN+"/comments",me="",comments=[],addMode=false,docH=0,pop=null,hint=null;
try{me=localStorage.getItem("cmt_name_"+TOKEN)||""}catch(e){}
var C="#123B30",C2="#1C5142",BR="#A9761F",INK="#19241F",MUT="#55605A",LN="#D3D7CE",PP="#F4F5F2";
var oHost=document.createElement("div");
oHost.style.cssText="position:absolute;top:0;left:0;width:100%;pointer-events:none;z-index:2147482000";
var fHost=document.createElement("div");
fHost.style.cssText="position:fixed;top:0;left:0;width:0;height:0;z-index:2147483000";
var b=document.body||document.documentElement;b.appendChild(oHost);b.appendChild(fHost);
var oR=oHost.attachShadow({mode:"open"}),fR=fHost.attachShadow({mode:"open"});
oR.innerHTML="<style>"+
"*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}"+
".pin{position:absolute;pointer-events:auto;cursor:pointer;transform:translateY(-50%)}"+
".dot{width:26px;height:26px;border-radius:50% 50% 50% 3px;background:"+C+";color:#fff;font-size:12px;font-weight:600;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,.28);border:2px solid #fff}"+
".pin.res .dot{background:#9aa8a0}.pin:hover .dot{background:"+C2+"}"+
"</style><div id=pins></div>";
fR.innerHTML="<style>"+
"*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}"+
".fab{position:fixed;bottom:18px;right:18px;background:"+C+";color:#fff;border:none;border-radius:24px;padding:11px 15px;font-size:14px;font-weight:600;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.22)}"+
".fab.act{background:"+BR+"}"+
".hint{position:fixed;bottom:64px;right:18px;background:"+INK+";color:#fff;font-size:12px;padding:7px 11px;border-radius:7px;max-width:230px;line-height:1.35}"+
".pop{position:fixed;width:300px;max-width:92vw;background:"+PP+";border:1px solid "+LN+";border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.24);overflow:hidden}"+
".pop .hd{background:"+C+";color:#fff;padding:8px 12px;font-size:12px;display:flex;justify-content:space-between;align-items:center;gap:8px}"+
".pop .hd b{font-weight:600}.pop .hd button{background:none;border:none;color:#fff;font-size:18px;cursor:pointer;line-height:1}"+
".pop .bd{max-height:44vh;overflow:auto;padding:10px 12px}"+
".c{margin:0 0 9px}.c .mt{font-size:11px;color:"+MUT+";margin-bottom:3px}.c .au{font-weight:600;color:"+C+"}"+
".c .tx{font-size:13.5px;color:"+INK+";white-space:pre-wrap;line-height:1.4}"+
".rep{margin:6px 0 0 10px;border-left:2px solid "+LN+";padding-left:8px}"+
".foot{border-top:1px solid "+LN+";padding:9px 12px;background:#ECEEE8}"+
".foot textarea{width:100%;border:1px solid #C2C8BC;border-radius:6px;padding:7px 9px;font-size:13px;resize:vertical;min-height:46px;font-family:inherit}"+
".foot .row{display:flex;gap:7px;margin-top:7px;align-items:center}.foot input{flex:1;border:1px solid #C2C8BC;border-radius:6px;padding:6px 8px;font-size:12px}"+
".foot button{background:"+C+";color:#fff;border:none;border-radius:6px;padding:7px 13px;font-size:12px;font-weight:600;cursor:pointer}.foot button:disabled{opacity:.5}"+
".rz{font-size:11px;color:#fff;cursor:pointer;text-decoration:underline;margin-left:auto}"+
"</style><button class=fab id=fab>"+L("＋ Kommentar","＋ Comment")+"</button>";
var pins=oR.getElementById("pins"),fab=fR.getElementById("fab");
function esc(s){return (s||"").replace(/[&<>\"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function docHeight(){return Math.max(document.documentElement.scrollHeight,document.body?document.body.scrollHeight:0)}
function section(cy){var hs=document.querySelectorAll("h1,h2,h3,h4"),r0="";for(var i=0;i<hs.length;i++){var r=hs[i].getBoundingClientRect();if(r.top<=(cy||150))r0=(hs[i].textContent||"").trim();else if(r.top>0)break;}return r0.slice(0,50)}
function layout(){docH=docHeight();oHost.style.height=docH+"px";renderPins();}
function renderPins(){
  var tops=comments.filter(function(c){return !c.parent});
  pins.innerHTML=tops.map(function(c){
    var n=1+comments.filter(function(r){return r.parent===c.id}).length;
    var top=(c.y||0)/100*docH;
    return "<div class='pin"+(c.resolved?" res":"")+"' data-id='"+c.id+"' style='top:"+top+"px;right:12px'><div class=dot>"+n+"</div></div>";
  }).join("");
  Array.prototype.forEach.call(pins.querySelectorAll(".pin"),function(p){p.onclick=function(ev){ev.stopPropagation();openThread(+p.getAttribute("data-id"),p);};});
}
function closePop(){if(pop){pop.remove();pop=null;}}
function place(el,rect){
  var w=300,left=rect.left-w-12;
  if(left<10)left=Math.min(rect.right+12,window.innerWidth-w-10);
  if(left<10)left=Math.max(10,window.innerWidth-w-10);
  var top=Math.max(10,Math.min(rect.top,window.innerHeight-340));
  el.style.left=left+"px";el.style.top=top+"px";
}
function threadHtml(c){
  var reps=comments.filter(function(r){return r.parent===c.id});
  return "<div class=c><div class=mt><span class=au>"+esc(c.author)+"</span> · "+esc(RL(c))+(c.page?" · "+esc(c.page):"")+"</div><div class=tx>"+esc(c.text)+"</div></div>"+
    reps.map(function(r){return "<div class='c rep'><div class=mt><span class=au>"+esc(r.author)+"</span> · "+esc(RL(r))+"</div><div class=tx>"+esc(r.text)+"</div></div>"}).join("");
}
function ensureName(nmEl){var nm=me||(nmEl&&nmEl.value.trim()||"");if(!nm){if(nmEl){nmEl.style.display="block";nmEl.focus();}return null;}if(!me){me=nm;try{localStorage.setItem("cmt_name_"+TOKEN,nm)}catch(e){}}return nm;}
function post(b2){return fetch(API,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b2)}).then(function(r){return r.json()})}
function load(cb){fetch(API).then(function(r){return r.json()}).then(function(d){if(d&&d.ok){if(!me&&d.me)me=d.me;comments=d.comments||[];layout();if(cb)cb();}}).catch(function(){})}
function openThread(id,pinEl){
  closePop();var c=comments.filter(function(x){return x.id===id})[0];if(!c)return;
  pop=document.createElement("div");pop.className="pop";
  pop.innerHTML="<div class=hd><b>"+L("Kommentar","Comment")+"</b><span class=rz id=rz>"+(c.resolved?L("↩ öffnen","↩ reopen"):L("✓ erledigt","✓ resolve"))+"</span><button id=x>×</button></div>"+
    "<div class=bd>"+threadHtml(c)+"</div>"+
    "<div class=foot><textarea id=ta placeholder='"+L("Antworten…","Reply…")+"'></textarea><div class=row><input id=nm placeholder='"+L("Dein Name","Your name")+"' style='display:"+(me?"none":"block")+"'><button id=snd>"+L("Antwort senden","Send reply")+"</button></div></div>";
  fR.appendChild(pop);place(pop,pinEl.getBoundingClientRect());
  fR.getElementById("x").onclick=closePop;
  fR.getElementById("rz").onclick=function(){post({resolve:!c.resolved,id:c.id}).then(function(){load(closePop);});};
  fR.getElementById("snd").onclick=function(){var t=fR.getElementById("ta").value.trim();if(!t)return;var nm=ensureName(fR.getElementById("nm"));if(!nm)return;post({text:t,parent:id,author:nm}).then(function(){load(closePop);});};
}
function openCompose(x,y,cx,cy){
  closePop();setMode(false);
  pop=document.createElement("div");pop.className="pop";
  pop.innerHTML="<div class=hd><b>"+L("Neuer Kommentar","New comment")+(section(cy)?" · "+esc(section(cy)):"")+"</b><button id=x>×</button></div>"+
    "<div class=foot style='border-top:none'><textarea id=ta placeholder='"+L("Kommentar oder Frage…","Comment or question…")+"'></textarea><div class=row><input id=nm placeholder='"+L("Dein Name","Your name")+"' style='display:"+(me?"none":"block")+"'><button id=snd>"+L("Senden","Send")+"</button></div></div>";
  fR.appendChild(pop);place(pop,{left:cx,right:cx,top:Math.min(cy,window.innerHeight-340)});
  fR.getElementById("x").onclick=closePop;
  var ta=fR.getElementById("ta");if(ta)ta.focus();
  fR.getElementById("snd").onclick=function(){var t=ta.value.trim();if(!t)return;var nm=ensureName(fR.getElementById("nm"));if(!nm)return;post({text:t,parent:0,author:nm,x:x,y:y,page:section(cy)}).then(function(){load(closePop);});};
}
function setMode(on){addMode=on;fab.classList.toggle("act",on);document.documentElement.style.cursor=on?"crosshair":"";
  if(on){if(!hint){hint=document.createElement("div");hint.className="hint";hint.textContent=L("Klick auf die Stelle im Dokument, die du kommentieren willst.","Click the spot in the document you want to comment on.");fR.appendChild(hint);}}
  else if(hint){hint.remove();hint=null;}}
fab.onclick=function(){closePop();setMode(!addMode);};
document.addEventListener("click",function(e){
  if(!addMode)return;
  if(e.composedPath){var p=e.composedPath();if(p.indexOf(oHost)>=0||p.indexOf(fHost)>=0)return;}
  var x=e.pageX/Math.max(1,document.documentElement.scrollWidth)*100;
  var y=e.pageY/Math.max(1,docHeight())*100;
  openCompose(x,y,e.clientX,e.clientY);
},true);
document.addEventListener("keydown",function(e){if(e.key==="Escape"){closePop();setMode(false);}});
window.addEventListener("resize",function(){layout();closePop();});
window.addEventListener("load",layout);
setTimeout(layout,1200);
load();
})();"""

BEACON = r"""(function(){"use strict";
var sp=new URLSearchParams(location.search);
var m=location.pathname.match(/\/r\/([A-Za-z0-9_-]{4,})/);var token=(m&&m[1])||sp.get("r")||"";
try{if(token){sessionStorage.setItem("_dst",token);}else{token=sessionStorage.getItem("_dst")||"";}}catch(e){}
if(sp.get("r")){try{sp.delete("r");var qs=sp.toString();history.replaceState(null,"",location.pathname+(qs?"?"+qs:"")+location.hash);}catch(e){}}
var EP="/t";try{var cs=document.currentScript;if(cs&&cs.src){EP=new URL(cs.src).origin+"/t";}}catch(e){}
var vid=localStorage.getItem("_dsv");if(!vid){vid=Date.now().toString(36)+Math.random().toString(36).slice(2,8);try{localStorage.setItem("_dsv",vid);}catch(e){}}
var sid=Date.now().toString(36)+Math.random().toString(36).slice(2,6);
function els(){var a=[].slice.call(document.querySelectorAll("[data-track]"));if(a.length)return a;return [].slice.call(document.querySelectorAll("section[id], .foot"));}
var secs=els(),virtual=secs.length===0,dwell={},labels={},maxScroll=0,furthest="",furIdx=-1,active=0,start=Date.now(),lastAct=Date.now();
/* virtual mode (external apps without [data-track] sections): one heat row per
   route: dwell = time on each page, labels prettified from the pathname. */
var vOrder=[];
function vKey(){return location.pathname||"/";}
function vLbl(){var p=(location.pathname||"/").replace(/\/$/,"");if(!p)return"Overview";
  var s=p.split("/").pop().replace(/\.[a-z0-9]+$/i,"").replace(/[-_]/g," ");
  return s?s.charAt(0).toUpperCase()+s.slice(1):"Overview";}
function keyOf(s){return s.getAttribute("data-track")||s.id||"foot";}
function lblOf(s){return s.getAttribute("data-track-label")||((s.querySelector("h1,h2,h3")||{}).textContent||"").trim().slice(0,80)||keyOf(s);}
secs.forEach(function(s){var k=keyOf(s);dwell[k]=0;labels[k]=lblOf(s);});
function sTop(){return window.pageYOffset||(document.scrollingElement||document.documentElement).scrollTop||document.body.scrollTop||0;}
function docH(){var b=document.body,e=document.documentElement;return Math.max(b.scrollHeight,e.scrollHeight,b.offsetHeight,e.offsetHeight);}
function nav(){var mx=(docH()-innerHeight)||1;var p=Math.min(100,Math.round(sTop()/mx*100));if(p>maxScroll)maxScroll=p;}
function mark(){lastAct=Date.now();}
["scroll","mousemove","keydown","click","touchstart","touchmove","wheel"].forEach(function(ev){window.addEventListener(ev,function(){mark();if(ev!=="mousemove"&&ev!=="keydown"&&ev!=="click")nav();},{passive:true});});
/* which section sits on the reading line (~42% down the viewport)? robust to tall sections on mobile */
function current(){var line=innerHeight*0.42,best=null,bi=-1,bestOv=-1;
  for(var i=0;i<secs.length;i++){var r=secs[i].getBoundingClientRect();if(r.height<=0)continue;
    if(r.top<=line&&r.bottom>=line){return {el:secs[i],i:i};}
    var ov=Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(r.top,0));if(ov>bestOv){bestOv=ov;best=secs[i];bi=i;}}
  return best?{el:best,i:bi}:null;}
setInterval(function(){if(document.visibilityState!=="visible")return;if(Date.now()-lastAct>30000)return;active+=1000;
  if(virtual){var vk=vKey();if(!(vk in dwell)){dwell[vk]=0;labels[vk]=vLbl();vOrder.push(vk);}
    dwell[vk]+=1000;var vi=vOrder.indexOf(vk);if(vi>furIdx){furIdx=vi;furthest=vk;}return;}
  var c=current();if(c){var k=keyOf(c.el);dwell[k]=(dwell[k]||0)+1000;if(c.i>furIdx){furIdx=c.i;furthest=k;}}},1000);
function bd(kind){return JSON.stringify({t:token,v:vid,s:sid,e:kind,url:location.href,ref:document.referrer||"",scr:screen.width+"x"+screen.height+"@"+(window.devicePixelRatio||1),vp:innerWidth+"x"+innerHeight,lang:navigator.language||"",tz:(function(){try{return Intl.DateTimeFormat().resolvedOptions().timeZone;}catch(e){return"";}})(),maxScroll:maxScroll,furthest:furthest,activeMs:active,totalMs:Date.now()-start,dwell:dwell,labels:labels});}
function send(kind,beacon){try{var b=bd(kind);if(beacon&&navigator.sendBeacon){navigator.sendBeacon(EP,new Blob([b],{type:"application/json"}));}else{fetch(EP,{method:"POST",body:b,headers:{"Content-Type":"application/json"},keepalive:true}).catch(function(){});}}catch(e){}}
nav();send("open");setInterval(function(){send("beat");},8000);
document.addEventListener("visibilitychange",function(){if(document.visibilityState==="hidden")send("end",true);});
window.addEventListener("pagehide",function(){send("end",true);});
window.addEventListener("blur",function(){send("beat");});})();"""

PDF_VIEW = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex,nofollow"><title>{{NAME}}</title>
<style>body{margin:0;background:#525659;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
#top{position:sticky;top:0;background:#323639;color:#eee;padding:8px 14px;font-size:14px;z-index:5}
#pages{max-width:920px;margin:0 auto;padding:16px}section{margin:0 0 14px;background:#fff;box-shadow:0 1px 6px rgba(0,0,0,.4)}
canvas{display:block;width:100%;height:auto}.ld{color:#ccc;text-align:center;padding:40px}</style></head>
<body><div id=top>{{NAME}}</div><div id=pages><div class=ld>Loading document …</div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<script>
pdfjsLib.GlobalWorkerOptions.workerSrc="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
var url="/r/{{TOKEN}}/file";
pdfjsLib.getDocument(url).promise.then(function(pdf){
  var wrap=document.getElementById("pages");wrap.innerHTML="";
  var chain=Promise.resolve();
  for(var i=1;i<=pdf.numPages;i++){(function(n){chain=chain.then(function(){return pdf.getPage(n).then(function(page){
    var vp=page.getViewport({scale:1.4});var sec=document.createElement("section");
    sec.setAttribute("data-track","p"+n);sec.setAttribute("data-track-label","Page "+n);
    var cv=document.createElement("canvas");cv.width=vp.width;cv.height=vp.height;sec.appendChild(cv);wrap.appendChild(sec);
    return page.render({canvasContext:cv.getContext("2d"),viewport:vp}).promise;});});})(i);}
  chain.then(function(){var s=document.createElement("script");s.src="/beacon.js";document.body.appendChild(s);});
}).catch(function(e){document.getElementById("pages").innerHTML="<div class=ld>Could not load the PDF.</div>";});
</script></body></html>"""

IMG_VIEW = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex,nofollow"><title>{{NAME}}</title>
<style>body{margin:0;background:#525659;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
#top{position:sticky;top:0;background:#323639;color:#eee;padding:8px 14px;font-size:14px;z-index:5}
#wrap{max-width:1100px;margin:0 auto;padding:16px}
section{background:#fff;box-shadow:0 1px 6px rgba(0,0,0,.4)}
img{display:block;width:100%;height:auto}</style></head>
<body><div id=top>{{NAME}}</div><div id=wrap><section data-track="image" data-track-label="Image"><img src="/r/{{TOKEN}}/file" alt="{{NAME}}"></section></div>
<script src="/beacon.js"></script></body></html>"""

PW_GATE = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex,nofollow"><title>Protected</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#123b30;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
.box{background:#fff;border-radius:10px;padding:30px 28px;max-width:340px;width:90%;box-shadow:0 10px 40px rgba(0,0,0,.3);text-align:center}
h1{font-size:17px;margin:0 0 4px;color:#123b30}p{color:#7a857d;font-size:13px;margin:0 0 16px}
input{width:100%;padding:11px 12px;border:1px solid #d8dcd3;border-radius:7px;font-size:15px;margin-bottom:10px}
button{width:100%;padding:11px;background:#a9761f;color:#fff;border:0;border-radius:7px;font-size:14px;cursor:pointer}
.err{color:#a23530;font-size:12.5px;margin-bottom:10px;min-height:16px}</style></head>
<body><form class=box method=post action="/r/{{TOKEN}}">
<h1>🔒 Password protected</h1><p>This document is protected. Please enter the password.</p>
<div class=err>{{ERR}}</div><input type=password name=pw placeholder="Password" autofocus required>
<button>Open</button></form></body></html>"""

GONE_PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex,nofollow"><title>404</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#fff;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#333}</style></head>
<body><p>Page not available</p></body></html>"""

FEEDBACK_BOX = """<div class=fq>
  <div class=fqhead>
    <div class=fqtitle><span class=fqbulb>💡</span> Feature requests
      <span class=fqbadge>{{COUNT}} open</span></div>
    <div class=fqhint>Suggest new features for the tracking tool. Requests are pushed to the configured notification channels.</div>
  </div>
  <div class=fqform>
    <input id=fq-author placeholder="From (optional)" class=fqauthin>
    <input id=fq-text placeholder="Which feature would you like?" class=fqtextin onkeydown="if(event.key==='Enter')featSend()">
    <button class=fqbtn onclick=featSend()>Send request</button>
    <span class=fqout id=fq-out></span>
  </div>
  <div class=fqlist>{{LIST}}</div>
</div>
"""

ARCHIV_CARD = """<div class=card>
  <div class=chead>
    <div><span class=dtype>{{TYPE}}</span> <span class=dname>{{NAME}}</span>
      <div class=verline>archived {{WHEN}} · folder: {{FOLDER}} · {{LINKS}} links disabled · {{SESS}} visits frozen</div></div>
    <div class=chx><button class=rstb data-did="{{DID}}" onclick="restoreDoc(this)">↺ Restore</button><button class=prgb data-did="{{DID}}" data-name="{{NAME}}" onclick="purgeDoc(this)">Delete permanently …</button></div>
  </div>
</div>"""

DOC_CARD = """<div class=card id="doc-{{DID}}">
  <div class=chead>
    <div><span class=dtype>{{TYPE}}</span> <span class=dname>{{NAME}}</span><div class=srcline>{{SRC}}</div><div class=verline>{{VERLINE}}</div></div>
    <div class=cx>
      <div class=kpi><b>{{OPENED}}</b> opened &nbsp;·&nbsp; <b>{{LINKS}}</b> links</div>
      <div class=chx>{{FOLDERSEL}}<button class=xarch data-did="{{DID}}" data-name="{{NAME}}" onclick="archiveDoc(this)" title="Archive document (disables all its links)">✕ Archive</button></div>
    </div>
  </div>
  <div class=vertabs>{{VERTABS}}</div>
  {{UPDATE}}
  {{AGG}}
  <div class=tw><table><thead><tr><th>Recipient &amp; section heatmap</th><th>Last / first</th><th>Device · location</th><th>Visits</th><th>Progress</th><th>Active</th><th>Link</th></tr></thead>
  <tbody>{{ROWS}}</tbody></table></div>
  <form class=newlink onsubmit="return mkLink(event,'{{DID}}')"><input name=name placeholder="New recipient (name)" required><input name=email placeholder="Email (optional)"><input name=pw placeholder="Password (optional)"><button>+ Create link</button><span class=out></span></form>
</div>"""

DASH = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex,nofollow"><title>{{BRAND}} · Document Tracking</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#fff;--soft:#f7f9fc;--ink:#0f172a;--mut:#64748b;--line:#e8ecf3;--brand:#2563eb;--brand2:#0ea5e9;--green:#16a34a;--gold:#f59e0b;--navy:#0b1220;--radius:14px;--sh:0 1px 2px rgba(15,23,42,.04),0 10px 30px rgba(15,23,42,.06)}
*{box-sizing:border-box}
body{font-family:'Inter',system-ui,-apple-system,sans-serif;background:radial-gradient(1100px 420px at 50% -140px,#eaf1ff,#f7f9fc 62%);color:var(--ink);margin:0;font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased}
.topbar{position:sticky;top:0;z-index:30;background:rgba(255,255,255,.82);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.inner{max-width:min(1720px,96vw);margin:0 auto;padding:12px clamp(16px,3vw,34px);display:flex;align-items:center;justify-content:space-between;gap:14px}
.brand{display:flex;align-items:center;gap:11px}
.logo{width:32px;height:32px;border-radius:9px;background:linear-gradient(135deg,var(--brand),var(--brand2));display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:16px;box-shadow:0 6px 16px rgba(37,99,235,.35)}
.brand .t{font-weight:700;font-size:15px;letter-spacing:-.01em;line-height:1}
.brand .s{color:var(--mut);font-size:11.5px;margin-top:2px}
.stamp{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}
.wrap{max-width:min(1720px,96vw);margin:0 auto;padding:0 clamp(16px,3vw,34px) 90px}
.eyebrow{text-transform:uppercase;letter-spacing:.14em;font-size:11px;font-weight:700;color:var(--brand);margin-top:32px}
h1.page{font-size:27px;font-weight:800;letter-spacing:-.025em;margin:6px 0 6px}
.lead{color:var(--mut);font-size:14.5px;max-width:680px;margin-bottom:26px}
.srch{display:flex;align-items:center;gap:8px;position:relative;background:var(--bg);border:1px solid var(--line);border-radius:var(--radius);padding:6px 6px 6px 40px;box-shadow:var(--sh);margin:0 0 22px}
.srch:focus-within{border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.srchi{position:absolute;left:14px;width:17px;height:17px;color:var(--mut);pointer-events:none}
.srch input#q{flex:1;border:0;outline:0;background:transparent;font:inherit;font-size:14.5px;color:var(--ink);padding:7px 2px;min-width:0}
.srch input#q::placeholder{color:#94a3b8}
.srchb{border:0;background:var(--brand);color:#fff;font:inherit;font-weight:600;font-size:13px;padding:9px 16px;border-radius:10px;cursor:pointer}
.srchb:hover{background:#1d4ed8}
.srchx{display:grid;place-items:center;width:32px;height:32px;border-radius:9px;color:var(--mut);text-decoration:none;font-size:15px;flex:none}
.srchx:hover{background:var(--soft);color:var(--ink)}
.gen{background:var(--bg);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--sh);padding:20px;margin-bottom:30px}
.gen h2{font-size:13.5px;font-weight:700;margin:0 0 14px;letter-spacing:-.01em}
.tabs{display:inline-flex;background:var(--soft);border:1px solid var(--line);border-radius:11px;padding:3px;gap:2px;margin-bottom:15px}
.tab{padding:8px 15px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;color:var(--mut);border:0;background:transparent;font-family:inherit}
.tab.on{background:#fff;color:var(--brand);box-shadow:0 1px 4px rgba(15,23,42,.1)}
.pane{display:none;gap:9px;flex-wrap:wrap;align-items:center}.pane.on{display:flex}
.gen input{font-family:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font-size:13.5px;background:#fff;min-width:210px;color:var(--ink)}
.gen input:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.14)}
.gen input[type=file]{padding:8px 10px;background:var(--soft);min-width:0}
.btn{font-family:inherit;font-weight:600;font-size:13px;border:0;border-radius:10px;padding:10px 16px;cursor:pointer;background:var(--brand);color:#fff;box-shadow:0 6px 16px rgba(37,99,235,.28);transition:filter .15s,transform .06s}
.btn:hover{filter:brightness(1.07)}.btn:active{transform:translateY(1px)}
.out{font-size:12.5px;color:var(--mut)}
.sec-h{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--mut);margin:4px 0 14px}
.card{background:var(--bg);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--sh);padding:20px 22px;margin-bottom:20px}
.chead{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
.dtype{font-size:11px;font-weight:600;background:#eff4ff;color:var(--brand);border-radius:6px;padding:3px 9px}
.dname{font-size:17.5px;font-weight:700;letter-spacing:-.015em}
.muted{color:var(--mut)}
.srcline{color:var(--mut);font-size:12px;margin-top:4px;word-break:break-all;max-width:560px}
.verline{font-size:12px;color:var(--mut);margin-top:5px}
.kpi{text-align:right;color:var(--mut);font-size:12.5px;white-space:nowrap}
.kpi b{color:var(--brand);font-size:20px;font-weight:800;font-variant-numeric:tabular-nums}
.vertabs{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0 2px}
.vtab{font-size:12px;font-weight:600;padding:5px 12px;border:1px solid var(--line);border-radius:999px;text-decoration:none;color:var(--mut);background:#fff}
.vtab:hover{border-color:var(--brand);color:var(--brand)}
.vtab.on{background:var(--brand);color:#fff;border-color:var(--brand);box-shadow:0 3px 10px rgba(37,99,235,.28)}
.upd{margin:14px 0;padding:12px 14px;background:var(--soft);border:1px dashed #c9d6ec;border-radius:11px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.upd input{font-family:inherit;padding:8px 11px;border:1px solid var(--line);border-radius:9px;font-size:13px;background:#fff}
.upd input:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.14)}
.upd button{background:var(--ink);font-family:inherit;font-weight:600;font-size:12.5px;border:0;border-radius:9px;padding:9px 13px;color:#fff;cursor:pointer}
.heatbox{background:linear-gradient(180deg,#fbfdff,var(--soft));border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0}
.hcap{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);margin-bottom:10px;font-weight:700}
.srow{display:grid;grid-template-columns:180px 1fr 60px;gap:12px;align-items:center;margin:5px 0}
.slbl{font-size:12.5px;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sbar{background:#e5ecf8;border-radius:6px;height:17px;overflow:hidden}.sbar i{display:block;height:100%;border-radius:6px}
.ssec{font-size:11.5px;text-align:right;font-variant-numeric:tabular-nums;color:var(--mut);font-weight:700}
.tw{overflow-x:auto;margin-top:14px;border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;min-width:1010px;font-size:13px}
/* time and device columns need a min width, otherwise the text wraps word by word */
.tw th:nth-child(2),.tw td:nth-child(2){min-width:145px}
.tw th:nth-child(3),.tw td:nth-child(3){min-width:132px}
th,td{padding:12px 14px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
thead th{background:var(--soft);font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:700}
tbody tr:last-child td{border-bottom:0}tbody tr:hover td{background:#fafcff}
td.n{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
.mini{margin-top:9px;width:310px}.mini .srow{grid-template-columns:130px 1fr 48px;gap:8px;margin:3px 0}.mini .slbl{font-size:11px}.mini .sbar{height:12px}.mini .ssec{font-size:10.5px}
tr.no td{background:#fcfdff}
.lk{white-space:nowrap}.lk input{width:215px;font:11px ui-monospace,Menlo,monospace;padding:6px 9px;border:1px solid var(--line);border-radius:8px;background:var(--soft);color:var(--ink)}
.lk button{margin-left:5px;font-size:11px;font-weight:600;padding:6px 11px;background:#eff4ff;color:var(--brand);border:1px solid #dbe6fb;border-radius:8px;cursor:pointer}
.lk a.lkopen{display:inline-block;margin-left:5px;font-size:11px;font-weight:600;padding:6px 9px;background:#eff4ff;color:var(--brand);border:1px solid #dbe6fb;border-radius:8px;text-decoration:none}
.lk a.lkopen:hover{filter:brightness(.96)}
.srcline a.srca{color:var(--brand);text-decoration:none}
.srcline a.srca:hover{text-decoration:underline}
.del{margin-left:4px;font-size:12px;font-weight:600;padding:6px 9px;background:#fff;color:var(--mut);border:1px solid var(--line);border-radius:8px;cursor:pointer}.del:hover{color:#dc2626;border-color:#fecaca;background:#fff5f5}.del:disabled{opacity:.5}
.newlink{margin-top:16px;padding-top:16px;border-top:1px solid var(--line);display:flex;gap:9px;flex-wrap:wrap;align-items:center}
.newlink input{font-family:inherit;padding:9px 12px;border:1px solid var(--line);border-radius:9px;font-size:13px}
.newlink input:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.14)}
.newlink button{font-family:inherit;font-weight:600;font-size:13px;border:0;border-radius:9px;padding:9px 15px;cursor:pointer;background:var(--brand);color:#fff;box-shadow:0 6px 16px rgba(37,99,235,.25)}
.shell{display:flex;gap:26px;align-items:flex-start}
.side{width:252px;flex:none;position:sticky;top:74px;background:var(--bg);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--sh);padding:14px 10px;margin-top:32px}
.slbl{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);margin:2px 10px 8px}
.fitem{display:flex;align-items:center;gap:9px;padding:9px 11px;border-radius:9px;color:#334155;text-decoration:none;font-weight:500;font-size:13.5px}
.fitem:hover{background:var(--soft)}
.fitem.on{background:#e7eefc;color:#1d4ed8;font-weight:600}
.fitem .dot{width:8px;height:8px;border-radius:50%;flex:none}
.fitem .ftxt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fitem .n{margin-left:auto;font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
.fitem.archl{margin-top:12px;border-top:1px solid var(--line);border-radius:0 0 9px 9px;padding-top:13px;color:var(--mut)}
.newf{display:block;width:100%;text-align:left;color:var(--brand);font:600 13px 'Inter',system-ui;background:none;border:none;padding:9px 11px;cursor:pointer;border-radius:9px}
.newf:hover{background:var(--soft)}
.mainc{flex:1;min-width:0}
.headx{display:inline-flex;gap:8px;margin-left:12px;vertical-align:middle}
.hxb{font:600 12px 'Inter',system-ui;color:var(--mut);background:#fff;border:1px solid var(--line);border-radius:8px;padding:5px 10px;cursor:pointer}
.hxb:hover{color:var(--brand);border-color:var(--brand)}
.hxb.red:hover{color:#dc2626;border-color:#fecaca;background:#fff5f5}
a.hxb{display:inline-block;text-decoration:none}
.hxb.on{color:#1d4ed8;background:#e7eefc;border-color:#c7d9f8}
.fitem.everyl{color:var(--mut);font-size:13px}
.genrow{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.genrow h2{margin:0}
.genf{display:flex;align-items:center;color:var(--mut);font-size:12.5px;font-weight:600}
.cx{display:flex;flex-direction:column;gap:10px;align-items:flex-end}
.chx{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.fsel{font:500 12px 'Inter',system-ui;color:#475569;background:var(--soft);border:1px solid var(--line);border-radius:8px;padding:6px 9px;max-width:190px}
.xarch{font:600 12px 'Inter',system-ui;color:var(--mut);background:#fff;border:1px solid var(--line);border-radius:8px;padding:6px 10px;cursor:pointer;white-space:nowrap}
.xarch:hover{color:#dc2626;border-color:#fecaca;background:#fff5f5}
.rstb{font:600 12px 'Inter',system-ui;color:var(--brand);background:#eff4ff;border:1px solid #dbe6fb;border-radius:8px;padding:7px 12px;cursor:pointer}
.prgb{font:600 12px 'Inter',system-ui;color:#dc2626;background:#fff;border:1px solid #fecaca;border-radius:8px;padding:7px 12px;cursor:pointer}
.prgb:hover{background:#fff5f5}
.aub{font:700 12.5px 'Inter',system-ui;color:#1d4ed8;background:#e7eefc;border:none;border-radius:7px;padding:6px 11px;cursor:pointer;white-space:nowrap}
.aub:hover{filter:brightness(.96)}
tr.aux td{background:#f8fafd;padding:8px 16px 16px}
.auh{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);font-weight:700;margin:6px 0 4px}
.aur{display:grid;grid-template-columns:230px 150px 130px 88px 96px 92px 1fr;gap:10px;align-items:center;padding:7px 4px;border-bottom:1px dashed var(--line);font-size:12.5px;color:#334155}
.aulist .aur:last-of-type{border-bottom:none}
.aud{font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap;min-width:0}
/* relative time + "since ..." on their own line, otherwise the column overflows */
.aurel{display:block;color:var(--mut);font-weight:400;white-space:normal;font-size:11px;line-height:1.35;margin-top:1px}
.aum{color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.aup{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.aubar{height:9px;border-radius:5px;background:#e5ecf8;overflow:hidden;min-width:80px}
.aubar i{display:block;height:100%;background:linear-gradient(90deg,#93c5fd,#2563eb);border-radius:5px}
.aumore{font:600 12px 'Inter',system-ui;color:var(--brand);background:none;border:none;cursor:pointer;padding:9px 4px 0}
.aur.dim{opacity:.5}
.aur.dim:hover{opacity:.85}
.vtag{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;border-radius:5px;padding:2px 6px;margin-left:6px;white-space:nowrap}
.vtag.bot{background:#fef2f2;color:#b91c1c}
.vtag.thin{background:#f1f5f9;color:#64748b}
.filtnote{font-size:10.5px;color:var(--mut);font-weight:500;margin-top:5px;white-space:nowrap}
.aur.hassec{cursor:pointer}
.aur.hassec:hover{background:#f2f7ff;border-radius:7px}
.auchev{display:inline-block;color:var(--brand);font-size:10px;margin-right:6px;transition:transform .15s}
.aur.open .auchev{transform:rotate(90deg)}
.ausec{background:#fff;border:1px solid var(--line);border-radius:10px;padding:11px 14px;margin:2px 0 10px 18px}
.ausec .srow{grid-template-columns:150px 1fr 54px;gap:10px;margin:4px 0}
.ausec .slbl{font-size:11.5px}.ausec .sbar{height:13px}.ausec .ssec{font-size:11px}
.fq{background:linear-gradient(180deg,#fffdf7,#fff);border:1px solid #f1e4c4;border-left:4px solid var(--gold);border-radius:var(--radius);box-shadow:var(--sh);padding:18px 20px;margin-bottom:30px}
.fqhead{margin-bottom:13px}
.fqtitle{font-size:14.5px;font-weight:800;letter-spacing:-.01em;display:flex;align-items:center;gap:9px}
.fqbulb{font-size:16px}
.fqbadge{font-size:11px;font-weight:700;color:#a16207;background:#fef6e6;border:1px solid #f6e3b4;border-radius:999px;padding:2px 9px;letter-spacing:.01em}
.fqhint{color:var(--mut);font-size:12.5px;margin-top:4px;max-width:640px}
.fqform{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-bottom:6px}
.fqform input{font-family:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font-size:13.5px;background:#fff;color:var(--ink)}
.fqform input:focus{outline:none;border-color:var(--gold);box-shadow:0 0 0 3px rgba(245,158,11,.16)}
.fqauthin{min-width:150px;flex:0 0 auto}
.fqtextin{flex:1;min-width:260px}
.fqbtn{font-family:inherit;font-weight:600;font-size:13px;border:0;border-radius:10px;padding:10px 16px;cursor:pointer;background:var(--gold);color:#fff;box-shadow:0 6px 16px rgba(245,158,11,.28);transition:filter .15s,transform .06s}
.fqbtn:hover{filter:brightness(1.06)}.fqbtn:active{transform:translateY(1px)}
.fqout{font-size:12.5px;color:var(--mut)}
.fqlist{margin-top:12px;display:flex;flex-direction:column;gap:8px}
.fqempty{color:var(--mut);font-size:12.5px;padding:6px 2px}
.fqitem{display:flex;align-items:flex-start;gap:12px;padding:11px 13px;border:1px solid var(--line);border-radius:11px;background:#fff}
.fqitem.done{opacity:.62}.fqitem.done .fqtext{text-decoration:line-through}
.fqmain{flex:1;min-width:0}
.fqtext{font-size:13.5px;color:var(--ink);white-space:pre-wrap;word-break:break-word}
.fqmeta{font-size:11.5px;color:var(--mut);margin-top:3px}
.fqauth{font-weight:700;color:#475569}
.fqactions{display:flex;gap:6px;flex:none;align-items:center}
.fqstat{font:700 11px 'Inter',system-ui;border:1px solid var(--line);border-radius:7px;padding:5px 11px;cursor:pointer}
.fqstat:hover{filter:brightness(.97)}
.fqdel{font-size:12px;font-weight:600;padding:5px 9px;background:#fff;color:var(--mut);border:1px solid var(--line);border-radius:7px;cursor:pointer}
.fqdel:hover{color:#dc2626;border-color:#fecaca;background:#fff5f5}
.tbr{display:flex;align-items:center;gap:14px}
@media(max-width:960px){.shell{flex-direction:column}.side{position:static;width:auto;margin-top:20px}.aur{grid-template-columns:1fr 1fr;row-gap:4px}.aubar{display:none}}
@media(max-width:560px){.stamp{display:none}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body>
<div class=topbar><div class=inner>
  <div class=brand><div class=logo>{{LOGO}}</div><div><div class=t>{{BRAND}}</div><div class=s>Document&nbsp;Tracking</div></div></div>
  <div class=tbr><div class=stamp>Updated {{NOW}}</div></div>
</div></div>
<div class=wrap>
<div class=shell>
<aside class=side>{{SIDE}}</aside>
<main class=mainc>
<div class=eyebrow>Reader analytics</div>
<h1 class=page>{{TITLE}}</h1>
<div class=lead>{{SUB}}{{HEADX}}</div>

<form class=srch method=get action=/dash>
  <input type=hidden name=key value="{{KEY}}"><input type=hidden name=f value="{{SEL}}"><input type=hidden name=s value="{{SORT}}">
  <svg class=srchi viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round><circle cx=11 cy=11 r=7/><path d="M20 20l-3.5-3.5"/></svg>
  <input id=q name=q value="{{QVAL}}" autocomplete=off placeholder="Search documents, recipients, folders or URLs …   (press /)">
  <button class=srchb>Search</button>{{QCLR}}
</form>

{{FEEDBACK}}

<div class=gen style="{{GENSTYLE}}">
  <div class=genrow><h2>Create a new tracking link</h2><label class=genf>Folder:&nbsp;<select id=g-folder class=fsel>{{GOPTS}}</select></label></div>
  <div class=tabs><div class="tab on" data-p=url onclick="tab(this)">🔗 Wrap a link</div><div class=tab data-p=html onclick="tab(this)">📄 HTML</div><div class=tab data-p=pdf onclick="tab(this)">📕 PDF</div><div class=tab data-p=img onclick="tab(this)">🖼 Image</div></div>
  <div class="pane on" id=p-url><input id=u-name placeholder="Document name"><input id=u-url placeholder="Target URL (https://…)" style="min-width:300px"><input id=u-rec placeholder="Recipient (optional)"><input id=u-pw placeholder="Password (optional)"><button class=btn onclick=genUrl()>Create link</button><span class=out id=o-url></span></div>
  <div class=pane id=p-html><input id=h-name placeholder="Document name"><input id=h-rec placeholder="Recipient (optional)"><input id=h-pw placeholder="Password (optional)"><input type=file id=h-file accept=".html,.htm"><button class=btn onclick="genFile('html')">Upload &amp; create link</button><span class=out id=o-html></span></div>
  <div class=pane id=p-pdf><input id=d-name placeholder="Document name"><input id=d-rec placeholder="Recipient (optional)"><input id=d-pw placeholder="Password (optional)"><input type=file id=d-file accept="application/pdf,.pdf"><button class=btn onclick="genFile('pdf')">Upload &amp; create link</button><span class=out id=o-pdf></span></div>
  <div class=pane id=p-img><input id=i-name placeholder="Image name"><input id=i-rec placeholder="Recipient (optional)"><input id=i-pw placeholder="Password (optional)"><input type=file id=i-file accept="image/*,.png,.jpg,.jpeg,.webp,.gif"><button class=btn onclick="genFile('img')">Upload &amp; create link</button><span class=out id=o-img></span></div>
</div>

<div class=sec-h>Documents</div>
{{CARDS}}
</main>
</div>
</div>

<script>
var KEY="{{KEY}}",BASE="{{BASE}}";
// "/" jumps into the search field, Esc leaves it (only when not typing elsewhere)
document.addEventListener("keydown",function(e){var q=document.getElementById("q");if(!q)return;
  var t=e.target,tag=(t.tagName||"").toLowerCase();
  if(e.key==="/"&&tag!=="input"&&tag!=="textarea"&&!t.isContentEditable){e.preventDefault();q.focus();q.select();}
  else if(e.key==="Escape"&&t===q){q.blur();}});
function tab(el){document.querySelectorAll(".tab").forEach(function(t){t.classList.remove("on")});el.classList.add("on");document.querySelectorAll(".pane").forEach(function(p){p.classList.remove("on")});document.getElementById("p-"+el.dataset.p).classList.add("on");}
function cp(t,btn){var ok=false;try{if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t);ok=true;}}catch(e){}if(!ok){var ta=document.createElement("textarea");ta.value=t;ta.setAttribute("readonly","");ta.style.position="fixed";ta.style.top="-1000px";document.body.appendChild(ta);ta.focus();ta.select();try{ta.setSelectionRange(0,ta.value.length);document.execCommand("copy");}catch(e){}document.body.removeChild(ta);}if(btn){var o=btn.textContent;btn.textContent="✓ Copied";setTimeout(function(){btn.textContent=o;},1200);}}
function delLink(tok,btn){if(!confirm("Really delete this recipient link and its analytics?"))return;if(btn){btn.disabled=true;}fetch("/admin/dellink?key="+encodeURIComponent(KEY),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:tok})}).then(function(){location.reload();}).catch(function(){if(btn)btn.disabled=false;});}
function post(path,body,json){return fetch(path+"?key="+encodeURIComponent(KEY),{method:"POST",body:body,headers:json?{"Content-Type":"application/json"}:{}});}
function mkFor(doc,name,email,pw,out){post("/admin/link",JSON.stringify({doc_id:doc,name:name||"Recipient",email:email||"",pw:pw||""}),true).then(function(r){return r.json()}).then(function(d){out.innerHTML="✓ Link"+(d.pw?" 🔒":"")+" created: <input value='"+d.link+"' readonly onclick='this.select()' style='width:230px;font:11px ui-monospace,Menlo,monospace;padding:5px 7px;border:1px solid #e8ecf3;border-radius:7px'> <button class=btn onclick=\"cp('"+d.link+"',this)\">Copy</button> · reload the page to see analytics";});}
function gFolder(){var s=document.getElementById("g-folder");return s?s.value:"";}
function genUrl(){var n=document.getElementById("u-name").value,u=document.getElementById("u-url").value,r=document.getElementById("u-rec").value,pw=document.getElementById("u-pw").value,o=document.getElementById("o-url");if(!n||!u){o.textContent="Name + URL required";return;}o.textContent="…";post("/admin/doc",JSON.stringify({name:n,target:u,folder_id:gFolder()}),true).then(function(x){return x.json()}).then(function(d){mkFor(d.doc_id,r||n,"",pw,o);});}
function genFile(type){var pfx={pdf:"d",html:"h",img:"i"}[type]||"h";var n=document.getElementById(pfx+"-name").value,r=document.getElementById(pfx+"-rec").value,pw=document.getElementById(pfx+"-pw").value,f=document.getElementById(pfx+"-file").files[0],o=document.getElementById("o-"+type);if(!n||!f){o.textContent="Name + file required";return;}o.textContent="uploading …";f.arrayBuffer().then(function(buf){return fetch("/admin/upload?key="+encodeURIComponent(KEY),{method:"POST",headers:{"X-Doc-Type":type,"X-Doc-Name":encodeURIComponent(n),"X-Filename":encodeURIComponent(f.name),"X-Folder-Id":encodeURIComponent(gFolder())},body:buf});}).then(function(x){return x.json()}).then(function(d){mkFor(d.doc_id,r||n,"",pw,o);});}
function mkLink(ev,doc){ev.preventDefault();var f=ev.target;mkFor(doc,f.name.value,f.email.value,f.pw.value,f.querySelector(".out"));return false;}
function updateDoc(did,type){var o=document.getElementById("ou-"+did);o.textContent="…";
if(type==="url"){var t=document.getElementById("uu-"+did).value;if(!t){o.textContent="URL required";return;}
fetch("/admin/update?key="+encodeURIComponent(KEY),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({doc_id:did,target:t})}).then(function(r){return r.json()}).then(function(d){o.textContent="✓ now version "+d.version;setTimeout(function(){location.reload();},800);});}
else{var f=document.getElementById("uf-"+did).files[0];if(!f){o.textContent="File required";return;}o.textContent="uploading …";
f.arrayBuffer().then(function(buf){return fetch("/admin/update?key="+encodeURIComponent(KEY),{method:"POST",headers:{"X-Doc-Id":did,"X-Doc-Type":type,"X-Filename":encodeURIComponent(f.name)},body:buf});}).then(function(r){return r.json()}).then(function(d){o.textContent="✓ now version "+d.version;setTimeout(function(){location.reload();},800);});}}
function toggleAu(tok){var r=document.getElementById("au-"+tok);if(r)r.hidden=!r.hidden;}
function toggleSec(hid,row){var s=document.getElementById("sec-"+hid);if(!s)return;s.hidden=!s.hidden;if(row)row.classList.toggle("open",!s.hidden);}
function moreVisits(tok,ver,off,btn){btn.disabled=true;fetch("/api/sessions?key="+encodeURIComponent(KEY)+"&token="+encodeURIComponent(tok)+"&ver="+ver+"&offset="+off+"&limit=200").then(function(r){return r.text()}).then(function(h){btn.insertAdjacentHTML("beforebegin",h);btn.remove();}).catch(function(){btn.disabled=false;});}
function archiveDoc(b){if(!confirm("Move \""+b.dataset.name+"\" to the archive?\n\nAll recipient links will be disabled (visitors see 404 \"Page not available\"). Statistics are kept and the document can be restored at any time."))return;b.disabled=true;post("/admin/archivedoc",JSON.stringify({doc_id:b.dataset.did}),true).then(function(){location.reload();}).catch(function(){b.disabled=false;});}
function restoreDoc(b){b.disabled=true;post("/admin/restoredoc",JSON.stringify({doc_id:b.dataset.did}),true).then(function(){location.reload();}).catch(function(){b.disabled=false;});}
function purgeDoc(b){var nm=b.dataset.name;if(!confirm("PERMANENTLY delete \""+nm+"\", including all links, visits, versions and files?"))return;if(!confirm("Are you sure? This can NOT be undone."))return;b.disabled=true;post("/admin/purgedoc",JSON.stringify({doc_id:b.dataset.did,confirm:b.dataset.did}),true).then(function(){location.reload();}).catch(function(){b.disabled=false;});}
function setFolder(did,fid){post("/admin/folder",JSON.stringify({action:"assign",doc_id:did,folder_id:fid}),true).then(function(){location.reload();});}
function newFolder(){var n=prompt("Name of the new folder:");if(!n)return;post("/admin/folder",JSON.stringify({action:"create",name:n}),true).then(function(r){if(r.status===409){alert("Folder already exists.");return;}location.reload();});}
function renFolder(b){var n=prompt("Rename folder:",b.dataset.name);if(!n||n===b.dataset.name)return;post("/admin/folder",JSON.stringify({action:"rename",id:b.dataset.fid,name:n}),true).then(function(r){if(r.status===409){alert("Folder already exists.");return;}location.reload();});}
function delFolder(b){if(!confirm("Dissolve folder \""+b.dataset.name+"\"?\n\nThe documents are kept and move to \"No folder\"."))return;post("/admin/folder",JSON.stringify({action:"delete",id:b.dataset.fid}),true).then(function(){location.href="/dash?key="+encodeURIComponent(KEY)+"&f=all";});}
function featSend(){var t=document.getElementById("fq-text"),a=document.getElementById("fq-author"),o=document.getElementById("fq-out");var txt=(t.value||"").trim();if(!txt){o.textContent="Please enter a request.";return;}o.textContent="…";post("/admin/feature",JSON.stringify({text:txt,author:(a.value||"").trim()}),true).then(function(r){return r.json()}).then(function(d){if(d&&d.ok){t.value="";o.textContent="✓ Sent";setTimeout(function(){location.reload();},600);}else{o.textContent="Error.";}}).catch(function(){o.textContent="Error.";});}
function featStatus(id,status){post("/admin/feature/status",JSON.stringify({id:id,status:status}),true).then(function(){location.reload();});}
function featDel(id){if(!confirm("Delete this feature request?"))return;post("/admin/feature/status",JSON.stringify({id:id,delete:true}),true).then(function(){location.reload();});}
</script>
</body></html>"""

# ---------------------------------------------------------------- CLI ---------
def main():
    init()
    a = sys.argv[1:]
    if a and a[0] == "add":
        if len(a) < 4 or not doc_row(a[3]):
            sys.exit('usage: tracker_service.py add "Name" email doc_id   (see: tracker_service.py docs)')
        print(BASE + "/r/" + add_link(a[3], a[1], a[2])); return
    if a and a[0] == "docs":
        with db() as c:
            for d in c.execute("SELECT * FROM documents ORDER BY created"):
                print("%-10s %-6s %s" % (d["id"], d["type"], d["name"]));
        return
    if a and a[0] == "list":
        with db() as c:
            for r in c.execute("SELECT l.*, d.name dn FROM links l LEFT JOIN documents d ON d.id=l.doc_id ORDER BY l.created"):
                print("%-24s [%s]  %s/r/%s  %s" % (r["name"], r["dn"], BASE, r["token"], r["email"] or ""));
        return
    srv = ThreadingHTTPServer((BIND, PORT), H)
    print("doc-tracker on %s:%d db=%s docs=%s ntfy=%s email=%s telegram=%s opens=%s forms=%s" % (
        BIND, PORT, DB, DOCDIR, "on" if NTFY else "-", "on" if (SMTP["HOST"] and ALERT_TO) else "-",
        "on" if (TG_TOKEN and TG_CHAT) else "-", NOTIFY_OPENS, ",".join(FORMS) or "-"), flush=True)
    if not KEY: print("WARNING: TRACKER_KEY is empty, the dashboard and admin API are open to anyone.", flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    main()
