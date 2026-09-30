import os
import secrets
import sqlite3
from io import BytesIO
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
SCHEMA = """
CREATE TABLE IF NOT EXISTS election (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    is_open INTEGER NOT NULL DEFAULT 0 CHECK (is_open IN (0, 1))
);
INSERT OR IGNORE INTO election (id, is_open) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO app_settings (key, value) VALUES ('school_name', 'School Poll');
UPDATE app_settings SET value = 'School Poll'
WHERE key = 'school_name' AND value = 'Campus Poll';

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE RESTRICT,
    name TEXT NOT NULL COLLATE NOCASE,
    photo_path TEXT,
    UNIQUE (position_id, name)
);

CREATE TABLE IF NOT EXISTS votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ballot_id TEXT NOT NULL,
    position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE RESTRICT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ballot_id, position_id)
);

CREATE INDEX IF NOT EXISTS idx_votes_candidate ON votes(candidate_id);
"""


def create_app(test_config=None):
    app = Flask(__name__)
    admin_password = (test_config or {}).get("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD")
    if not admin_password or len(admin_password) < 12:
        raise RuntimeError("Set ADMIN_PASSWORD to a password of at least 12 characters.")
    secret_key = (test_config or {}).get("SECRET_KEY") or os.environ.get("SECRET_KEY")
    if not secret_key or len(secret_key) < 32:
        raise RuntimeError("Set SECRET_KEY to a random value of at least 32 characters.")

    app.config.update(
        SECRET_KEY=secret_key,
        DATABASE=os.environ.get("POLLING_DATABASE", str(ROOT / "polling.db")),
        UPLOAD_FOLDER=str(ROOT / "static" / "uploads" / "candidates"),
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,
        ADMIN_PASSWORD_HASH=generate_password_hash(admin_password),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)

    csrf = CSRFProtect()
    csrf.init_app(app)
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[],
        storage_uri="memory://",
    )
    limiter.init_app(app)

    def connect_db():
        connection = sqlite3.connect(app.config["DATABASE"], timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize_db():
        Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
        connection = connect_db()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            candidate_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(candidates)")
            }
            if "photo_path" not in candidate_columns:
                connection.execute("ALTER TABLE candidates ADD COLUMN photo_path TEXT")
        finally:
            connection.close()

    initialize_db()

    def get_db():
        if "db" not in g:
            g.db = connect_db()
        return g.db

    @app.context_processor
    def inject_school_name():
        row = get_db().execute(
            "SELECT value FROM app_settings WHERE key = 'school_name'"
        ).fetchone()
        return {"school_name": row["value"] if row else "School Poll"}

    def store_candidate_photo(upload):
        if not upload or not upload.filename:
            return None
        contents = upload.stream.read(5 * 1024 * 1024 + 1)
        if len(contents) > 5 * 1024 * 1024:
            raise ValueError("Candidate photos must be 5 MB or smaller.")
        try:
            with Image.open(BytesIO(contents)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("Use a JPEG, PNG, or WebP image.")
                if image.width * image.height > 40_000_000:
                    raise ValueError("Image dimensions are too large.")
                image.verify()
            with Image.open(BytesIO(contents)) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                image = ImageOps.fit(image, (512, 512), method=Image.Resampling.LANCZOS)
                output = BytesIO()
                image.save(output, format="JPEG", quality=86, optimize=True)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ValueError("That file is not a valid supported image.") from error

        filename = f"{secrets.token_hex(16)}.jpg"
        upload_folder = Path(app.config["UPLOAD_FOLDER"])
        upload_folder.mkdir(parents=True, exist_ok=True)
        (upload_folder / filename).write_bytes(output.getvalue())
        return filename

    def delete_candidate_photos(filenames):
        upload_folder = Path(app.config["UPLOAD_FOLDER"]).resolve()
        all_deleted = True
        for filename in filenames:
            if not filename:
                continue
            photo_path = (upload_folder / filename).resolve()
            if photo_path.parent != upload_folder or photo_path.suffix != ".jpg":
                app.logger.error("Refusing to delete candidate photo outside upload directory")
                all_deleted = False
                continue
            try:
                photo_path.unlink(missing_ok=True)
            except OSError:
                app.logger.exception("Could not delete candidate photo %s", filename)
                all_deleted = False
        return all_deleted

    @app.get("/candidate-photos/<filename>")
    def candidate_photo(filename):
        if Path(filename).name != filename or not filename.endswith(".jpg"):
            abort(404)
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    @app.teardown_appcontext
    def close_db(_error=None):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("is_admin"):
                return redirect(url_for("admin_login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def election_open(connection=None):
        connection = connection or get_db()
        row = connection.execute("SELECT is_open FROM election WHERE id = 1").fetchone()
        return bool(row["is_open"])

    def active_positions(connection=None):
        connection = connection or get_db()
        return connection.execute(
            "SELECT id, name FROM positions ORDER BY id"
        ).fetchall()

    def results_data(connection=None):
        connection = connection or get_db()
        rows = connection.execute(
            """
            SELECT p.id AS position_id, p.name AS position_name,
                   c.id AS candidate_id, c.name AS candidate_name, c.photo_path,
                   COUNT(v.id) AS votes
            FROM positions p
            JOIN candidates c ON c.position_id = p.id
            LEFT JOIN votes v ON v.candidate_id = c.id
            GROUP BY p.id, p.name, c.id, c.name, c.photo_path
            ORDER BY p.id, c.id
            """
        ).fetchall()
        grouped = []
        for row in rows:
            if not grouped or grouped[-1]["id"] != row["position_id"]:
                grouped.append({"id": row["position_id"], "name": row["position_name"], "candidates": []})
            grouped[-1]["candidates"].append(
                {
                    "id": row["candidate_id"],
                    "name": row["candidate_name"],
                    "photo_path": row["photo_path"],
                    "votes": row["votes"],
                }
            )
        for position in grouped:
            total = sum(candidate["votes"] for candidate in position["candidates"])
            position["total"] = total
            for candidate in position["candidates"]:
                candidate["percent"] = round(candidate["votes"] * 100 / total) if total else 0
        return grouped

    @app.get("/")
    def index():
        return render_template("index.html", is_open=election_open())

    @app.route("/vote", methods=["GET", "POST"])
    def vote():
        if request.method == "GET":
            db = get_db()
            positions = active_positions(db)
            if not election_open(db):
                return render_template("vote.html", is_open=False, positions=[])
            if not positions:
                return render_template("vote.html", is_open=True, positions=[])
            candidates = db.execute(
                "SELECT id, position_id, name, photo_path FROM candidates ORDER BY position_id, id"
            ).fetchall()
            by_position = {}
            for candidate in candidates:
                by_position.setdefault(candidate["position_id"], []).append(candidate)
            return render_template(
                "vote.html",
                is_open=True,
                positions=positions,
                candidates=by_position,
            )

        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            if not election_open(db):
                db.rollback()
                flash("Voting is currently closed.", "error")
                return redirect(url_for("index"))

            positions = active_positions(db)
            if not positions:
                db.rollback()
                flash("Voting is not configured yet.", "error")
                return redirect(url_for("index"))

            selections = []
            for position in positions:
                candidate_id = request.form.get(f"position_{position['id']}", type=int)
                candidate = db.execute(
                    "SELECT id FROM candidates WHERE id = ? AND position_id = ?",
                    (candidate_id, position["id"]),
                ).fetchone()
                if candidate is None:
                    db.rollback()
                    flash("Choose one valid candidate for every position.", "error")
                    return redirect(url_for("vote"))
                selections.append((position["id"], candidate["id"]))

            ballot_id = secrets.token_urlsafe(24)
            db.executemany(
                "INSERT INTO votes (ballot_id, position_id, candidate_id) VALUES (?, ?, ?)",
                [(ballot_id, position_id, candidate_id) for position_id, candidate_id in selections],
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            app.logger.exception("Could not save an anonymous ballot")
            abort(500)
        return render_template("thanks.html")

    @app.get("/admin/login")
    def admin_login():
        if session.get("is_admin"):
            return redirect(url_for("admin_dashboard"))
        return render_template("admin_login.html")

    @app.post("/admin/login")
    @limiter.limit("5 per minute")
    def admin_login_submit():
        password = request.form.get("password", "")
        if check_password_hash(app.config["ADMIN_PASSWORD_HASH"], password):
            session.clear()
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Invalid administrator password.", "error")
        return render_template("admin_login.html"), 401

    @app.post("/admin/logout")
    @login_required
    def admin_logout():
        session.clear()
        return redirect(url_for("index"))

    @app.get("/admin")
    @login_required
    def admin_dashboard():
        db = get_db()
        positions = db.execute(
            """
            SELECT p.id, p.name, COUNT(v.id) AS vote_count
            FROM positions p LEFT JOIN votes v ON v.position_id = p.id
            GROUP BY p.id, p.name ORDER BY p.id
            """
        ).fetchall()
        candidates = db.execute(
            """
            SELECT c.id, c.name, c.position_id, c.photo_path, p.name AS position_name,
                   COUNT(v.id) AS vote_count
            FROM candidates c
            JOIN positions p ON p.id = c.position_id
            LEFT JOIN votes v ON v.candidate_id = c.id
            GROUP BY c.id, c.name, c.position_id, p.name
            ORDER BY p.id, c.id
            """
        ).fetchall()
        total_ballots = db.execute("SELECT COUNT(DISTINCT ballot_id) FROM votes").fetchone()[0]
        return render_template(
            "admin.html",
            is_open=election_open(db),
            positions=positions,
            candidates=candidates,
            results=results_data(db),
            total_ballots=total_ballots,
        )

    @app.post("/admin/school-name")
    @login_required
    def update_school_name():
        name = request.form.get("school_name", "").strip()
        if not name or len(name) > 100:
            flash("School name must be between 1 and 100 characters.", "error")
            return redirect(url_for("admin_dashboard"))
        try:
            get_db().execute(
                """
                INSERT INTO app_settings (key, value) VALUES ('school_name', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (name,),
            )
            get_db().commit()
        except sqlite3.Error:
            get_db().rollback()
            app.logger.exception("Could not update school name")
            abort(500)
        flash("School name updated.", "success")
        return redirect(url_for("admin_dashboard"))

    def require_closed_election():
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        if election_open(db):
            db.rollback()
            flash("Close voting before changing the ballot.", "error")
            return redirect(url_for("admin_dashboard"))
        return None

    @app.post("/admin/election/toggle")
    @login_required
    def toggle_election():
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            opened = not election_open(db)
            if opened:
                positions = active_positions(db)
                positions_without_candidates = db.execute(
                    """
                    SELECT p.id
                    FROM positions p
                    LEFT JOIN candidates c ON c.position_id = p.id
                    GROUP BY p.id
                    HAVING COUNT(c.id) = 0
                    """
                ).fetchone()
                if not positions or positions_without_candidates:
                    db.rollback()
                    flash("Add at least one candidate to every position before opening voting.", "error")
                    return redirect(url_for("admin_dashboard"))
            db.execute("UPDATE election SET is_open = ? WHERE id = 1", (int(opened),))
            db.commit()
        except sqlite3.Error:
            db.rollback()
            app.logger.exception("Could not update election state")
            abort(500)
        flash("Voting is now open." if opened else "Voting is now closed.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/positions")
    @login_required
    def add_position():
        closed_response = require_closed_election()
        if closed_response:
            return closed_response
        db = get_db()
        name = request.form.get("name", "").strip()
        if not name or len(name) > 100:
            db.rollback()
            flash("Position names must be between 1 and 100 characters.", "error")
            return redirect(url_for("admin_dashboard"))
        try:
            db.execute("INSERT INTO positions (name) VALUES (?)", (name,))
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            flash("A position with that name already exists.", "error")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/candidates")
    @login_required
    def add_candidate():
        closed_response = require_closed_election()
        if closed_response:
            return closed_response
        db = get_db()
        name = request.form.get("name", "").strip()
        position_id = request.form.get("position_id", type=int)
        if not name or len(name) > 100 or position_id is None:
            db.rollback()
            flash("Enter a candidate name and select a position.", "error")
            return redirect(url_for("admin_dashboard"))
        if db.execute("SELECT 1 FROM positions WHERE id = ?", (position_id,)).fetchone() is None:
            db.rollback()
            flash("Select a valid position.", "error")
            return redirect(url_for("admin_dashboard"))
        try:
            photo_path = store_candidate_photo(request.files.get("photo"))
        except ValueError as error:
            db.rollback()
            flash(str(error), "error")
            return redirect(url_for("admin_dashboard"))
        try:
            db.execute(
                "INSERT INTO candidates (position_id, name, photo_path) VALUES (?, ?, ?)",
                (position_id, name, photo_path),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            delete_candidate_photos([photo_path])
            flash("That candidate is already listed for this position.", "error")
        except sqlite3.Error:
            db.rollback()
            delete_candidate_photos([photo_path])
            app.logger.exception("Could not add candidate")
            abort(500)
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/candidates/<int:candidate_id>/photo")
    @login_required
    def update_candidate_photo(candidate_id):
        closed_response = require_closed_election()
        if closed_response:
            return closed_response
        db = get_db()
        candidate = db.execute(
            "SELECT photo_path FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if candidate is None:
            db.rollback()
            abort(404)
        try:
            photo_path = store_candidate_photo(request.files.get("photo"))
            if photo_path is None:
                db.rollback()
                flash("Choose an image file to upload.", "error")
                return redirect(url_for("admin_dashboard"))
            db.execute(
                "UPDATE candidates SET photo_path = ? WHERE id = ?",
                (photo_path, candidate_id),
            )
            db.commit()
        except ValueError as error:
            db.rollback()
            flash(str(error), "error")
            return redirect(url_for("admin_dashboard"))
        except sqlite3.Error:
            db.rollback()
            delete_candidate_photos([photo_path])
            app.logger.exception("Could not update candidate photo")
            abort(500)
        if not delete_candidate_photos([candidate["photo_path"]]):
            flash("Photo updated, but the previous image could not be removed. Check the server log.", "error")
        else:
            flash("Candidate photo updated.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/candidates/<int:candidate_id>/photo/delete")
    @login_required
    def delete_candidate_photo(candidate_id):
        closed_response = require_closed_election()
        if closed_response:
            return closed_response
        db = get_db()
        candidate = db.execute(
            "SELECT photo_path FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if candidate is None:
            db.rollback()
            abort(404)
        db.execute("UPDATE candidates SET photo_path = NULL WHERE id = ?", (candidate_id,))
        db.commit()
        if not delete_candidate_photos([candidate["photo_path"]]):
            flash("Photo removed from the ballot, but its file could not be deleted. Check the server log.", "error")
        else:
            flash("Candidate photo removed.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/positions/<int:position_id>/delete")
    @login_required
    def delete_position(position_id):
        closed_response = require_closed_election()
        if closed_response:
            return closed_response
        db = get_db()
        if db.execute("SELECT 1 FROM positions WHERE id = ?", (position_id,)).fetchone() is None:
            db.rollback()
            abort(404)
        if db.execute("SELECT 1 FROM votes WHERE position_id = ? LIMIT 1", (position_id,)).fetchone():
            db.rollback()
            flash("A position with recorded votes cannot be deleted.", "error")
            return redirect(url_for("admin_dashboard"))
        photo_filenames = [
            row["photo_path"]
            for row in db.execute(
                "SELECT photo_path FROM candidates WHERE position_id = ? AND photo_path IS NOT NULL",
                (position_id,),
            )
        ]
        db.execute("DELETE FROM candidates WHERE position_id = ?", (position_id,))
        db.execute("DELETE FROM positions WHERE id = ?", (position_id,))
        db.commit()
        if not delete_candidate_photos(photo_filenames):
            flash("Position deleted, but some candidate photos could not be removed. Check the server log.", "error")
            return redirect(url_for("admin_dashboard"))
        flash("Position and its candidates deleted.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/candidates/<int:candidate_id>/delete")
    @login_required
    def delete_candidate(candidate_id):
        closed_response = require_closed_election()
        if closed_response:
            return closed_response
        db = get_db()
        if db.execute("SELECT 1 FROM candidates WHERE id = ?", (candidate_id,)).fetchone() is None:
            db.rollback()
            abort(404)
        if db.execute("SELECT 1 FROM votes WHERE candidate_id = ? LIMIT 1", (candidate_id,)).fetchone():
            db.rollback()
            flash("A candidate with recorded votes cannot be deleted.", "error")
            return redirect(url_for("admin_dashboard"))
        candidate = db.execute(
            "SELECT photo_path FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        db.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
        db.commit()
        if not delete_candidate_photos([candidate["photo_path"]]):
            flash("Candidate deleted, but its photo could not be removed. Check the server log.", "error")
            return redirect(url_for("admin_dashboard"))
        flash("Candidate deleted.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/election/reset")
    @login_required
    def reset_election():
        reset_type = request.form.get("reset_type")
        if reset_type not in {"votes", "candidates", "all"}:
            flash("Choose a valid reset option.", "error")
            return redirect(url_for("admin_dashboard"))

        db = get_db()
        photo_filenames = []
        try:
            db.execute("BEGIN IMMEDIATE")
            photo_filenames = [
                row["photo_path"]
                for row in db.execute("SELECT photo_path FROM candidates WHERE photo_path IS NOT NULL")
            ]
            db.execute("UPDATE election SET is_open = 0 WHERE id = 1")
            db.execute("DELETE FROM votes")
            if reset_type in {"candidates", "all"}:
                db.execute("DELETE FROM candidates")
            if reset_type == "all":
                db.execute("DELETE FROM positions")
                db.execute(
                    """
                    INSERT INTO app_settings (key, value) VALUES ('school_name', 'School Poll')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            app.logger.exception("Could not reset election data")
            abort(500)

        if reset_type in {"candidates", "all"}:
            if not delete_candidate_photos(photo_filenames):
                flash("Election data was reset, but some candidate photos could not be removed. Check the server log.", "error")
                return redirect(url_for("admin_dashboard"))
        messages = {
            "votes": "Votes cleared. School name, positions, and candidates were kept.",
            "candidates": "Votes and candidates cleared. School name and positions were kept.",
            "all": "All election data cleared. The school name was reset to School Poll.",
        }
        flash(messages[reset_type], "success")
        return redirect(url_for("admin_dashboard"))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        threaded=True,
    )
