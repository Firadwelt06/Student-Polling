import os
import tempfile
import unittest
import csv
import hashlib
import hmac
import secrets
import sqlite3
from io import BytesIO, StringIO

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password-123")

_original_database = os.environ.get("POLLING_DATABASE")
_bootstrap_temp_dir = tempfile.TemporaryDirectory()
os.environ["POLLING_DATABASE"] = os.path.join(_bootstrap_temp_dir.name, "bootstrap.db")
from app import create_app
from PIL import Image
if _original_database is None:
    os.environ.pop("POLLING_DATABASE", None)
else:
    os.environ["POLLING_DATABASE"] = _original_database


class PollingAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret-key-with-at-least-32-characters",
                "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
                "UPLOAD_FOLDER": os.path.join(self.temp_dir.name, "uploads"),
                "ADMIN_PASSWORD": "test-admin-password-123",
                "WTF_CSRF_ENABLED": False,
            }
        )
        self.client = self.app.test_client()
        self.codes = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def sign_in(self):
        response = self.client.post("/admin/login", data={"password": "test-admin-password-123"})
        self.assertEqual(response.status_code, 302)
        code_response = self.client.post("/admin/voting-codes", data={"count": "5"})
        self.assertEqual(code_response.status_code, 200)
        self.codes = [
            row["voting_code"]
            for row in csv.DictReader(StringIO(code_response.data.decode("utf-8")))
        ]
        return response

    def cast_ballot(self, selections):
        data = dict(selections)
        data["voting_code"] = self.codes.pop(0)
        return self.client.post("/vote", data=data)

    def test_admin_builds_ballot_opens_and_views_anonymous_results(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})
        self.client.post("/admin/candidates", data={"name": "Jordan", "position_id": "1"})
        self.client.post("/admin/election/toggle")

        ballot = self.client.get("/vote")
        self.assertEqual(ballot.status_code, 200)
        self.assertIn(b"Alex", ballot.data)
        self.cast_ballot({"position_1": "2"})

        dashboard = self.client.get("/admin")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"1</strong>", dashboard.data)
        self.assertIn(b"Jordan", dashboard.data)
        self.assertNotIn(b"Results by position", dashboard.data)
        results = self.client.get("/admin/results")
        self.assertEqual(results.status_code, 200)
        self.assertIn(b"Results by position", results.data)
        self.assertIn(b"Jordan", results.data)
        with self.app.app_context():
            from flask import current_app
            import sqlite3

            connection = sqlite3.connect(current_app.config["DATABASE"])
            columns = {row[1] for row in connection.execute("PRAGMA table_info(votes)")}
            connection.close()
        self.assertNotIn("voter_name", columns)
        self.assertNotIn("student_id", columns)

    def test_voting_codes_are_single_use_and_stored_only_as_hashes(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})
        self.client.post("/admin/election/toggle")
        code = self.codes[0]

        accepted = self.client.post("/vote", data={"position_1": "1", "voting_code": code})
        self.assertEqual(accepted.status_code, 200)
        reused = self.client.post("/vote", data={"position_1": "1", "voting_code": code})
        self.assertEqual(reused.status_code, 302)
        self.assertIn(b"already been used", self.client.get(reused.location).data)

        with self.app.app_context():
            from flask import current_app
            import sqlite3

            connection = sqlite3.connect(current_app.config["DATABASE"])
            used_code = connection.execute(
                "SELECT code_hash, used FROM voter_codes WHERE used = 1"
            ).fetchone()
            ballot_count = connection.execute(
                "SELECT COUNT(DISTINCT ballot_id) FROM votes"
            ).fetchone()[0]
            connection.close()
        self.assertNotEqual(used_code[0], code)
        self.assertEqual(used_code[1], 1)
        self.assertEqual(ballot_count, 1)

    def test_admin_can_replace_unused_codes_and_download_csv_once(self):
        self.sign_in()
        response = self.client.post(
            "/admin/voting-codes",
            data={"count": "2", "replace_unused": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        rows = list(csv.DictReader(StringIO(response.data.decode("utf-8"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["voting_code"] for row in rows}), 2)
        for row in rows:
            code = row["voting_code"]
            self.assertRegex(code, r"^[A-HJ-NP-Z]{5}$")
            self.assertNotIn("I", code)
            self.assertNotIn("O", code)
        dashboard = self.client.get("/admin")
        self.assertIn(b"Codes available: <strong>2</strong>", dashboard.data)

    def test_previously_issued_long_voting_code_remains_valid(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})
        legacy_code = secrets.token_hex(8).upper()
        digest = hmac.new(
            self.app.config["SECRET_KEY"].encode("utf-8"),
            legacy_code.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        with self.app.app_context():
            from flask import current_app

            connection = sqlite3.connect(current_app.config["DATABASE"])
            connection.execute(
                "INSERT INTO voter_codes (code_hash) VALUES (?)", (digest,)
            )
            connection.commit()
            connection.close()

        self.client.post("/admin/election/toggle")
        response = self.client.post(
            "/vote", data={"position_1": "1", "voting_code": legacy_code}
        )
        self.assertEqual(response.status_code, 200)

    def test_vote_post_attempts_are_rate_limited_without_throttling_ballot_page(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})
        self.client.post("/admin/election/toggle")

        self.assertEqual(self.client.get("/vote").status_code, 200)
        responses = [
            self.client.post("/vote", data={"position_1": "1", "voting_code": "AAAAA"})
            for _ in range(11)
        ]
        self.assertEqual(responses[-1].status_code, 429)
        self.assertEqual(self.client.get("/vote").status_code, 200)

    def test_election_cannot_open_without_unused_codes(self):
        self.client.post("/admin/login", data={"password": "test-admin-password-123"})
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})

        self.client.post("/admin/election/toggle")
        dashboard = self.client.get("/admin")
        self.assertIn(b"Generate at least one unused voting code", dashboard.data)
        self.assertIn(b">Closed</strong>", dashboard.data)

        self.client.post("/admin/voting-codes", data={"count": "1"})
        self.client.post("/admin/election/toggle")
        dashboard = self.client.get("/admin")
        self.assertIn(b">Open</strong>", dashboard.data)

    def test_live_tally_refreshes_and_renaming_keeps_votes(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})
        self.client.post("/admin/candidates", data={"name": "Jordan", "position_id": "1"})
        self.client.post("/admin/election/toggle")
        self.cast_ballot({"position_1": "1"})

        live_tally = self.client.get("/admin/results/live")
        self.assertIn(b"ANONYMOUS BALLOTS", live_tally.data)
        self.assertIn(b">1</strong>", live_tally.data)
        self.assertIn(b"1 total", live_tally.data)
        self.assertIn(b"Alex", live_tally.data)

        self.client.post("/admin/election/toggle")
        self.client.post("/admin/positions/1", data={"name": "Student President"})
        self.client.post("/admin/candidates/1", data={"name": "Alex Rivera"})
        updated_tally = self.client.get("/admin/results/live")
        self.assertIn(b"Student President", updated_tally.data)
        self.assertIn(b"Alex Rivera", updated_tally.data)
        self.assertIn(b">1</strong>", updated_tally.data)
        self.assertIn(b"1 total", updated_tally.data)

    def test_fullscreen_slideshow_route_is_available(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})
        self.client.post("/admin/candidates", data={"name": "Jordan", "position_id": "1"})
        self.client.post("/admin/election/toggle")
        self.cast_ballot({"position_1": "1"})

        slideshow = self.client.get("/admin/results/slideshow")
        self.assertEqual(slideshow.status_code, 200)
        self.assertIn(b"LIVE TALLY", slideshow.data)
        self.assertIn(b"Position 1", slideshow.data)
        self.assertIn(b"Alex", slideshow.data)

    def test_live_tally_requires_admin_login(self):
        page = self.client.get("/admin/results")
        live_data = self.client.get("/admin/results/live")
        self.assertEqual(page.status_code, 302)
        self.assertIn("/admin/login", page.location)
        self.assertEqual(live_data.status_code, 302)
        self.assertIn("/admin/login", live_data.location)

    def test_admin_can_move_candidate_while_voting_is_closed(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/positions", data={"name": "Treasurer"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})

        response = self.client.post("/admin/candidates/1/move", data={"position_id": "2"})
        self.assertEqual(response.status_code, 302)
        dashboard = self.client.get("/admin")
        self.assertIn(b"Alex", dashboard.data)
        self.assertIn(b"Treasurer", dashboard.data)
        with self.app.app_context():
            from flask import current_app
            import sqlite3

            connection = sqlite3.connect(current_app.config["DATABASE"])
            position_id = connection.execute(
                "SELECT position_id FROM candidates WHERE id = 1"
            ).fetchone()[0]
            connection.close()
        self.assertEqual(position_id, 2)

    def test_admin_cannot_move_candidate_with_recorded_votes(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/positions", data={"name": "Treasurer"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})
        self.client.post("/admin/candidates", data={"name": "Jordan", "position_id": "2"})
        self.client.post("/admin/election/toggle")
        self.cast_ballot({"position_1": "1", "position_2": "2"})
        self.client.post("/admin/election/toggle")

        response = self.client.post("/admin/candidates/1/move", data={"position_id": "2"})
        self.assertEqual(response.status_code, 302)
        dashboard = self.client.get("/admin")
        self.assertIn(b"candidate with recorded votes cannot be moved", dashboard.data)
        tally = self.client.get("/admin/results/live")
        self.assertIn(b"President", tally.data)
        self.assertIn(b"Alex", tally.data)

    def test_ballot_requires_every_position_and_rejects_wrong_position_candidate(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/positions", data={"name": "Treasurer"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})
        self.client.post("/admin/candidates", data={"name": "Taylor", "position_id": "2"})
        self.client.post("/admin/election/toggle")

        missing = self.client.post(
            "/vote", data={"position_1": "1", "voting_code": self.codes[0]}
        )
        self.assertEqual(missing.status_code, 302)
        wrong_position = self.client.post(
            "/vote", data={"position_1": "2", "position_2": "2", "voting_code": self.codes[0]}
        )
        self.assertEqual(wrong_position.status_code, 302)
        accepted = self.cast_ballot({"position_1": "1", "position_2": "2"})
        self.assertEqual(accepted.status_code, 200)
        with self.app.app_context():
            from flask import current_app
            import sqlite3

            connection = sqlite3.connect(current_app.config["DATABASE"])
            count = connection.execute("SELECT COUNT(*) FROM votes").fetchone()[0]
            ballots = connection.execute("SELECT COUNT(DISTINCT ballot_id) FROM votes").fetchone()[0]
            connection.close()
        self.assertEqual(count, 2)
        self.assertEqual(ballots, 1)

    def test_closed_election_rejects_ballot_submission(self):
        self.sign_in()
        response = self.client.post("/vote", data={"position_1": "1", "voting_code": self.codes[0]})
        self.assertEqual(response.status_code, 302)

    def test_election_cannot_open_until_every_position_has_a_candidate(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/positions", data={"name": "Treasurer"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})

        self.client.post("/admin/election/toggle")
        dashboard = self.client.get("/admin")

        self.assertIn(b">Closed</strong>", dashboard.data)
        self.assertIn(b"Add at least one candidate to every position", dashboard.data)

    def test_admin_routes_require_login(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.location)

    def test_admin_can_change_school_name_and_it_persists_on_public_pages(self):
        home = self.client.get("/")
        self.assertIn(b"School Poll", home.data)

        unauthorized = self.client.post("/admin/school-name", data={"school_name": "North High"})
        self.assertEqual(unauthorized.status_code, 302)

        self.sign_in()
        updated = self.client.post("/admin/school-name", data={"school_name": "North High School"})
        self.assertEqual(updated.status_code, 302)
        home = self.client.get("/")
        dashboard = self.client.get("/admin")

        self.assertIn(b"North High School", home.data)
        self.assertIn(b"North High School", dashboard.data)

    def test_school_name_cannot_be_empty_or_longer_than_100_characters(self):
        self.sign_in()
        for name in ("  ", "N" * 101):
            response = self.client.post("/admin/school-name", data={"school_name": name})
            self.assertEqual(response.status_code, 302)
        home = self.client.get("/")
        self.assertIn(b"School Poll", home.data)

    def test_candidate_photo_is_normalized_and_visible_on_ballot(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        image_bytes = BytesIO()
        Image.new("RGB", (800, 400), color="forestgreen").save(image_bytes, format="PNG")
        image_bytes.seek(0)
        self.client.post(
            "/admin/candidates",
            data={
                "name": "Alex",
                "position_id": "1",
                "photo": (image_bytes, "candidate.png"),
            },
            content_type="multipart/form-data",
        )
        with self.app.app_context():
            from flask import current_app
            import sqlite3

            connection = sqlite3.connect(current_app.config["DATABASE"])
            photo_path = connection.execute(
                "SELECT photo_path FROM candidates WHERE name = 'Alex'"
            ).fetchone()[0]
            connection.close()
        self.assertTrue(photo_path.endswith(".jpg"))
        with Image.open(os.path.join(self.app.config["UPLOAD_FOLDER"], photo_path)) as saved_image:
            self.assertEqual(saved_image.size, (512, 512))

        self.client.post("/admin/election/toggle")
        ballot = self.client.get("/vote")
        self.assertIn(b"/candidate-photos/" + photo_path.encode(), ballot.data)
        photo_response = self.client.get(f"/candidate-photos/{photo_path}")
        self.assertEqual(photo_response.status_code, 200)
        self.assertEqual(photo_response.mimetype, "image/jpeg")
        photo_response.close()

    def test_invalid_candidate_photo_is_rejected(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        response = self.client.post(
            "/admin/candidates",
            data={
                "name": "Alex",
                "position_id": "1",
                "photo": (BytesIO(b"not an image"), "candidate.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from flask import current_app
            import sqlite3

            connection = sqlite3.connect(current_app.config["DATABASE"])
            count = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            connection.close()
        self.assertEqual(count, 0)

    def test_reset_votes_keeps_ballot_and_school_name_and_closes_election(self):
        self.sign_in()
        self.client.post("/admin/school-name", data={"school_name": "North High"})
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})
        self.client.post("/admin/election/toggle")
        self.cast_ballot({"position_1": "1"})

        response = self.client.post("/admin/election/reset", data={"reset_type": "votes"})
        self.assertEqual(response.status_code, 302)
        dashboard = self.client.get("/admin")
        self.assertIn(b"North High", dashboard.data)
        self.assertIn(b"Alex", dashboard.data)
        self.assertIn(b">Closed</strong>", dashboard.data)
        self.assertIn(b"Votes and voting codes cleared", dashboard.data)
        self.assertIn(b">0</strong>", dashboard.data)
        self.assertIn(b"Codes available: <strong>0</strong>", dashboard.data)

    def test_reset_candidates_keeps_positions_and_school_name(self):
        self.sign_in()
        self.client.post("/admin/school-name", data={"school_name": "North High"})
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})
        self.client.post("/admin/election/reset", data={"reset_type": "candidates"})
        dashboard = self.client.get("/admin")
        self.assertIn(b"North High", dashboard.data)
        self.assertIn(b"President", dashboard.data)
        self.assertNotIn(b"<strong>Alex</strong>", dashboard.data)

    def test_reset_all_restores_default_school_name_and_clears_ballot(self):
        self.sign_in()
        self.client.post("/admin/school-name", data={"school_name": "North High"})
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})
        self.client.post("/admin/election/reset", data={"reset_type": "all"})
        dashboard = self.client.get("/admin")
        self.assertIn(b"School Poll", dashboard.data)
        self.assertNotIn(b"<strong>President</strong>", dashboard.data)
        self.assertNotIn(b"<strong>Alex</strong>", dashboard.data)

    def test_records_persist_when_app_restarts(self):
        self.sign_in()
        self.client.post("/admin/positions", data={"name": "President"})
        self.client.post("/admin/candidates", data={"name": "Alex", "position_id": "1"})

        restarted_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret-key-with-at-least-32-characters",
                "DATABASE": self.app.config["DATABASE"],
                "UPLOAD_FOLDER": self.app.config["UPLOAD_FOLDER"],
                "ADMIN_PASSWORD": "test-admin-password-123",
                "WTF_CSRF_ENABLED": False,
            }
        )
        restarted_client = restarted_app.test_client()
        restarted_client.post("/admin/login", data={"password": "test-admin-password-123"})
        dashboard = restarted_client.get("/admin")
        self.assertIn(b"President", dashboard.data)
        self.assertIn(b"Alex", dashboard.data)

    def test_existing_database_gets_candidate_photo_column(self):
        import sqlite3

        legacy_database = os.path.join(self.temp_dir.name, "legacy.db")
        connection = sqlite3.connect(legacy_database)
        connection.executescript(
            """
            CREATE TABLE positions (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY,
                position_id INTEGER NOT NULL,
                name TEXT NOT NULL
            );
            """
        )
        connection.close()
        migrated_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret-key-with-at-least-32-characters",
                "DATABASE": legacy_database,
                "UPLOAD_FOLDER": self.app.config["UPLOAD_FOLDER"],
                "ADMIN_PASSWORD": "test-admin-password-123",
                "WTF_CSRF_ENABLED": False,
            }
        )
        with migrated_app.app_context():
            from flask import current_app

            connection = sqlite3.connect(current_app.config["DATABASE"])
            columns = {row[1] for row in connection.execute("PRAGMA table_info(candidates)")}
            connection.close()
        self.assertIn("photo_path", columns)

    def test_csrf_protection_rejects_a_submission_without_a_token(self):
        self.app.config["WTF_CSRF_ENABLED"] = True
        response = self.client.post("/vote", data={})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
