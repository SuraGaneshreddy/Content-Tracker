# 📚 Personal Content Tracker

A self-hosted, single-user-per-account tracker for everything you read, watch and follow:
**Manga · Manhwa · Manhua · Anime · Movies/Cinema · Sports · News · Coding · Other**.

Save a URL, keep track of where you got to, get told when something looks broken, and
get notified when a series has moved on — without any of your data leaving the box.

Built with **FastAPI + SQLAlchemy + SQLite + Jinja2 + vanilla JS**. No build step,
no Node, no frontend framework. Runs fully offline: **every external API is optional**.

---

## Contents

1. [Quick start](#1-quick-start)
2. [Feature tour](#2-feature-tour)
3. [Project structure](#3-project-structure)
4. [Database schema](#4-database-schema)
5. [Environment configuration](#5-environment-configuration)
6. [External API integration](#6-external-api-integration)
7. [Security model](#7-security-model)
8. [Import, export and backup](#8-import-export-and-backup)
9. [Testing](#9-testing)
10. [Deployment](#10-deployment)
11. [Troubleshooting](#11-troubleshooting)

> 🖥️ **Just want to get it running?** See **[RUNNING.md](RUNNING.md)** for
> copy-paste instructions for Windows, Linux and macOS.

---

## 1. Quick start

> Platform-specific walkthroughs (including PowerShell activation-policy and
> `python3-venv` gotchas) are in **[RUNNING.md](RUNNING.md)**.

```bash
cd content-tracker

# 1. Create and activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
#    → edit .env and set SECRET_KEY to a long random string:
#      python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# 4. Run (creates the database and seeds categories/statuses on first boot)
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open <http://localhost:8000>. The **first account you register becomes the admin**.

Verify the server is alive:

```bash
curl http://localhost:8000/healthz
# {"ok":true,"app":"Personal Content Tracker","env":"development"}
```

> No demo data is inserted. You start with an empty library and add your own content.

---

## 2. Feature tour

| Area | What you get |
| --- | --- |
| **Auth** | Register, login, logout, forgot password, reset password, profile, settings. Argon2id hashes, signed session cookies, rolling idle expiry. |
| **Dashboard** | Stats cards (total / reading / watching / completed / planning / on hold / favourites), category breakdown, recent activity, items with pending updates, recently opened. |
| **Add content** | One form for every category. Progress fields change per category; "Find metadata" pulls the page's public title/cover/description; duplicate URLs are flagged before saving. |
| **Cards** | Cover (server-cached WebP thumbnail or a generated placeholder), title, category, status, progress, rating, last updated, update indicator, and Open / Edit / Delete / Favourite. |
| **Open URL** | Your URL is stored and opened exactly as entered — never rewritten, never proxied. Opening records the timestamp and increments the open count. |
| **Link health** | Working 🟢 / Temporarily unavailable 🟡 / Broken 🔴 / Not checked ⚪. Broken links show *"⚠ This link may no longer be working."* plus a **Find Alternative** button that only suggests reputable public catalogues and search engines, and always requires your review before the URL changes. |
| **Updates** | Periodic checks per item. An unreachable page is reported as an error, never as "no update". Updates are marked Read / Unread / Ignore with a notification badge. |
| **Category pages** | Per-category update pages: Anime, Manga, Manhwa, Manhua, Movie (country filter: India, Japan, South Korea, China, USA, UK, Other), Sports (cricket/football/basketball/tennis/F1/other), News (country + topic), Coding. |
| **Library** | Filter by category, status, rating, favourites, tag, recency; sort A–Z, Z–A, recently added, recently opened, recently updated, highest rated. Paginated. |
| **Search** | Global search across title, description, notes, tags and status, with instant AJAX results. |
| **Details** | Full page per item: progress history, URL history (never auto-deleted), update history, notes, tags, quick progress form. |
| **Trash** | Soft delete with restore; empty trash purges permanently. |
| **Data** | JSON + CSV export, JSON/CSV import with per-row validation, and a backup download that excludes secrets. |
| **UI** | Responsive with a mobile bottom nav and collapsible sidebar, dark/light theme remembered per account, subtle transitions, keyboard-friendly. |

---

## 3. Project structure

```
content-tracker/
├── app/
│   ├── main.py              # FastAPI app, middleware order, CSP, error handlers, /healthz
│   ├── config.py            # .env loader + frozen Settings dataclass
│   ├── database.py          # engine (WAL, foreign keys on), SessionLocal, init_db()
│   ├── models.py            # all ORM models + list_categories()/list_statuses()
│   ├── constants.py         # categories, statuses, countries, sports, news topics, limits
│   ├── security.py          # Argon2id + pepper, token generation/hashing, CSRF, reset tokens
│   ├── deps.py              # session middleware, current_user, CSRF dependency, template context
│   ├── routers/
│   │   ├── auth.py          # register/login/logout/forgot/reset/profile/admin
│   │   ├── content.py       # add, detail, edit, favourite, progress, open, delete/restore, checks
│   │   ├── library.py       # library, search, tags, favourites, trash
│   │   ├── updates.py       # updates feed + per-category update pages
│   │   ├── notifications.py # notifications page + JSON API
│   │   ├── settings.py      # settings page, theme/view API, password change
│   │   ├── data.py          # import / export / backup
│   │   └── admin.py         # admin dashboard (first registered user only)
│   ├── services/
│   │   ├── content_service.py  # CRUD, queries, validation, progress labels, search
│   │   ├── url_checker.py      # link health, degraded streaks, history
│   │   ├── updates.py          # update detection (page scrape, Jikan, TMDB, RSS, ESPN)
│   │   ├── metadata.py         # page scraping + external API clients
│   │   ├── alternatives.py     # reputable-source suggestions for broken links
│   │   ├── rss.py              # news / sports / coding feeds
│   │   ├── io_services.py      # JSON + CSV export/import with validation
│   │   ├── backup.py           # secret-free backup archive
│   │   ├── thumbnails.py       # cover fetching + WebP cache
│   │   ├── activity.py         # activity log + notifications
│   │   ├── scheduler.py        # background periodic checks
│   │   └── seed.py             # idempotent reference data
│   └── utils/
│       ├── safe_http.py        # SSRF-safe fetcher: DNS pinning, private-range block, rate limits
│       ├── url_validation.py   # URL syntax + scheme/host policy
│       └── rate_limit.py       # in-memory sliding window limiter
├── templates/               # Jinja2 (base, partials/, auth/, one page per route)
├── static/css/app.css       # design system, responsive layout, themes
├── static/js/app.js         # progressive enhancement only (no framework)
├── tests/                   # pytest suite (see §9)
├── data/                    # SQLite DB + thumbnail cache (gitignored)
├── requirements.txt
└── .env.example
```

---

## 4. Database schema

Tables are created by `init_db()` on first boot (`Base.metadata.create_all`). Every
content-bearing table carries `user_id`, so all queries are scoped per account.

| Table | Purpose | Notable columns |
| --- | --- | --- |
| `users` | Accounts | `email` (unique), `password_hash`, `display_name`, `is_admin`, `failed_login_count`, `locked_until`, `password_reset_token_hash`, `password_reset_expires` |
| `session_tokens` | Server-side sessions | `token_hash`, `csrf_secret`, `user_agent`, `expires_at`, `last_seen_at`, `revoked_at` |
| `user_settings` | Per-user preferences | `theme`, `default_view`, `items_per_page`, `default_category`, `auto_check_urls`, `auto_check_updates`, `check_interval_minutes`, notify flags |
| `categories` | Reference data (extensible) | `slug`, `name`, `icon`, `accent`, `sort_order`, `progress_fields`, `status_options`, `update_source`, `is_active` |
| `statuses` | Reference data | `slug`, `label`, `group`, `color`, `sort_order` |
| `content` | The single generic item table | `title`, `url`, `category_id`, `status`, `current_chapter/episode/season/volume`, `progress_text`, `progress_percent`, `rating`, `notes`, `cover_image_url`, `external_id`, `update_source`, `latest_available`, `update_available`, `update_state`, `url_status`, `url_status_code`, `url_last_checked`, `open_count`, `last_opened_at`, `deleted_at`, `purged_at` |
| `tags` / `content_tags` | Many-to-many tags | `tags.user_id` keeps tag names private per account |
| `favorites` | Favourites | unique `(user_id, content_id)` |
| `url_history` | Every URL an item has had | `url`, `source` (`user` / `suggested`), `added_at` — never auto-deleted |
| `update_history` | Every detected update | `kind`, `previous_value`, `new_value`, `seen_at`, `state` |
| `url_check_log` | Health check results | `status`, `status_code`, `latency_ms`, `host`, `checked_at` |
| `notifications` | In-app alerts | `kind` (`broken_url`, `new_update`, `info`), `title`, `message`, `read_at` |
| `activity_log` | Recent activity feed | `action`, `summary`, `created_at` |
| `external_cache` | Cached external payloads | `cache_key`, `payload` (JSON), `expires_at` |

**Design decision:** one generic `content` table with sparse, nullable progress columns,
driven by `categories.progress_fields` (a CSV such as `chapter,volume`). Adding a new
category is a row plus a `CATEGORIES` entry — no schema change, no new template. The form
and the card renderer both read the category configuration, so new categories render
correctly immediately.

`progress_fields` uses the **form field names** (`chapter`, `volume`, `season`,
`episode`, `progress_text`, `progress_percent`) so `static/js/app.js` can show and hide
the right inputs. `seed_reference_data()` re-syncs those three configuration columns
(`progress_fields`, `status_options`, `update_source`) on every boot; names, icons and
sort order are never overwritten.

---

## 5. Environment configuration

All configuration comes from the environment or a `.env` file at the project root.
`.env.example` is the canonical, commented list. Summary:

| Group | Variables |
| --- | --- |
| App | `APP_NAME`, `APP_ENV` (`development`/`production`), `SECRET_KEY` (**required in production**), `PUBLIC_BASE_URL`, `HOST`, `PORT` |
| Database | `DATABASE_URL` (default `sqlite:///data/tracker.db`; any SQLAlchemy URL works) |
| Security | `SESSION_COOKIE_NAME`, `SESSION_MAX_AGE_DAYS`, `SESSION_IDLE_MINUTES`, `REMEMBER_ME_DAYS`, `PASSWORD_MIN_LENGTH`, `PCT_PASSWORD_PEPPER`, `AUTH_RATE_LIMIT`, `AUTH_RATE_WINDOW_SECONDS`, `MAX_UPLOAD_MB` |
| Outbound fetch | `FETCH_TIMEOUT_SECONDS`, `FETCH_MAX_BYTES`, `FETCH_ALLOWED_PORTS`, `FETCH_BLOCK_PRIVATE_NETWORKS`, `FETCH_MAX_REDIRECTS`, `FETCH_MIN_INTERVAL_PER_HOST_SECONDS`, `FETCH_MAX_REQUESTS_PER_RUN` |
| Scheduler | `ENABLE_SCHEDULER`, `CHECK_INTERVAL_MINUTES`, `URL_HEALTH_INTERVAL_HOURS`, `UPDATE_CHECK_INTERVAL_HOURS` |
| External APIs | `TMDB_API_KEY`, `TMDB_API_READ_TOKEN`, `JIKAN_ENABLED`, `NEWSAPI_KEY`, `ESPN_ENABLED`, `RSS_ENABLED` |
| Scraping/cache | `ENABLE_URL_METADATA_SCRAPING`, `ENABLE_THUMBNAIL_CACHE`, `THUMBNAIL_WIDTH`, `THUMBNAIL_HEIGHT` |

**Keys are never exposed to the browser.** They live in `Settings` on the server, are used
only inside `app/services/metadata.py`, and the admin page reports integration state as a
boolean ("enabled"/"not configured") — never the key. Export, backup and every template
exclude secrets by construction.

---

## 6. External API integration

Every integration is **optional**. With no keys configured the app still works: you enter
titles by hand, and "Find metadata" falls back to scraping the page's own public
`<title>`, Open Graph image and meta description.

| Source | Env var | Key needed? | Used for |
| --- | --- | --- | --- |
| Page scrape | `ENABLE_URL_METADATA_SCRAPING` | No | Title, cover, description of any URL you add; chapter-number detection for update checks |
| Jikan (MyAnimeList) | `JIKAN_ENABLED` | No | Anime search + latest episode for items with an `external_id` |
| TMDB | `TMDB_API_KEY` | Yes | Movie search and upcoming releases |
| NewsAPI | `NEWSAPI_KEY` | Yes | News headlines by country/topic |
| ESPN JSON | `ESPN_ENABLED` | No | Sports scores/fixtures |
| RSS | `RSS_ENABLED` | No | News, coding and sports feeds when no API key is set |

**How to add an integration**

1. Add settings in `app/config.py` (read from env, default off) and list them in `.env.example`.
2. Write a client function in `app/services/metadata.py` that calls `safe_get(...)` —
   never `httpx` or `requests` directly. Cache results through `ExternalCache` so the same
   request is not repeated.
3. Hook it into `app/services/updates.py::check_series` (for update detection) or into the
   relevant `/updates/<category>` route (for public listings).
4. Register the source name in `Category.update_source` (`none|page|jikan|tmdb|rss|espn`).
5. Report availability on the settings page via the `integrations` context dict.
6. Return an explicit `error` when the call fails. **Never** represent "the request failed"
   as "nothing new" — an unverified update is never claimed.

All outbound traffic goes through `app/utils/safe_http.py`, which resolves DNS, pins the
validated address for the actual connection, blocks private/link-local/metadata ranges and
non-allow-listed ports, caps response size and redirects, and enforces per-host and
per-run rate limits.

---

## 7. Security model

| Concern | Implementation |
| --- | --- |
| Passwords | Argon2id (`argon2-cffi`) with a per-install pepper; strength checks on register and reset |
| Sessions | Random token, stored server-side as a SHA-256 hash; cookie is signed, `HttpOnly`, `SameSite=lax`, `Path=/`; rolling idle expiry; password change revokes other sessions |
| CSRF | HMAC token bound to the session's `csrf_secret`, required on **every** state-changing route via the `csrf_required` router dependency; accepted from the `csrf_token` form field or the `X-CSRF-Token` header; `/logout` is exempt |
| Authorisation | Every query filters by `user_id`; accessing another user's item returns 404, not 403, so existence is not leaked |
| Input | Server-side validation on every route; titles/notes sanitised for display; Jinja autoescaping on; SQL only via SQLAlchemy parameters |
| URLs | Syntax + scheme/host policy at save time (`http`/`https`, allow-listed ports, no private/internal hosts); the fetcher re-validates at request time |
| User URLs are never executed | The server only performs a plain `GET`/`HEAD` for health and metadata, with redirects capped and the response treated as untrusted text. It never renders, evaluates or proxies user content. |
| Headers | Strict CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, HSTS in production |
| Errors | Friendly HTML/JSON error pages; stack traces and secrets are never rendered |
| API surface | `docs_url`, `redoc_url` and `openapi_url` are disabled |
| Rate limiting | Login/register/reset per IP; outbound requests per host and per run |

---

## 8. Import, export and backup

Reached from **Settings → Your data** or the **Data** page (`/data`).

- **Export JSON** (`/export/json`) — full library with tags, progress, favourites, URL
  history and update history. Includes `app` and `version` markers for forward compatibility.
- **Export CSV** (`/export/csv`) — flat, spreadsheet-friendly, tags joined with `; `.
- **Import** (`POST /data/import`) — accepts either format. Category and status accept
  slugs *or* display names ("Movie / Cinema" → `movie`). Per-row validation rejects empty
  titles, unsafe URLs and out-of-range ratings; an unknown category falls back to `other`
  instead of failing the file. Duplicate URLs are skipped and reported.
- **Backup** (`/backup`) — JSON archive with your settings and library. It **excludes**
  `password_hash`, session tokens, reset tokens and every API key.
- **Restore** (`POST /data/restore`) — re-imports a backup.

Uploads are capped by `MAX_UPLOAD_MB` (default 12 MB); oversize or malformed files
redirect back with a readable message rather than an error page.

---

## 9. Testing

```bash
pip install -r requirements.txt
python3 -m pytest -q                 # whole suite
python3 -m pytest tests/ -q -v       # verbose
python3 -m pytest tests/test_security.py -q          # one module
python3 -m pytest -q -k "csrf"       # by keyword
python3 -m pytest --cov=app -q       # coverage (requires pytest-cov)
```

Current suite: **217 tests**.

| Module | Focus |
| --- | --- |
| `test_auth.py` | register, login, logout, sessions, lockout, password reset, profile |
| `test_content.py` | add/edit/delete, validation, progress, rating, tags, favourites, duplicates |
| `test_library.py` | filters, sorting, pagination, search, tags, trash, restore |
| `test_security.py` | per-user isolation, CSRF, XSS, SQL injection, SSRF, hashing, secret leakage, docs disabled |
| `test_urls.py` | health statuses, broken-link notification, throttling, escalation, history, alternatives |
| `test_updates.py` | newer/same/older chapters, unreachable pages, fractional chapters, anime episodes, feed |
| `test_data.py` | JSON/CSV export + import, validation, oversize uploads, backup, restore |
| `test_pages.py` | every route renders for anonymous / authed / admin visitors; theme and form contracts |

**The tests never touch the network.** `tests/conftest.py` forces a temporary database and
test-only environment variables *before* the app is imported, and stubs `safe_get`,
`scrape_page`, `fetch_feed` and `jikan_latest_episode` on the modules that import them.

Two helpers you will need when writing tests:

```python
from tests.conftest import add_item, api_post, body, content_id_from

location = add_item(client, title="Solo Leveling", url="https://example.org/solo",
                    category="manhwa", status="reading", chapter=125)
content_id = content_id_from(location)          # redirects carry ?msg=… — never parse them by hand

api_post(client, f"/api/content/{content_id}/favorite", {"favorite": True})
assert "Solo Leveling" in body(client.get("/")) # body() unescapes Jinja's HTML entities
```

Every state-changing API call must send the CSRF token — `api_post` does this for you.

---

## 10. Deployment

### Production checklist

1. `APP_ENV=production`
2. `SECRET_KEY` — a long random string, unique per install, kept out of version control
3. `PUBLIC_BASE_URL` — the real HTTPS origin (used for reset links)
4. Serve behind TLS (HSTS is only emitted when `APP_ENV=production`)
5. Set `ENABLE_SCHEDULER=true` on exactly **one** worker (see below)
6. Back up `data/` (SQLite file + thumbnail cache), or point `DATABASE_URL` at Postgres

### systemd + nginx (single host, recommended)

`/etc/systemd/system/tracker.service`

```ini
[Unit]
Description=Personal Content Tracker
After=network.target

[Service]
Type=simple
User=tracker
WorkingDirectory=/opt/content-tracker
EnvironmentFile=/opt/content-tracker/.env
ExecStart=/opt/content-tracker/.venv/bin/uvicorn app.main:app \
          --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now tracker
```

nginx site:

```nginx
server {
    listen 443 ssl http2;
    server_name tracker.example.com;

    ssl_certificate     /etc/letsencrypt/live/tracker.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tracker.example.com/privkey.pem;

    client_max_body_size 16m;               # import uploads

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
    }

    location /static/ {
        alias /opt/content-tracker/static/;
        expires 7d;
        add_header Cache-Control "public";
    }
}

server {
    listen 80;
    server_name tracker.example.com;
    return 301 https://$host$request_uri;
}
```

### Docker

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m tracker && mkdir -p data && chown -R tracker /app
USER tracker
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t content-tracker .
docker run -d --name tracker -p 8000:8000 \
  --env-file .env -v tracker-data:/app/data content-tracker
```

### Multiple workers

The in-process scheduler and the in-memory rate limiter are per-process. If you run more
than one worker, set `ENABLE_SCHEDULER=false` on all of them and trigger checks from cron
instead:

```cron
*/30 * * * * curl -fsS -X POST http://127.0.0.1:8000/api/run-checks
```

(That endpoint requires an authenticated session, so the simpler option is a small
management script, or keep a single worker — this app is comfortably single-worker for one
or a few users.)

### Upgrades

```bash
git pull
pip install -r requirements.txt
sudo systemctl restart tracker
```

`init_db()` uses `create_all`, which adds missing tables but does not migrate existing
columns. For schema changes on an existing database, take a `/backup` download first, then
apply the migration manually or restore into a fresh database.

---

## 11. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Your session expired` on a button click | The page was opened before a restart. Reload it — the CSRF token is bound to the session. |
| Covers never appear | `ENABLE_THUMBNAIL_CACHE=false`, or the host blocks the fetch. The card falls back to a generated placeholder. |
| Nothing happens in **Updates** | `ENABLE_SCHEDULER=false`, or the item's `update_source` is `none`, or the interval has not elapsed. Use **Check for updates** on the item to force one. |
| A link shows 🟡 forever | The site is rate-limiting or returning 5xx. Health only escalates to 🔴 after repeated failures, so a temporary outage is not recorded as broken. |
| Import says `err=too-large` | Raise `MAX_UPLOAD_MB`, or split the file. |
| Forgot password, no email arrives | No mail server is wired up. With `APP_ENV=development` the reset link is printed on the page itself; with `APP_ENV=production` it is suppressed, so connect an SMTP provider in `app/routers/auth.py::request_reset` before deploying. |
| Port already in use | Change `PORT` in `.env`, or stop the other process. |

---

### Licence

Personal-use project. Do what you like with it; no warranty is implied.
