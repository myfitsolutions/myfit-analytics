# MyFit Analytics Production Deployment

This guide targets a Render Web Service with Supabase PostgreSQL, but the commands work on comparable hosted Python platforms. Production must use HTTPS; let the hosting platform terminate TLS rather than configuring certificates in Uvicorn.

## Architecture

- FastAPI/Uvicorn web application
- SQLAlchemy with PostgreSQL and `pool_pre_ping=True`
- Supabase-hosted PostgreSQL
- Server-side authorization resolved from the session's `user_id`
- Jinja2 templates and static JavaScript/CSS
- Optional OpenAI message generation with deterministic browser fallback
- Optional SMTP manual email
- Explicit, ordered, idempotent schema migrations

Production does not run `Base.metadata.create_all()`. Development retains it as a local convenience. Schema initialization and upgrades are explicit trusted-shell operations.

## Database initialization and migrations

For a brand-new, empty PostgreSQL database, run once:

```text
python -m app.bootstrap_database
```

The bootstrap command verifies connectivity, refuses to modify an existing or partial MyFit schema, creates the current SQLAlchemy baseline schema, runs the complete ordered migration runner internally, and inspects the resulting tables, columns, relationships, and important constraints. It creates schema only—no studios, users, members, bookings, payments, or sample data. Do not immediately run `app.migrate` after a successful bootstrap because bootstrap already ran it.

For an existing initialized MyFit database, run only the idempotent incremental migrations:

```text
python -m app.migrate
```

Neither command drops tables or deletes application data. Production application startup continues to perform no schema creation.

## Environment variables

Required in production:

- `APP_ENV=production`
- `DATABASE_URL`
- `SESSION_SECRET` — a randomly generated value of at least 32 characters
- `SESSION_COOKIE_SECURE=true`

Optional:

- `OPENAI_API_KEY`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `EMAIL_FROM`
- `SMTP_USE_SSL`
- `SMTP_USE_TLS`

Do not commit values to source control or print them in deployment logs. SMTP and OpenAI may be omitted; analytics still run, email fails with a controlled response, and the existing deterministic message fallback remains available.

## Render commands

Build command:

```text
pip install -r requirements.txt
```

Pre-deploy/migration command for an existing initialized database:

```text
python -m app.migrate
```

Start command:

```text
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Production startup validates configuration and performs `SELECT 1`. A missing/weak session secret, insecure cookie setting, missing database URL, or unavailable database stops startup.

## First studio and Owner

There is no public signup. Create the studio and owner deliberately from a trusted deployment shell:

```text
python -m app.create_studio
python -m app.create_user
```

Record the Studio ID from the first command. In `create_user`, enter that ID and explicitly choose `owner`. Password input and hashing are never printed. The command validates that the studio exists and rejects duplicate email addresses.

## Operations

- `GET /health` is a public process-health probe and returns only `{"status":"ok"}`.
- `GET /ready` performs `SELECT 1`; database failures return a generic 503 body.
- Every response includes `X-Request-ID` and conservative browser security headers.
- Unexpected failures return a generic message plus request ID. Server logs contain only request ID and exception class.
- Configure platform/proxy login rate limiting. This V1 app does not implement distributed brute-force protection and should not use an in-memory limiter as a substitute.
- A strict Content Security Policy is deferred because the dashboard currently uses inline JavaScript and Chart.js delivery patterns; adding one now would break the application without a dedicated asset refactor.
- Use the PostgreSQL provider's supported backup, point-in-time recovery, and restore testing. MyFit does not implement a custom backup system and this document does not imply backups are enabled.
