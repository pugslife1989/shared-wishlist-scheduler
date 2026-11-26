"""Places to Go v2 — multi-user, self-hosted.

Flask + SQLite + gunicorn. Same app as v1, with accounts:

- Users sign up with a username + password (scrypt hash, never plain
  text). No email required. Login/logout, session stays logged in.
- Each user has their own lists (e.g. Family, Friends, Couple).
  A list owner can share a list with another user by username;
  shared users can add/edit places in that list, but cannot delete
  the list or change sharing.
- A user only ever sees their own lists plus lists shared with them.
  Every place query is scoped to list_id + a membership check.
- Each list has its own calendar token (random, stored in the DB),
  feed at /calendar/<token>.ics, with VALARM 3-day / 1-day alerts.
  No global CALENDAR_TOKEN env var, no shared PLACES_PASSWORD.

Migration from v1: if an old places.db has places with no list_id,
the first user to sign up gets a default list "Places to Go" and
all those places are assigned to it. Nothing is deleted.

Run:  python app.py   (dev)   or   gunicorn --bind 0.0.0.0:5000 app:app
DB:   $DB_PATH, default data/places.db (mount /data in Docker)
Env:  SECRET_KEY (required in production — see README/.env.example)
"""
import csv
import html
import io
import os
import re
import secrets
import sqlite3
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

from flask import Flask, g, jsonify, redirect, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from PIL import Image, UnidentifiedImageError

app = Flask(__name__, static_folder="static", template_folder="templates")
_secret = os.environ.get("SECRET_KEY", "")
if not _secret:
    # Dev fallback only. In production set SECRET_KEY in .env — the
    # README says so, and the app logs a warning on every start.
    _secret = "places-to-go-dev-only-change-me"
    print("WARNING: SECRET_KEY is not set — using an insecure dev default. "
          "Set SECRET_KEY in .env before exposing this app.", flush=True)
app.secret_key = _secret

