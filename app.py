import os
import re
import sqlite3
import urllib.request
from datetime import datetime, date
from werkzeug.utils import secure_filename
from flask import Flask, request, redirect, render_template, send_from_directory, Response

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "places.db")
PHOTOS_DIR = os.environ.get("PHOTOS_DIR", "photos")


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            category TEXT NOT NULL,
            place_type TEXT DEFAULT 'Food',
            visit_date TEXT NOT NULL,
            created_date TEXT,
            thumbnail TEXT,
            visited INTEGER DEFAULT 0,
            photo TEXT
        )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(places)").fetchall()]
    if "place_type" not in cols:
        conn.execute("ALTER TABLE places ADD COLUMN place_type TEXT DEFAULT 'Food'")
    if "created_date" not in cols:
        conn.execute("ALTER TABLE places ADD COLUMN created_date TEXT")
    if "thumbnail" not in cols:
        conn.execute("ALTER TABLE places ADD COLUMN thumbnail TEXT")
    conn.commit()
    conn.close()


init_db()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_og_image(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if not match:
            match = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
        if match:
            return match.group(1)
    except Exception:
        pass
    return ""


@app.route("/")
def index():
    conn = get_db()
    rows = conn.execute("SELECT * FROM places ORDER BY visited ASC, visit_date ASC").fetchall()
    conn.close()

    today = date.today()
    places = []
    reminders = []

    for r in rows:
        item = dict(r)
        try:
            v_date = datetime.strptime(item["visit_date"], "%Y-%m-%d").date()
            diff = (v_date - today).days
            item["days_left"] = diff
            if not item["visited"] and 0 <= diff <= 3:
                reminders.append(item)
        except Exception:
            item["days_left"] = None
        places.append(item)

    return render_template("index.html", places=places, reminders=reminders)


@app.route("/add", methods=["POST"])
def add_place():
    url = request.form["url"].strip()
    title = request.form["title"].strip()
    thumb = fetch_og_image(url)
    today_str = date.today().isoformat()

    conn = get_db()
    conn.execute(
        "INSERT INTO places (title, url, category, place_type, visit_date, created_date, thumbnail) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (title, url, request.form["category"], request.form.get("place_type", "Food"), request.form["visit_date"], today_str, thumb)
    )
    conn.commit()
    conn.close()
    return redirect("/")


@app.route("/edit/<int:place_id>", methods=["POST"])
def edit_place(place_id):
    conn = get_db()
    conn.execute(
        "UPDATE places SET title = ?, url = ?, category = ?, place_type = ?, visit_date = ? WHERE id = ?",
        (request.form["title"], request.form["url"], request.form["category"], request.form.get("place_type", "Food"), request.form["visit_date"], place_id)
    )
    conn.commit()
    conn.close()
    return redirect("/")


@app.route("/went/<int:place_id>", methods=["POST"])
def mark_went(place_id):
    filename = None
    file = request.files.get("photo")
    if file and file.filename:
        safe_name = f"{place_id}_{int(datetime.now().timestamp())}_{secure_filename(file.filename)}"
        file.save(os.path.join(PHOTOS_DIR, safe_name))
        filename = safe_name

    conn = get_db()
    conn.execute("UPDATE places SET visited = 1, photo = COALESCE(?, photo) WHERE id = ?", (filename, place_id))
    conn.commit()
    conn.close()
    return redirect("/")


@app.route("/calendar/<int:place_id>")
def calendar_ics(place_id):
    conn = get_db()
    p = conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
    conn.close()
    date_str = p["visit_date"].replace("-", "")
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART;VALUE=DATE:{date_str}
SUMMARY:[{p['category']}] {p['title']}
DESCRIPTION:Saved link: {p['url']}
BEGIN:VALARM
TRIGGER:-PT24H
ACTION:DISPLAY
DESCRIPTION:Reminder: {p['title']}
END:VALARM
END:VEVENT
END:VCALENDAR"""
    return Response(ics, mimetype="text/calendar", headers={"Content-Disposition": f"attachment; filename=outing_{place_id}.ics"})


@app.route("/photos/<path:filename>")
def serve_photo(filename):
    return send_from_directory(PHOTOS_DIR, filename)


@app.route("/delete/<int:place_id>", methods=["POST"])
def delete_place(place_id):
    conn = get_db()
    conn.execute("DELETE FROM places WHERE id = ?", (place_id,))
    conn.commit()
    conn.close()
    return redirect("/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
