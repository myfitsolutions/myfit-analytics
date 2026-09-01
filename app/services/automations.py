"""Provider boundary that sends Analytics facts to MyFit Automations."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import httpx
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models import AutomationsDelivery, AutomationsIntegration, Member, Payment

SOURCE = "myfit_analytics"
SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class Fact:
    payload: dict[str, Any]
    idempotency_key: str


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _identity(studio_id: int, event_type: str, subject_id: str, fact_at: datetime, facts: dict) -> str:
    canonical = json.dumps({"studio": studio_id, "event_type": event_type, "subject": subject_id,
                            "fact_at": _iso(fact_at), "facts": facts}, sort_keys=True, separators=(",", ":"))
    return "mfa-analytics-" + hashlib.sha256(canonical.encode()).hexdigest()


def member_fact(member: Member, automations_studio_id: str, evaluation_at: datetime, *, reactivation: bool) -> Fact | None:
    if member.last_visit_at is None or member.status not in {"active", "inactive", "lapsed"}:
        return None
    active = member.status == "active"
    if reactivation == active:
        return None
    event_type = "member_status_snapshot" if reactivation else "member_activity_snapshot"
    facts = {"member_active": active, "last_attendance_at": _iso(member.last_visit_at)}
    payload = {"event_type": event_type, "studio_id": automations_studio_id, "subject_type": "member",
               "subject_id": str(member.id), "occurred_at": _iso(evaluation_at), "evaluation_at": _iso(evaluation_at),
               "source": SOURCE, "source_reference": f"analytics:member:{member.id}:{event_type}", "payload": facts}
    return Fact(payload, _identity(member.studio_id, event_type, str(member.id), evaluation_at, facts))


def payment_fact(payment: Payment, automations_studio_id: str, evaluation_at: datetime) -> Fact | None:
    if payment.status not in {"failed", "declined", "unpaid"} or payment.payment_date is None:
        return None
    facts = {"resolved": False, "failed_at": _iso(payment.payment_date), "member_id": str(payment.member_id)}
    payload = {"event_type": "payment_failure", "studio_id": automations_studio_id, "subject_type": "payment",
               "subject_id": str(payment.id), "occurred_at": _iso(payment.payment_date), "evaluation_at": _iso(evaluation_at),
               "source": SOURCE, "source_reference": f"analytics:payment:{payment.id}", "payload": facts}
    return Fact(payload, _identity(payment.studio_id, "payment_failure", str(payment.id), payment.payment_date, facts))


class AutomationsClient:
    def __init__(self, transport=None):
        self.transport = transport

    def _credential(self, integration: AutomationsIntegration) -> str | None:
        return os.getenv(integration.credential_env_var) or None

    def connection(self, integration: AutomationsIntegration, correlation_id: str | None = None) -> dict:
        return self._request(integration, "GET", "connection", None, None, correlation_id)

    def deliver(self, db: Session, integration: AutomationsIntegration, fact: Fact,
                correlation_id: str | None = None) -> AutomationsDelivery:
        from app.models import AutomationsDelivery
        correlation_id = correlation_id or str(uuid.uuid4())
        existing = db.query(AutomationsDelivery).filter_by(analytics_studio_id=integration.analytics_studio_id,
                                                           idempotency_key=fact.idempotency_key).first()
        result = self._request(integration, "POST", "events", fact.payload, fact.idempotency_key, correlation_id)
        delivery = existing or AutomationsDelivery(analytics_studio_id=integration.analytics_studio_id,
            automations_studio_id=integration.automations_studio_id, event_type=fact.payload["event_type"],
            subject_id=fact.payload["subject_id"], correlation_id=correlation_id,
            idempotency_key=fact.idempotency_key, delivery_status="attempting")
        if existing:
            delivery.attempt_count += 1
        delivery.correlation_id = result.get("correlation_id", correlation_id)
        delivery.delivery_status = "accepted" if result["ok"] else "failed"
        delivery.http_status = result.get("http_status")
        delivery.evaluation_id = result.get("evaluation_id")
        delivery.runs_created = result.get("runs_created", 0)
        delivery.runs_reused = result.get("runs_reused", 0)
        delivery.safe_error_code = result.get("error")
        delivery.last_attempt_at = datetime.now(timezone.utc)
        if not existing:
            db.add(delivery)
        db.commit(); db.refresh(delivery)
        return delivery

    def _request(self, integration, method, suffix, payload, idempotency_key, correlation_id):
        correlation_id = correlation_id or str(uuid.uuid4())
        if not integration.integration_enabled:
            return {"ok": False, "error": "integration_disabled", "correlation_id": correlation_id}
        token = self._credential(integration)
        if not token:
            return {"ok": False, "error": "credential_not_configured", "correlation_id": correlation_id}
        url = f"{integration.automations_base_url.rstrip('/')}/internal/v1/studios/{integration.automations_studio_id}/{suffix}"
        headers = {"Authorization": f"Bearer {token}", "X-MyFit-Event-Version": SCHEMA_VERSION,
                   "X-Correlation-ID": correlation_id, "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0), transport=self.transport) as client:
                response = client.request(method, url, json=payload, headers=headers)
            try: body = response.json()
            except ValueError: body = {}
            return {"ok": 200 <= response.status_code < 300, "http_status": response.status_code,
                    "correlation_id": body.get("correlation_id", correlation_id),
                    "evaluation_id": body.get("evaluation_id"), "runs_created": body.get("runs_created", 0),
                    "runs_reused": body.get("runs_reused", 0), "error": body.get("error")}
        except httpx.RequestError:
            return {"ok": False, "error": "automations_unavailable", "correlation_id": correlation_id}