app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "places.db"))
PHOTOS_DIR = os.environ.get(
    "PHOTOS_DIR",
    os.path.join(os.path.dirname(__file__), "data", "photos"),
)
MAX_NAME = 200
MAX_PHOTO_DIM = 1600
MAX_PHOTO_BYTES = 2 * 1024 * 1024
CATEGORIES = ("Dating", "Family")
TYPES = ("Food", "Cafe", "Place", "Activity")
STATUSES = ("want", "visited")
MAX_USERNAME = 50


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            calendar_token TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS list_members (
            list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'member',
            PRIMARY KEY (list_id, user_id)
        )"""
    )
    # v1 places table, plus list_id (added in v2). added_by is kept
    # for old rows (never dropped); it is no longer set or shown.
    db.execute(
        """CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER REFERENCES lists(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            link TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Dating',
            type TEXT NOT NULL DEFAULT 'Place',
            added_by TEXT NOT NULL DEFAULT 'Me',
            date_added TEXT NOT NULL,
            remind_on TEXT,
            visit_date TEXT,
            status TEXT NOT NULL DEFAULT 'want',
            visit_count INTEGER NOT NULL DEFAULT 0,
            image_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    cols = [r[1] for r in db.execute("PRAGMA table_info(places)").fetchall()]
    if "list_id" not in cols:
        db.execute("ALTER TABLE places ADD COLUMN list_id INTEGER REFERENCES lists(id) ON DELETE CASCADE")
    if "visit_count" not in cols:
        db.execute("ALTER TABLE places ADD COLUMN visit_count INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE places SET visit_count=1 WHERE status='visited'")
        db.execute("UPDATE places SET visit_count=0 WHERE status!='visited'")
    if "visit_date" not in cols:
        db.execute("ALTER TABLE places ADD COLUMN visit_date TEXT")
        db.execute("UPDATE places SET visit_date=remind_on WHERE visit_date IS NULL")
    elif "remind_on" in cols:
        db.execute("UPDATE places SET visit_date=remind_on WHERE visit_date IS NULL AND remind_on IS NOT NULL")
        db.execute("UPDATE places SET remind_on=visit_date WHERE remind_on IS NULL AND visit_date IS NOT NULL")
    db.execute(
        """CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            place_id INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            taken_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    photo_cols = [r[1] for r in db.execute("PRAGMA table_info(photos)").fetchall()]
    if "taken_date" not in photo_cols:
        db.execute("ALTER TABLE photos ADD COLUMN taken_date TEXT")
    db.commit()
    # Legacy migration (v1 -> v2): if users already exist and there
    # are places with no list, assign them now. If no user exists
    # yet, this runs again after the first signup (see
    # migrate_legacy_places). Nothing is ever deleted here.
    _migrate_legacy_locked(db)


def _migrate_legacy_locked(db):
    """Assign v1 places (list_id NULL) to a default list owned by the
    first registered user. Returns the list id or None."""
    orphan = db.execute("SELECT COUNT(*) c FROM places WHERE list_id IS NULL").fetchone()["c"]
    if not orphan:
        return None
    first = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    if first is None:
        return None
    owner_id = first["id"]
    existing = db.execute(
        "SELECT id FROM lists WHERE owner_id=? AND name='Places to Go' ORDER BY id LIMIT 1",
        (owner_id,)).fetchone()
    if existing:
        list_id = existing["id"]
    else:
        cur = db.execute(
            "INSERT INTO lists (name, owner_id, calendar_token) VALUES (?, ?, ?)",
            ("Places to Go", owner_id, secrets.token_hex(16)))
        list_id = cur.lastrowid
        db.execute("INSERT OR IGNORE INTO list_members (list_id, user_id, role) VALUES (?, ?, 'owner')",
                   (list_id, owner_id))
    db.execute("UPDATE places SET list_id=? WHERE list_id IS NULL", (list_id,))
    db.commit()
    return list_id


def migrate_legacy_places(owner_id):
    """Run after the first signup: v1 places with no list become
    that user's default 'Places to Go' list."""
    db = get_db()
    return _migrate_legacy_locked(db)


# ------------------------------------------------------------------- auth
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT id, username, created_at FROM users WHERE id=?", (uid,)).fetchone()


def valid_username(name):
    name = (name or "").strip()
    if not (1 <= len(name) <= MAX_USERNAME):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.\- ]+", name):
        return None
    return name


def valid_password(pw):
    return isinstance(pw, str) and len(pw) >= 4


@app.before_request
def require_login():
    if session.get("user_id"):
        return None
    # Health check stays open; auth pages are open; calendar feeds
    # are open — they do their own per-list token check (Apple
    # Calendar cannot log in). Everything else needs a login.
    if request.endpoint in ("healthz", "login", "signup", "logout", "calendar_feed", "calendar_feed_legacy", "static", "sw"):
        return None
    if request.path in ("/login", "/signup", "/logout", "/sw.js", "/manifest.webmanifest") or request.path.startswith("/calendar/") or request.path.startswith("/static/"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Please log in first."}), 401
    return redirect(url_for("login"))


AUTH_STYLE = """body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:#faf7f2;color:#1c1917;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px}
main{background:#fff;border:1px solid #e7e0d6;border-radius:18px;padding:28px;width:100%;max-width:360px}
h1{margin:0 0 6px;font-size:22px}p{margin:0 0 18px;color:#78716c;font-size:14px}
input{width:100%;padding:12px;border:1px solid #e7e0d6;border-radius:12px;font-size:16px;box-sizing:border-box;margin-bottom:10px}
button{width:100%;margin-top:6px;background:#e4572e;color:#fff;border:0;border-radius:12px;padding:14px;font-size:16px;font-weight:700;cursor:pointer}
.err{color:#b91c1c;background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:10px;margin-top:12px;font-size:14px}
a{color:#c7431f}"""


def auth_page(title, subtitle, action, button, alt_link):
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#e4572e">
<link rel="manifest" href="/static/manifest.webmanifest">
<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/icon-180-v2.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<title>Places to Go — {title}</title><style>{AUTH_STYLE}</style></head>
<body><main><h1>Places to Go</h1><p>{subtitle}</p>
<form method="post"><input name="username" placeholder="Username" required autofocus maxlength="50">
<input type="password" name="password" placeholder="Password" required>
<button type="submit">{button}</button>{{error}}</form>
<p style="margin-top:16px">{alt_link}</p></main></body></html>"""


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user_id"):
        return redirect(url_for("index"))
    page = auth_page("Sign up", "Create your account — pick a username and password. No email needed.",
                     "/signup", "Create account", 'Already have an account? <a href="/login">Log in</a>')
    if request.method == "POST":
        form = request.form or {}
        username = valid_username(form.get("username"))
        password = form.get("password") or ""
        db = get_db()
        err = None
        if not username:
            err = "Pick a username (letters, numbers, spaces, _ . -), up to 50 characters."
        elif not valid_password(password):
            err = "Password must be at least 4 characters."
        elif db.execute("SELECT 1 FROM users WHERE lower(username)=lower(?)", (username,)).fetchone():
            err = "That username is already taken — pick another one."
        if err:
            return page.replace("{error}", f'<div class="err">{html.escape(err)}</div>'), 400
        cur = db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         (username, generate_password_hash(password)))
        db.commit()
        user_id = cur.lastrowid
        migrate_legacy_places(user_id)
        session["user_id"] = user_id
        session["username"] = username
        session.permanent = True
        return redirect(url_for("index"))
    return page.replace("{error}", "")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))
    page = auth_page("Log in", "Log in to see your lists.",
                     "/login", "Log in", 'No account yet? <a href="/signup">Sign up</a>')
    if request.method == "POST":
        form = request.form or {}
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE lower(username)=lower(?)", (username,)).fetchone()
        if row is None or not check_password_hash(row["password_hash"], password):
            return page.replace("{error}", '<div class="err">Wrong username or password — try again.</div>'), 401
        session["user_id"] = row["id"]
        session["username"] = row["username"]
        session.permanent = True
        migrate_legacy_places(row["id"])
        return redirect(url_for("index"))
    return page.replace("{error}", "")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------- list access
def user_lists(db, user_id):
    """All lists a user can see: their own + lists shared with them."""
    return db.execute(
        """SELECT l.id, l.name, l.owner_id, l.calendar_token, l.created_at,
                  u.username AS owner_name,
                  lm.role AS my_role
           FROM lists l
           JOIN list_members lm ON lm.list_id = l.id AND lm.user_id = ?
           JOIN users u ON u.id = l.owner_id
           ORDER BY l.name COLLATE NOCASE""",
        (user_id,)).fetchall()


def list_for_user(db, list_id, user_id):
    """A list row + the caller's role, or None if not a member."""
    return db.execute(
        """SELECT l.*, u.username AS owner_name, lm.role AS my_role
           FROM lists l
           JOIN list_members lm ON lm.list_id = l.id AND lm.user_id = ?
           JOIN users u ON u.id = l.owner_id
           WHERE l.id = ?""",
        (user_id, list_id)).fetchone()


def place_for_user(db, place_id, user_id):
    """A place row, only if its list is visible to this user."""
    return db.execute(
        """SELECT p.* FROM places p
           JOIN list_members lm ON lm.list_id = p.list_id AND lm.user_id = ?
           WHERE p.id = ?""",
        (user_id, place_id)).fetchone()


def new_calendar_token(db):
    while True:
        token = secrets.token_hex(16)
        if not db.execute("SELECT 1 FROM lists WHERE calendar_token=?", (token,)).fetchone():
            return token


with app.app_context():
    init_db()


# ------------------------------------------------------------------ helpers
def photos_for_place(db, place_id):
    rows = db.execute(
        "SELECT id, place_id, filename, taken_date, created_at FROM photos"
        " WHERE place_id=? ORDER BY id",
        (place_id,),
    ).fetchall()
    out = []
    for r in rows:
        taken = (r["taken_date"] or "").strip() if "taken_date" in r.keys() else ""
        added = (r["created_at"] or "")[:10]
        out.append(
            {"id": r["id"], "place_id": r["place_id"], "filename": r["filename"],
             "taken_date": taken or None,
             "display_date": taken or added,
             "url": f"/photos/{r['filename']}", "created_at": r["created_at"]}
        )
    return out


def row_to_dict(row, db=None):
    d = dict(row)
    try:
        d["visit_count"] = int(d.get("visit_count") or 0)
    except (TypeError, ValueError):
        d["visit_count"] = 1 if d.get("status") == "visited" else 0
    d["visit_date"] = d.get("visit_date") or d.get("remind_on")
    d["remind_on"] = d["visit_date"]
    if db is not None:
        d["photos"] = photos_for_place(db, row["id"])
    return d


def list_to_dict(row, db, user_id):
    members = db.execute(
        """SELECT u.id, u.username, lm.role FROM list_members lm
           JOIN users u ON u.id = lm.user_id WHERE lm.list_id = ?
           ORDER BY lm.role DESC, u.username COLLATE NOCASE""",
        (row["id"],)).fetchall()
    return {
        "id": row["id"], "name": row["name"], "owner_id": row["owner_id"],
        "owner_name": row["owner_name"], "my_role": row["my_role"],
        "created_at": row["created_at"],
        "members": [{"id": m["id"], "username": m["username"], "role": m["role"]} for m in members],
    }


def valid_visit_date(value):
    if not value or not str(value).strip():
        return None
    try:
        date.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return str(value).strip()


def find_date_clash(db, list_id, visit_date, exclude_id=None):
    """One place per day, per list: another place in the SAME list
    already on this date, or None. A place keeping its own date is
    not a clash. Other lists are not checked — two lists can both
    have a place on the same day."""
    q = ("SELECT id, name, COALESCE(visit_date, remind_on) AS d FROM places "
         "WHERE list_id=? AND COALESCE(visit_date, remind_on)=?")
    args = [list_id, visit_date]
    if exclude_id is not None:
        q += " AND id != ?"
        args.append(exclude_id)
    return db.execute(q + " LIMIT 1", args).fetchone()


def clash_error(row, visit_date):
    return f"You already have {row['name']} on {visit_date} — pick a different Date to visit."


def shorten_name(name):
    name = re.sub(r"\s+", " ", (name or "").strip())
    if len(name) <= MAX_NAME:
        return name
    cut = name[:MAX_NAME].rsplit(" ", 1)[0].strip()
    return cut or name[:MAX_NAME].strip()


def exif_taken_date(img):
    try:
        exif = img.getexif()
    except Exception:
        return None
    if not exif:
        return None
    for tag in (36867, 36868, 306):
        raw = exif.get(tag)
        if not raw:
            continue
        try:
            return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").date().isoformat()
        except Exception:
            continue
    return None


def save_photo(uploaded_file):
    raw = uploaded_file.read()
    if not raw:
        raise ValueError("That file was empty — pick a photo and try again.")
    if len(raw) > app.config["MAX_CONTENT_LENGTH"]:
        raise ValueError("That photo is too big — pick one under 10 MB.")
    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValueError("That file doesn't look like a photo — try a JPG, PNG or WebP image.")
    taken_date = exif_taken_date(img)
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA").convert("RGB") if img.mode == "P" else img.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((MAX_PHOTO_DIM, MAX_PHOTO_DIM), Image.LANCZOS)
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.jpg"
    quality, size = 82, img.size
    while True:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        if len(buf.getvalue()) <= MAX_PHOTO_BYTES or (quality <= 60 and size[0] <= 800):
            with open(os.path.join(PHOTOS_DIR, filename), "wb") as f:
                f.write(buf.getvalue())
            return filename, taken_date
        if quality > 60:
            quality -= 10
        else:
            size = (max(1, size[0] // 2), max(1, size[1] // 2))
            img = img.resize(size, Image.LANCZOS)


def fetch_preview(url):
    if not url or not re.match(r"^https?://", url, re.I):
        return {"title": "", "image_url": ""}
    req = urllib.request.Request(
        url,
        headers={"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
                 "Accept": "text/html,application/xhtml+xml"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if "text/html" not in resp.headers.get("Content-Type", ""):
                return {"title": "", "image_url": ""}
            raw = resp.read(300_000).decode("utf-8", errors="ignore")
    except Exception:
        return {"title": "", "image_url": ""}

    def meta(prop):
        m = re.search(
            r'<meta[^>]+(?:property|name)=["\'](?:og|twitter):' + prop +
            r'["\'][^>]+content=["\']([^"\']+)["\']', raw, re.I) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og|twitter):' +
            prop + r'["\']', raw, re.I)
        return html.unescape(m.group(1).strip()) if m else ""

    title = meta("title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else ""
    image = meta("image") or meta("image:src")
    if image:
        image = urllib.parse.urljoin(url, image)
    return {"title": shorten_name(title), "image_url": image}


# ------------------------------------------------------------------- routes
@app.errorhandler(413)
def too_large(_exc):
    return jsonify({"error": "That photo is too big — pick one under 10 MB."}), 413


@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


@app.route("/sw.js")
def sw():
    # Service worker must be served from / (not /static/) so its
    # scope covers the whole app.
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    u = current_user()
    return jsonify({"id": u["id"], "username": u["username"]})


@app.route("/api/preview", methods=["POST"])
def preview():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(fetch_preview(data.get("url", "")))


# -------------------------------------------------------------------- lists
@app.route("/api/lists", methods=["GET"])
def get_lists():
    db = get_db()
    rows = user_lists(db, session["user_id"])
    return jsonify([list_to_dict(r, db, session["user_id"]) for r in rows])


@app.route("/api/lists", methods=["POST"])
def create_list():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name or len(name) > 100:
        return jsonify({"error": "Give the list a name (up to 100 characters)."}), 400
    db = get_db()
    cur = db.execute("INSERT INTO lists (name, owner_id, calendar_token) VALUES (?, ?, ?)",
                     (name, session["user_id"], new_calendar_token(db)))
    list_id = cur.lastrowid
    db.execute("INSERT INTO list_members (list_id, user_id, role) VALUES (?, ?, 'owner')",
               (list_id, session["user_id"]))
    db.commit()
    row = list_for_user(db, list_id, session["user_id"])
    return jsonify(list_to_dict(row, db, session["user_id"])), 201


@app.route("/api/lists/<int:lid>", methods=["PATCH"])
def rename_list(lid):
    db = get_db()
    lst = list_for_user(db, lid, session["user_id"])
    if lst is None:
        return jsonify({"error": "Not found"}), 404
    if lst["my_role"] != "owner":
        return jsonify({"error": "Only the list owner can rename it."}), 403
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name or len(name) > 100:
        return jsonify({"error": "Give the list a name (up to 100 characters)."}), 400
    db.execute("UPDATE lists SET name=? WHERE id=?", (name, lid))
    db.commit()
    return jsonify(list_to_dict(list_for_user(db, lid, session["user_id"]), db, session["user_id"]))


@app.route("/api/lists/<int:lid>", methods=["DELETE"])
def delete_list(lid):
    db = get_db()
    lst = list_for_user(db, lid, session["user_id"])
    if lst is None:
        return jsonify({"error": "Not found"}), 404
    if lst["my_role"] != "owner":
        return jsonify({"error": "Only the list owner can delete it."}), 403
    # Delete its photos from disk first, then the list (places and
    # members cascade in the DB).
    for row in db.execute("SELECT id FROM places WHERE list_id=?", (lid,)).fetchall():
        for photo in photos_for_place(db, row["id"]):
            try:
                os.remove(os.path.join(PHOTOS_DIR, photo["filename"].split("/")[-1]))
            except OSError:
                pass
    db.execute("DELETE FROM lists WHERE id=?", (lid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/lists/<int:lid>/share", methods=["POST"])
def share_list(lid):
    db = get_db()
    lst = list_for_user(db, lid, session["user_id"])
    if lst is None:
        return jsonify({"error": "Not found"}), 404
    if lst["my_role"] != "owner":
        return jsonify({"error": "Only the list owner can change sharing."}), 403
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    target = db.execute("SELECT id, username FROM users WHERE lower(username)=lower(?)",
                        (username,)).fetchone()
    if target is None:
        return jsonify({"error": f"No user called '{username}' — check the username and try again."}), 404
    if target["id"] == session["user_id"]:
        return jsonify({"error": "That's you — you already own this list."}), 400
    db.execute("INSERT OR IGNORE INTO list_members (list_id, user_id, role) VALUES (?, ?, 'member')",
               (lid, target["id"]))
    db.commit()
    return jsonify(list_to_dict(list_for_user(db, lid, session["user_id"]), db, session["user_id"]))


@app.route("/api/lists/<int:lid>/share/<int:uid>", methods=["DELETE"])
def unshare_list(lid, uid):
    db = get_db()
    lst = list_for_user(db, lid, session["user_id"])
    if lst is None:
        return jsonify({"error": "Not found"}), 404
    if lst["my_role"] != "owner":
        return jsonify({"error": "Only the list owner can change sharing."}), 403
    if uid == lst["owner_id"]:
        return jsonify({"error": "You can't remove the owner."}), 400
    db.execute("DELETE FROM list_members WHERE list_id=? AND user_id=?", (lid, uid))
    db.commit()
    return jsonify(list_to_dict(list_for_user(db, lid, session["user_id"]), db, session["user_id"]))


@app.route("/api/lists/<int:lid>/calendar/regenerate", methods=["POST"])
def regenerate_calendar_token(lid):
    db = get_db()
    lst = list_for_user(db, lid, session["user_id"])
    if lst is None:
        return jsonify({"error": "Not found"}), 404
    if lst["my_role"] != "owner":
        return jsonify({"error": "Only the list owner can regenerate the calendar link."}), 403
    db.execute("UPDATE lists SET calendar_token=? WHERE id=?", (new_calendar_token(db), lid))
    db.commit()
    return jsonify(list_to_dict(list_for_user(db, lid, session["user_id"]), db, session["user_id"]))


@app.route("/api/lists/<int:lid>/calendar-feed")
def list_calendar_feed_info(lid):
    db = get_db()
    lst = list_for_user(db, lid, session["user_id"])
    if lst is None:
        return jsonify({"error": "Not found"}), 404
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    base = f"{scheme}://{request.host}"
    token = lst["calendar_token"]
    return jsonify({
        "configured": True,
        "feed_url": f"{base}/calendar/{token}.ics",
        "webcal_url": f"webcal://{request.host}/calendar/{token}.ics",
        "list_name": lst["name"],
    })


# ------------------------------------------------------------------- places
@app.route("/api/lists/<int:lid>/places", methods=["GET"])
def list_places(lid):
    db = get_db()
    if list_for_user(db, lid, session["user_id"]) is None:
        return jsonify({"error": "Not found"}), 404
    rows = db.execute("SELECT * FROM places WHERE list_id=? ORDER BY created_at DESC, id DESC",
                      (lid,)).fetchall()
    return jsonify([row_to_dict(r, db) for r in rows])


@app.route("/api/lists/<int:lid>/places", methods=["POST"])
def add_place(lid):
    db = get_db()
    if list_for_user(db, lid, session["user_id"]) is None:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    name = shorten_name(data.get("name") or data.get("title") or "")
    link = (data.get("link") or "").strip()
    if not link or not re.match(r"^https?://", link, re.I):
        return jsonify({"error": "Add a link starting with http:// or https://"}), 400
    if not name:
        prev = fetch_preview(link)
        name = prev["title"] or urllib.parse.urlparse(link).netloc
    if len(name) > MAX_NAME:
        return jsonify({"error": f"Name must be {MAX_NAME} characters or fewer"}), 400
    category = data.get("category") if data.get("category") in CATEGORIES else "Dating"
    ptype = data.get("type") if data.get("type") in TYPES else "Place"
    visit_date = valid_visit_date(data.get("visit_date") or data.get("remind_on"))
    if not visit_date:
        return jsonify({"error": "Please pick a Date to visit — it is required."}), 400
    image_url = (data.get("image_url") or "").strip() or None
    if image_url is None:
        image_url = fetch_preview(link)["image_url"] or None
    clash = find_date_clash(db, lid, visit_date)
    if clash is not None:
        return jsonify({"error": clash_error(clash, visit_date)}), 400
    cur = db.execute(
        "INSERT INTO places (list_id, name, link, category, type, date_added,"
        " remind_on, visit_date, status, visit_count, image_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (lid, name, link, category, ptype, date.today().isoformat(),
         visit_date, visit_date, "want", 0, image_url),
    )
    db.commit()
    row = db.execute("SELECT * FROM places WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(row_to_dict(row, db)), 201


@app.route("/api/places/<int:pid>", methods=["PATCH"])
def update_place(pid):
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()
    row = place_for_user(db, pid, session["user_id"])
    if row is None:
        return jsonify({"error": "Not found"}), 404
    fields, values = [], []
    if data.get("status") == "visited" and row["status"] == "want":
        try:
            current = int(row["visit_count"] or 0)
        except (TypeError, ValueError, KeyError):
            current = 1 if row["status"] == "visited" else 0
        fields.append("visit_count=?"); values.append(current + 1)
    for key, allowed in (("category", CATEGORIES), ("type", TYPES),
                         ("status", STATUSES)):
        if key in data and data[key] in allowed:
            fields.append(f"{key}=?"); values.append(data[key])
    if "name" in data:
        name = shorten_name(data["name"])
        if not name:
            return jsonify({"error": "Name cannot be empty"}), 400
        fields.append("name=?"); values.append(name)
    if "link" in data:
        fields.append("link=?"); values.append(data["link"])
    if "visit_date" in data or "remind_on" in data:
        raw = data.get("visit_date") if "visit_date" in data else data.get("remind_on")
        v = valid_visit_date(raw)
        if not v:
            return jsonify({"error": "Please pick a Date to visit — it is required."}), 400
        clash = find_date_clash(db, row["list_id"], v, exclude_id=pid)
        if clash is not None:
            return jsonify({"error": clash_error(clash, v)}), 400
        fields.append("visit_date=?"); values.append(v)
        fields.append("remind_on=?"); values.append(v)
    if "image_url" in data:
        fields.append("image_url=?"); values.append(data["image_url"] or None)
    if fields:
        values.append(pid)
        db.execute(f"UPDATE places SET {', '.join(fields)} WHERE id=?", values)
        db.commit()
    row = db.execute("SELECT * FROM places WHERE id=?", (pid,)).fetchone()
    return jsonify(row_to_dict(row, db))


@app.route("/api/places/<int:pid>", methods=["DELETE"])
def delete_place(pid):
    db = get_db()
    row = place_for_user(db, pid, session["user_id"])
    if row is None:
        return jsonify({"error": "Not found"}), 404
    for photo in photos_for_place(db, pid):
        try:
            os.remove(os.path.join(PHOTOS_DIR, photo["filename"].split("/")[-1]))
        except OSError:
            pass
    db.execute("DELETE FROM photos WHERE place_id=?", (pid,))
    db.execute("DELETE FROM places WHERE id=?", (pid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/lists/<int:lid>/export.csv")
def export_csv(lid):
    db = get_db()
    if list_for_user(db, lid, session["user_id"]) is None:
        return jsonify({"error": "Not found"}), 404
    rows = db.execute("SELECT * FROM places WHERE list_id=? ORDER BY created_at DESC", (lid,)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Place name", "Link", "Category", "Type", "Date added", "Date to visit", "Status"])
    for r in rows:
        w.writerow([r["name"], r["link"], r["category"], r["type"],
                    r["date_added"], (r["visit_date"] if "visit_date" in r.keys() else None) or r["remind_on"] or "",
                    "Want to go" if r["status"] == "want" else "Visited"])
    return app.response_class(buf.getvalue(), mimetype="text/csv",
                              headers={"Content-Disposition": "attachment; filename=places-to-go.csv"})


# ------------------------------------------------------------------- photos
@app.route("/photos/<path:filename>")
def serve_photo(filename):
    if not re.fullmatch(r"[0-9a-f]{32}\.jpg", filename or ""):
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(PHOTOS_DIR, filename, mimetype="image/jpeg")


@app.route("/api/places/<int:pid>/photos", methods=["GET"])
def list_photos(pid):
    db = get_db()
    if place_for_user(db, pid, session["user_id"]) is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(photos_for_place(db, pid))


@app.route("/api/places/<int:pid>/photos", methods=["POST"])
def upload_photo(pid):
    db = get_db()
    row = place_for_user(db, pid, session["user_id"])
    if row is None:
        return jsonify({"error": "Not found"}), 404
    if row["status"] != "visited":
        return jsonify({"error": "Mark this place Visited first — photos are for places you've been to."}), 403
    uploaded = request.files.get("photo")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Choose a photo to upload."}), 400
    try:
        filename, taken_date = save_photo(uploaded)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    cur = db.execute(
        "INSERT INTO photos (place_id, filename, taken_date) VALUES (?, ?, ?)",
        (pid, filename, taken_date))
    db.commit()
    photo = db.execute("SELECT * FROM photos WHERE id=?", (cur.lastrowid,)).fetchone()
    display_date = taken_date or (photo["created_at"] or "")[:10]
    return jsonify({"id": photo["id"], "place_id": pid,
                    "url": f"/photos/{filename}",
                    "taken_date": taken_date,
                    "display_date": display_date,
                    "created_at": photo["created_at"]}), 201


@app.route("/api/places/<int:pid>/photos/<int:photo_id>", methods=["DELETE"])
def delete_photo(pid, photo_id):
    db = get_db()
    if place_for_user(db, pid, session["user_id"]) is None:
        return jsonify({"error": "Not found"}), 404
    photo = db.execute(
        "SELECT * FROM photos WHERE id=? AND place_id=?", (photo_id, pid)).fetchone()
    if photo is None:
        return jsonify({"error": "Not found"}), 404
    try:
        os.remove(os.path.join(PHOTOS_DIR, photo["filename"].split("/")[-1]))
    except OSError:
        pass
    db.execute("DELETE FROM photos WHERE id=?", (photo_id,))
    db.commit()
    return jsonify({"ok": True})


# ------------------------------------------------------- iPhone Calendar feed
# Per-list feed: /calendar/<token>.ics — no login, the token IS the
# access. Each list's token is random, stored in the DB, generated
# on list creation, and regenerated by the owner from the UI.
# Anyone with the full feed URL can see that list's upcoming visit
# dates/names/links, so treat it like a password.
def _ics_escape(text):
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _ics_fold(line):
    if len(line) <= 75:
        return line
    out, rest = line[:75], line[75:]
    while rest:
        out += "\r\n " + rest[:74]
        rest = rest[74:]
    return out


def build_ics(rows):
    """Timed events (9am-10am Asia/Singapore) for every Want-to-go place
    in one list — not all-day events, so alerts fire at 9am, not midnight.
    UID is stable per place (place-<id>@places-to-go), so a date
    change updates the same event instead of duplicating it.
    Each event carries VALARM TRIGGER:-P3D and TRIGGER:-P1D, which
    now fire at 9am 3 days / 1 day before the visit."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Places to Go//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Places to Go",
        "X-WR-TIMEZONE:Asia/Singapore",
        "BEGIN:VTIMEZONE",
        "TZID:Asia/Singapore",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "TZNAME:SGT",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for r in rows:
        visit = valid_visit_date(r["visit_date"] if "visit_date" in r.keys() else None) or \
            valid_visit_date(r["remind_on"] if "remind_on" in r.keys() else None)
        if not visit:
            continue
        start = date.fromisoformat(visit)
        desc = f"{r['category']} · {r['type']}\n{r['link']}"
        for line in (
            "BEGIN:VEVENT",
            f"UID:place-{r['id']}@places-to-go",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID=Asia/Singapore:{start.strftime('%Y%m%d')}T090000",
            f"DTEND;TZID=Asia/Singapore:{start.strftime('%Y%m%d')}T100000",
            f"SUMMARY:{_ics_escape(r['name'])}",
            f"DESCRIPTION:{_ics_escape(desc)}",
            f"URL:{r['link']}",
            "BEGIN:VALARM",
            "TRIGGER:-P3D",
            "ACTION:DISPLAY",
            f"DESCRIPTION:Places to Go: {_ics_escape(r['name'])} in 3 days",
            "END:VALARM",
            "BEGIN:VALARM",
            "TRIGGER:-P1D",
            "ACTION:DISPLAY",
            f"DESCRIPTION:Places to Go: {_ics_escape(r['name'])} in 1 day",
            "END:VALARM",
            "END:VEVENT",
        ):
            lines.append(_ics_fold(line))
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@app.route("/calendar/<token>.ics")
def calendar_feed(token):
    db = get_db()
    lst = db.execute("SELECT id, name FROM lists WHERE calendar_token=?", (token,)).fetchone()
    if lst is None:
        return app.response_class("Wrong or missing calendar token.", status=401, mimetype="text/plain")
    rows = db.execute(
        "SELECT * FROM places WHERE list_id=? AND status='want' AND "
        "COALESCE(visit_date, remind_on) IS NOT NULL AND "
        "TRIM(COALESCE(visit_date, remind_on)) != '' "
        "ORDER BY COALESCE(visit_date, remind_on), name",
        (lst["id"],)).fetchall()
    return app.response_class(
        build_ics(rows), mimetype="text/calendar",
        headers={"Content-Disposition": 'inline; filename="places-to-go.ics"',
                 "Cache-Control": "no-cache"})


@app.route("/calendar.ics")
def calendar_feed_legacy():
    # v1 feed URL (/calendar.ics?token=...) no longer exists in v2:
    # tokens are per-list now. Point callers at the per-list feed.
    return app.response_class(
        "This calendar feed moved in v2: each list has its own feed at "
        "/calendar/<token>.ics — open the list in the app, go to "
        "'Sync to iPhone Calendar', and use the link shown there.",
        status=410, mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
