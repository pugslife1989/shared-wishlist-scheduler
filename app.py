import os
import sqlite3
from datetime import datetime
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
            visit_date TEXT NOT NULL,
            visited INTEGER DEFAULT 0,
            photo TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/")
def index():
    conn = get_db()
    places = conn.execute("SELECT * FROM places ORDER BY visited ASC, visit_date ASC").fetchall()
    conn.close()
    return render_template("index.html", places=places)


@app.route("/add", methods=["POST"])
def add_place():
    conn = get_db()
    conn.execute(
        "INSERT INTO places (title, url, category, visit_date) VALUES (?, ?, ?, ?)",
        (request.form["title"], request.form["url"], request.form["category"], request.form["visit_date"])
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
TRIGGER:-PT12H
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
