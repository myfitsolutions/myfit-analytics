# MyFit Automations integration

MyFit Analytics and MyFit Automations remain separate products and databases. Analytics sends authoritative member-attendance, member-status, and unresolved-payment facts over the authenticated normalized event API. Automations alone applies workflow thresholds and creates or reuses queued runs; nothing executes automatically.

Each Analytics studio has one explicit `automations_integrations` mapping containing the target base URL, target Automations studio UUID, enabled state, and the name of an environment variable holding its credential. The bearer value is never stored in the database, rendered, or logged. Deployments with multiple studios should resolve each mapping's environment reference through a managed per-tenant secret store in a later milestone.

Create an Automations key restricted to `allowed_source = myfit_analytics`, then set the configured environment variable (default `MYFIT_AUTOMATIONS_API_KEY`). Operators use `/integrations/myfit-automations` to view mapping/status, test the non-mutating connection endpoint, or explicitly send bounded current facts. Owners configure mappings; owners and managers may test/sync; staff have no mutation controls.

Adapters skip facts with insufficient or untrusted state. They do not reproduce retention, payment-recovery, or reactivation thresholds. Each request key is `mfa-analytics-` plus SHA-256 over canonical sorted JSON containing Analytics studio, event type, subject, authoritative fact timestamp, and material fact values. Changed facts produce a new identity; an identical delivery reuses its Analytics observation and Automations evaluation.

## Durable outbox

Fact generation first inserts or reuses one `automations_deliveries` outbox row. The database uniquely constrains Analytics studio plus deterministic request identity. Only the safe normalized schema payload is retained—never exports or credentials. Legal transitions are `pending → delivering → delivered|failed` and operator-controlled `failed → pending`. Each attempt adds a small safe history row. One persistent correlation ID and Idempotency-Key follow the logical delivery across retries.

Operators explicitly generate facts, deliver at most 100 pending records, or retry a failed record. Delivery uses a 4-second connect timeout and 8-second overall timeout with no automatic retries, scheduler, or draining loop. Unavailability, 429, and 5xx failures may be retried manually; 400/401/403/409 errors remain visible for operator correction. Failures do not affect startup or other Analytics workspaces.

Credential resolution uses the `CredentialProvider` interface. The current `EnvironmentCredentialProvider` resolves only the environment reference on the selected studio integration; there is no cross-studio/global fallback.

## Disposable PostgreSQL gate

With Docker Desktop running:

```powershell
docker compose -f docker-compose.test.yml up -d --wait
$env:TEST_POSTGRES_URL='postgresql://myfit_test:myfit_test_only@127.0.0.1:55433/myfit_analytics_test'
python -m app.migrate
pytest tests/test_postgresql_outbox.py
docker compose -f docker-compose.test.yml down
```

Use only this disposable test database. Never substitute a production URL.
