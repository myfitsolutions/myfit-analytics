# MyFit Automations integration

MyFit Analytics and MyFit Automations remain separate products and databases. Analytics sends authoritative member-attendance, member-status, and unresolved-payment facts over the authenticated normalized event API. Automations alone applies workflow thresholds and creates or reuses queued runs; nothing executes automatically.

Each Analytics studio has one explicit `automations_integrations` mapping containing a validated target HTTPS origin, target Automations studio identifier, enabled state, and the name of an environment variable holding its credential. The bearer value is never stored in the database, rendered, or logged. Deployments with multiple studios should resolve each mapping's environment reference through a managed per-tenant secret store in a later milestone. `MYFIT_AUTOMATIONS_ALLOWED_ORIGINS` is a comma-separated list of exact HTTPS origins, for example `https://automations.example.com,https://automations-staging.example.com`. Missing or malformed configuration fails closed. An owner may select only an origin in this server-managed list; redirects, URL credentials, paths, queries, fragments, IP literals, localhost and nonstandard ports are rejected. The sender revalidates stored mappings before every request and does not use environment-configured proxies.

Create an Automations key restricted to `allowed_source = myfit_analytics`, then set the configured environment variable (default `MYFIT_AUTOMATIONS_API_KEY`). Operators use `/integrations/myfit-automations` to view mapping/status, test the non-mutating connection endpoint, or explicitly send bounded current facts. Owners configure mappings; owners and managers may test/sync; staff have no mutation controls.

Adapters skip facts with insufficient or untrusted state. They do not reproduce retention, payment-recovery, or reactivation thresholds. New member keys use `member-fact-v3`; new payment keys use `payment-fact-v2`. Each canonical identity includes the Analytics studio, event and subject, authoritative fact time, material facts, validated target origin and studio, and a mapping revision that changes only when the target changes. The time an operator clicks Generate Facts is retained in the event payload but does not change identity. Aware timestamps are normalized to UTC; naive database timestamps are interpreted as UTC. An unchanged fact for an unchanged target reuses its row. Changing the target origin or studio atomically cancels non-delivered old-target rows, clears their claims, retains them for aggregate audit, and advances the mapping revision so returning to an earlier target still produces a new key.

## Durable outbox

Fact generation first inserts or reuses one `automations_deliveries` outbox row. The database uniquely constrains Analytics studio plus deterministic request identity. Only the safe normalized schema payload is retained—never exports or credentials. Legal transitions are `pending → delivering → delivered|failed` and operator-controlled `failed → pending`. Each attempt adds a small safe history row. One persistent correlation ID and Idempotency-Key follow the logical delivery across retries.

Operators explicitly generate facts, deliver at most three pending records per web request, or retry up to three failed records. Every Automations POST has a shared router-level session-bound CSRF dependency that executes before database-backed authorization; mapping changes remain owner-only, while owners and managers can operate delivery. Delivery atomically claims one row with a 120-second lease, commits before outbound HTTP, and persists a validated acknowledgement only when the claim token still matches. HTTP uses 2-second connect/I/O timeouts and a 12-second monotonic request budget; asynchronous cancellation bounds response streaming at the 12-second deadline. Requests and responses are bounded to 16 KiB and 8 KiB. The lease exceeds this budget plus a 30-second persistence/scheduling margin. Stale claims may be retried using the same idempotency key. A row has at most five attempts; failures use a bounded retry timestamp and fixed sanitized codes. The idempotent upgrade moves legacy in-progress rows without leases to interrupted failures (or terminal legacy failures if the payload is unavailable), and normalizes exhausted pending/in-progress rows to terminal failures. A batch stops after authentication, permission, configuration, rate-limit or receiver-unavailable failure. There is no automatic scheduler, worker, or draining loop. Receiver responses are untrusted: arbitrary strings are never persisted, rendered, or logged. The workspace shows aggregate pending, in-progress, delivered, retryable-failed, attempt-exhausted, and legacy/cancelled counts rather than subject-level facts or identifiers.

Credential resolution uses the `CredentialProvider` interface. The current `EnvironmentCredentialProvider` resolves only the environment reference on the selected studio integration; there is no cross-studio/global fallback.

## Disposable PostgreSQL gate

With Docker Desktop running, use the repository's localhost-only disposable test database and shared PostgreSQL fixture; do not run application migrations against any production URL:

```powershell
docker compose -f docker-compose.test.yml up -d --wait
$env:TEST_POSTGRES_URL='postgresql://myfit_test:myfit_test_only@127.0.0.1:55433/myfit_analytics_test'
pytest tests/test_postgresql_outbox.py
docker compose -f docker-compose.test.yml down
```

Use only this disposable test database. Never substitute a production URL.
