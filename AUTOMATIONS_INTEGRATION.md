# MyFit Automations integration

MyFit Analytics and MyFit Automations remain separate products and databases. Analytics sends authoritative member-attendance, member-status, and unresolved-payment facts over the authenticated normalized event API. Automations alone applies workflow thresholds and creates or reuses queued runs; nothing executes automatically.

Each Analytics studio has one explicit `automations_integrations` mapping containing the target base URL, target Automations studio UUID, enabled state, and the name of an environment variable holding its credential. The bearer value is never stored in the database, rendered, or logged. Deployments with multiple studios should resolve each mapping's environment reference through a managed per-tenant secret store in a later milestone.

Create an Automations key restricted to `allowed_source = myfit_analytics`, then set the configured environment variable (default `MYFIT_AUTOMATIONS_API_KEY`). Operators use `/integrations/myfit-automations` to view mapping/status, test the non-mutating connection endpoint, or explicitly send bounded current facts. Owners configure mappings; owners and managers may test/sync; staff have no mutation controls.

Adapters skip facts with insufficient or untrusted state. They do not reproduce retention, payment-recovery, or reactivation thresholds. Each request key is `mfa-analytics-` plus SHA-256 over canonical sorted JSON containing Analytics studio, event type, subject, authoritative fact timestamp, and material fact values. Changed facts produce a new identity; an identical delivery reuses its Analytics observation and Automations evaluation.

Delivery uses a 4-second connect timeout and 8-second overall timeout with no automatic retries. Operators may retry later using the same deterministic key. Unavailability and safe HTTP error codes are persisted without payloads, secrets, or exception traces, and do not affect application startup or other Analytics workspaces.
