"""Durable, operator-controlled Analytics facts delivery boundary."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, TYPE_CHECKING

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models import AutomationsDelivery, AutomationsIntegration, Member, Payment

SOURCE = "myfit_analytics"
SCHEMA_VERSION = "1"
OUTBOX_BATCH_SIZE = 100
LEGAL_TRANSITIONS = {"pending":{"delivering"}, "delivering":{"delivered","failed"}, "failed":{"pending"}, "delivered":set()}


@dataclass(frozen=True)
class Fact:
    payload: dict[str, Any]
    idempotency_key: str


class CredentialProvider(Protocol):
    def get_automations_bearer_token(self, integration: AutomationsIntegration) -> str | None: ...


class EnvironmentCredentialProvider:
    def get_automations_bearer_token(self, integration: AutomationsIntegration) -> str | None:
        # There is deliberately no global fallback: each mapping selects its reference.
        reference = (integration.credential_env_var or "").strip()
        return os.getenv(reference) or None if reference else None


def _iso(value: datetime) -> str:
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_fingerprint(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _identity(studio_id: int, event_type: str, subject_id: str, fact_at: datetime, facts: dict) -> str:
    canonical = _canonical({"studio":studio_id,"event_type":event_type,"subject":subject_id,
                            "fact_at":_iso(fact_at),"facts":facts})
    return "mfa-analytics-" + hashlib.sha256(canonical.encode()).hexdigest()


def member_fact(member: Member, automations_studio_id: str, evaluation_at: datetime, *, reactivation: bool) -> Fact | None:
    if member.last_visit_at is None or member.status not in {"active","inactive","lapsed"}: return None
    active = member.status == "active"
    if reactivation == active: return None
    event_type = "member_status_snapshot" if reactivation else "member_activity_snapshot"
    facts = {"member_active":active,"last_attendance_at":_iso(member.last_visit_at)}
    payload = {"event_type":event_type,"studio_id":automations_studio_id,"subject_type":"member",
        "subject_id":str(member.id),"occurred_at":_iso(evaluation_at),"evaluation_at":_iso(evaluation_at),
        "source":SOURCE,"source_reference":f"analytics:member:{member.id}:{event_type}","payload":facts}
    return Fact(payload,_identity(member.studio_id,event_type,str(member.id),evaluation_at,facts))


def payment_fact(payment: Payment, automations_studio_id: str, evaluation_at: datetime) -> Fact | None:
    if payment.status not in {"failed","declined","unpaid"} or payment.payment_date is None: return None
    facts = {"resolved":False,"failed_at":_iso(payment.payment_date),"member_id":str(payment.member_id)}
    payload = {"event_type":"payment_failure","studio_id":automations_studio_id,"subject_type":"payment",
        "subject_id":str(payment.id),"occurred_at":_iso(payment.payment_date),"evaluation_at":_iso(evaluation_at),
        "source":SOURCE,"source_reference":f"analytics:payment:{payment.id}","payload":facts}
    return Fact(payload,_identity(payment.studio_id,"payment_failure",str(payment.id),payment.payment_date,facts))


def transition(item: AutomationsDelivery, target: str) -> None:
    if target not in LEGAL_TRANSITIONS.get(item.delivery_status, set()):
        raise ValueError(f"Illegal outbox transition: {item.delivery_status} -> {target}")
    item.delivery_status = target


class AutomationsClient:
    def __init__(self, transport=None, credential_provider: CredentialProvider | None = None):
        self.transport = transport
        self.credentials = credential_provider or EnvironmentCredentialProvider()

    def connection(self, integration, correlation_id: str | None = None) -> dict:
        return self._request(integration,"GET","connection",None,None,correlation_id)

    def send(self, integration, payload: dict, idempotency_key: str, correlation_id: str) -> dict:
        return self._request(integration,"POST","events",payload,idempotency_key,correlation_id)

    def _request(self,integration,method,suffix,payload,idempotency_key,correlation_id):
        correlation_id = correlation_id or str(uuid.uuid4())
        if not integration.integration_enabled: return {"ok":False,"error":"integration_disabled","correlation_id":correlation_id}
        token = self.credentials.get_automations_bearer_token(integration)
        if not token: return {"ok":False,"error":"credential_not_configured","correlation_id":correlation_id}
        url=f"{integration.automations_base_url.rstrip('/')}/internal/v1/studios/{integration.automations_studio_id}/{suffix}"
        headers={"Authorization":f"Bearer {token}","X-MyFit-Event-Version":SCHEMA_VERSION,
                 "X-Correlation-ID":correlation_id,"Accept":"application/json"}
        if idempotency_key: headers["Idempotency-Key"]=idempotency_key
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0,connect=4.0),transport=self.transport) as client:
                response=client.request(method,url,json=payload,headers=headers)
            try: body=response.json()
            except ValueError: body={}
            return {"ok":200<=response.status_code<300,"http_status":response.status_code,
                "correlation_id":body.get("correlation_id",correlation_id),"evaluation_id":body.get("evaluation_id"),
                "runs_created":body.get("runs_created",0),"runs_reused":body.get("runs_reused",0),
                "error":body.get("error") or (None if response.status_code<300 else f"http_{response.status_code}")}
        except httpx.RequestError:
            return {"ok":False,"error":"automations_unavailable","correlation_id":correlation_id}


class OutboxService:
    def __init__(self, client: AutomationsClient | None = None): self.client=client or AutomationsClient()

    def enqueue(self, db: Session, integration, fact: Fact):
        from app.models import AutomationsDelivery
        existing=db.query(AutomationsDelivery).filter_by(analytics_studio_id=integration.analytics_studio_id,
                                                          idempotency_key=fact.idempotency_key).first()
        if existing: return existing,False
        item=AutomationsDelivery(analytics_studio_id=integration.analytics_studio_id,integration_id=integration.id,
            automations_studio_id=integration.automations_studio_id,event_type=fact.payload["event_type"],
            subject_type=fact.payload["subject_type"],subject_id=fact.payload["subject_id"],
            correlation_id=str(uuid.uuid4()),idempotency_key=fact.idempotency_key,
            payload_fingerprint=payload_fingerprint(fact.payload),normalized_payload=_canonical(fact.payload),
            delivery_status="pending",attempt_count=0)
        db.add(item)
        try: db.commit(); db.refresh(item); return item,True
        except IntegrityError:
            db.rollback(); return db.query(AutomationsDelivery).filter_by(
                analytics_studio_id=integration.analytics_studio_id,idempotency_key=fact.idempotency_key).one(),False

    def retry(self, db: Session, item):
        if item.delivery_status != "failed": raise ValueError("Only failed outbox items can be retried")
        transition(item,"pending"); item.next_retry_at=None; db.commit(); return item

    def deliver(self, db: Session, integration, item):
        from app.models import AutomationsDelivery, AutomationsDeliveryAttempt
        item=db.query(AutomationsDelivery).filter_by(id=item.id,analytics_studio_id=integration.analytics_studio_id).with_for_update().one()
        if item.delivery_status=="delivered": return item
        if item.delivery_status!="pending": raise ValueError("Outbox item is not pending")
        transition(item,"delivering"); db.commit()
        result=self.client.send(integration,json.loads(item.normalized_payload),item.idempotency_key,item.correlation_id)
        item=db.query(AutomationsDelivery).filter_by(id=item.id,analytics_studio_id=integration.analytics_studio_id).with_for_update().one()
        item.attempt_count+=1; item.last_attempt_at=datetime.now(timezone.utc); item.http_status=result.get("http_status")
        item.safe_error_code=result.get("error"); item.evaluation_id=result.get("evaluation_id") or item.evaluation_id
        item.runs_created=result.get("runs_created",0); item.runs_reused=result.get("runs_reused",0)
        target="delivered" if result["ok"] else "failed"; transition(item,target)
        if result["ok"]: item.delivered_at=datetime.now(timezone.utc); item.safe_error_code=None
        db.add(AutomationsDeliveryAttempt(delivery_id=item.id,attempt_number=item.attempt_count,
            correlation_id=item.correlation_id,result=target,http_status=item.http_status,safe_error_code=item.safe_error_code))
        db.commit(); db.refresh(item); return item

    def deliver_pending(self, db: Session, integration, limit: int = OUTBOX_BATCH_SIZE):
        from app.models import AutomationsDelivery
        items=db.query(AutomationsDelivery).filter_by(analytics_studio_id=integration.analytics_studio_id,
            integration_id=integration.id,delivery_status="pending").order_by(AutomationsDelivery.created_at).limit(min(limit,OUTBOX_BATCH_SIZE)).all()
        return [self.deliver(db,integration,item) for item in items]
