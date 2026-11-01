# School Poll

A locally hosted Flask application for secondary-school elections. Administrators create positions and candidates, open and close the election, and view live count visualizations. Students submit one anonymous ballot with a choice for every configured position.

## Privacy and voting behavior

- The ballot does not ask for or store a voter's name, student ID, email, or other voter identity.
- Each ballot requires a random one-time voting code. The app stores only a keyed hash of each code and marks it used in the same database transaction that saves the complete ballot; the code is not stored with the selections.
- Codes prevent reuse of the same issued code, but they do not verify a voter's identity or stop code sharing, someone receiving multiple codes, or an administrator issuing extra codes. Distribute one code to each eligible voter.
- The code and ballot are stored separately, but this is not a guarantee of absolute anonymity. Server access logs, timing, or other operational data could potentially correlate activity. Avoid collecting identifying request logs and restrict database and server access.
- A random, non-identifying ballot reference groups the selections in one submission and is not shown to the voter.
- The administrator dashboard shows aggregate counts. The application does not expose live results on the public pages.
- SQLite stores election data in `polling.db` in this folder by default. Data and candidate photos persist when the server restarts. Keep access to this file and the `static/uploads/candidates` folder limited to trusted administrators.
- Candidate photos are optional. JPEG, PNG, and WebP images up to 5 MB are accepted and normalized to square 512 × 512 JPEG portraits.

## Requirements

- Python 3.10 or newer
- A current browser on the host computer and devices on the same trusted local network

## Install and run on Windows

Open PowerShell in this project folder, then run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

The app reads `ADMIN_PASSWORD` and `SECRET_KEY` from the `.env` file in the project folder. A private `.env` with generated credentials is included for local use; keep it private and do not share or commit it. The repository ignores `.env`. To replace the credentials, edit `.env`: use a unique administrator password of at least 12 characters and a random secret key of at least 32 characters. You can copy `.env.example` as a template if you need to recreate the file. Both settings are required, and the secret key should remain stable between restarts. Changing it invalidates any unused voting codes; generate and distribute new codes after changing the key.

Open `http://127.0.0.1:5000` on the host computer. The administrator sign-in is at `http://127.0.0.1:5000/admin/login`; use the `ADMIN_PASSWORD` value in `.env`.

### Allow students on the same local network

The server listens on all network interfaces by default. Find the host computer's local IPv4 address with `ipconfig`, then have students on the same trusted network open `http://<host-ip-address>:5000` (for example, `http://192.168.1.25:5000`). If Windows Firewall prompts you, allow Python only on the trusted private network. Do not expose this development server to the public internet; Flask's built-in server is intended for local use, not internet-facing production.

Set a different port with `$env:PORT = "8000"` before starting if needed.

## Run on macOS or Linux

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Create a private `.env` file with `ADMIN_PASSWORD` and `SECRET_KEY` values first (see `.env.example`). The app loads these values automatically. Open `http://127.0.0.1:5000`. For local-network access, use the host computer's local IP address and port 5000.

## Election workflow

1. Sign in at `/admin/login`.
2. Optionally change the school name from the dashboard. It defaults to **School Poll** and is shown in the site header, footer, and page titles.
3. Add all election positions, then add candidates under each position. Ballot changes are permitted only while voting is closed.
4. While voting is closed, generate a batch of one-time codes from **Voter access** and download the CSV. Distribute one code to each eligible voter using a private channel. Codes are shown only in that download; the database stores hashes, not recoverable codes. If the file is lost, replace unused codes and distribute the new batch.
5. Select **Open voting** when the ballot and codes are ready. Voting cannot open unless at least one unused code exists. The ballot requires a valid code and one candidate selection for every position.
6. Share the host's local network address with students. Each code can submit one complete ballot and is consumed only when that ballot is accepted.
7. Open **Live tally** from the administrator navigation to view counts by candidate and position. The tally refreshes automatically every three seconds and remains administrator-only.

Close voting before changing the ballot. Administrators can rename positions and candidates or move candidates to another position while voting is closed. Renames preserve existing vote counts, while candidates with recorded votes cannot be moved or deleted, which protects the published tally.

Administrators can generate up to 500 codes per batch. **Replace all unused codes** revokes the existing unused codes before issuing a new batch; already-used codes stay marked used. Keep exported code files private and delete them securely after distribution. Closing voting does not invalidate unspent codes.

## Preparing for another election

The server restart does not clear records. Sign in to the dashboard and use **Reset election data** to choose one of three reset options. Every reset closes voting and requires confirmation:

1. **Clear votes** — removes all ballots, tallies, and voting codes but keeps the school name, positions, candidates, and their photos.
2. **Clear votes and candidates** — removes ballots, tallies, voting codes, candidates, and candidate photos, while keeping positions and the school name.
3. **Delete all election data** — removes ballots, voting codes, candidates, positions, and photos, and resets the school name to **School Poll**.

These actions permanently remove data. Before resetting, stop the server and make a backup copy of `polling.db` (and `static/uploads/candidates` if you need candidate photos). Restart the server after backing up, then select the desired reset option in the dashboard. Generate and distribute a fresh code batch before opening the next election.

## Candidate photos

When adding a candidate, optionally choose a JPEG, PNG, or WebP photo up to 5 MB. The app crops and resizes it to a square portrait for a consistent ballot. Administrators can replace or remove photos while voting is closed. Existing databases are upgraded automatically to support candidate photos.

## Configuration and data

| Variable | Purpose | Default |
| --- | --- | --- |
| `ADMIN_PASSWORD` | Administrator sign-in password (required; minimum 12 characters) | Set in `.env` |
| `SECRET_KEY` | Flask session and CSRF signing key (required; minimum 32 characters) | Set in `.env` |
| `POLLING_DATABASE` | SQLite database file path | `polling.db` in the project folder |
| `HOST` | Bind address | `0.0.0.0` |
| `PORT` | HTTP port | `5000` |

## Commercial license

The included [COMMERCIAL-LICENSE.txt](./COMMERCIAL-LICENSE.txt) is a plain-language draft for a non-exclusive license to use one copy at one school or campus. It prohibits redistribution, resale, sublicensing, and multi-site use. Complete the seller and transaction details and obtain legal review before offering it as a binding commercial agreement; this template is not legal advice.

Stop the server with **Ctrl+C**. Back up `polling.db` while the application is stopped. The app does not verify voter identity, prevent voting-code sharing, provide voter identity recovery, or allow ballot editing. It is intended for low-stakes school polls, not high-stakes or legally binding elections.
