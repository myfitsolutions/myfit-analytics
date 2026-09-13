"""Durable, operator-controlled Analytics facts delivery boundary."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Protocol, TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
import anyio
from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models import AutomationsDelivery, AutomationsIntegration, Member, Payment

SOURCE = "myfit_analytics"
SCHEMA_VERSION = "1"
MEMBER_IDENTITY_VERSION = "member-fact-v3"
PAYMENT_IDENTITY_VERSION = "payment-fact-v2"
OUTBOX_BATCH_SIZE = 3
MAX_ATTEMPTS = 5
# A request is abandoned after 12s of monotonic wall time; one in-progress
# HTTPX read may consume another 2s before that check runs. The 120s lease
# leaves ample room for result persistence and scheduling jitter.
HTTP_CONNECT_TIMEOUT_SECONDS = 2
HTTP_IO_TIMEOUT_SECONDS = 2
HTTP_TOTAL_BUDGET_SECONDS = 12
HTTP_MAX_REQUEST_BYTES = 16384
HTTP_MAX_RESPONSE_BYTES = 8192
PERSISTENCE_MARGIN_SECONDS = 30
CLAIM_SECONDS = 120
assert CLAIM_SECONDS > HTTP_TOTAL_BUDGET_SECONDS + HTTP_IO_TIMEOUT_SECONDS + PERSISTENCE_MARGIN_SECONDS
BATCH_STOP_CODES = frozenset({"automations_auth_failed","automations_permission_denied",
    "automations_configuration_invalid","automations_rate_limited","automations_unavailable",
    "credential_not_configured","integration_disabled"})
LEGAL_TRANSITIONS = {"pending":{"delivering"}, "delivering":{"delivered","failed"}, "failed":{"pending"}, "delivered":set()}
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{1,36}\Z")


@dataclass(frozen=True)
class Fact:
    payload: dict[str, Any]
    idempotency_key: str
    target_revision: int | None = None


class CredentialProvider(Protocol):
    def get_automations_bearer_token(self, integration: AutomationsIntegration) -> str | None: ...


class EnvironmentCredentialProvider:
    def get_automations_bearer_token(self, integration: AutomationsIntegration) -> str | None:
        # There is deliberately no global fallback: each mapping selects its reference.
        reference = (integration.credential_env_var or "").strip()
        return os.getenv(reference) or None if reference else None


def _iso(value: datetime) -> str:
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_fingerprint(payload: dict) -> str:
    return hashlib.sha256(_canonical({"schema_version": SCHEMA_VERSION, "event": payload}).encode()).hexdigest()


def _identity(studio_id: int, event_type: str, subject_id: str, fact_at: datetime, facts: dict,
              *, version: str | None = None) -> str:
    identity={"studio":studio_id,"event_type":event_type,"subject":subject_id,
              "fact_at":_iso(fact_at),"facts":facts}
    if version: identity["identity_version"]=version
    canonical = _canonical(identity)
    return "mfa-analytics-" + hashlib.sha256(canonical.encode()).hexdigest()


def member_fact(member: Member, automations_studio_id: str, evaluation_at: datetime, *, reactivation: bool,
                target_origin: str | None = None, target_revision: int = 1) -> Fact | None:
    if member.last_visit_at is None or member.status not in {"active","inactive","lapsed"}: return None
    active = member.status == "active"
    if reactivation == active: return None
    event_type = "member_status_snapshot" if reactivation else "member_activity_snapshot"
    facts = {"member_active":active,"last_attendance_at":_iso(member.last_visit_at)}
    payload = {"event_type":event_type,"studio_id":automations_studio_id,"subject_type":"member",
        "subject_id":str(member.id),"occurred_at":_iso(evaluation_at),"evaluation_at":_iso(evaluation_at),
        "source":SOURCE,"source_reference":f"analytics:member:{member.id}:{event_type}","payload":facts}
    return Fact(payload,_identity(member.studio_id,event_type,str(member.id),member.last_visit_at,
        {**facts,"member_status":member.status,"target_origin":target_origin or "",
         "target_studio_id":automations_studio_id,"target_revision":target_revision},
        version=MEMBER_IDENTITY_VERSION),target_revision)


def payment_fact(payment: Payment, automations_studio_id: str, evaluation_at: datetime,
                 *, target_origin: str | None = None, target_revision: int = 1) -> Fact | None:
    if payment.status not in {"failed","declined","unpaid"} or payment.payment_date is None: return None
    facts = {"resolved":False,"failed_at":_iso(payment.payment_date),"member_id":str(payment.member_id)}
    payload = {"event_type":"payment_failure","studio_id":automations_studio_id,"subject_type":"payment",
        "subject_id":str(payment.id),"occurred_at":_iso(payment.payment_date),"evaluation_at":_iso(evaluation_at),
        "source":SOURCE,"source_reference":f"analytics:payment:{payment.id}","payload":facts}
    return Fact(payload,_identity(payment.studio_id,"payment_failure",str(payment.id),payment.payment_date,
        {**facts,"target_origin":target_origin or "","target_studio_id":automations_studio_id,
         "target_revision":target_revision},version=PAYMENT_IDENTITY_VERSION),target_revision)


def transition(item: AutomationsDelivery, target: str) -> None:
    if target not in LEGAL_TRANSITIONS.get(item.delivery_status, set()):
        raise ValueError(f"Illegal outbox transition: {item.delivery_status} -> {target}")
    item.delivery_status = target


def _valid_origin(value: str) -> str | None:
    if not isinstance(value, str) or len(value) > 253 or value != value.strip(): return None
    if any(character in value for character in "?#@\\%") or any(character.isspace() or ord(character)<32 for character in value): return None
    try: parts = urlsplit(value)
    except ValueError: return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password: return None
    if parts.path or parts.query or parts.fragment or parts.hostname.endswith("."): return None
    try: port = parts.port
    except ValueError: return None
    if port not in (None, 443): return None
    host = parts.hostname
    if host != host.lower() or host in {"localhost", "localhost.localdomain"}: return None
    try: ipaddress.ip_address(host); return None
    except ValueError: pass
    labels = host.split(".")
    if len(labels) < 2 or labels[-1] in {"local", "internal", "localhost"}: return None
    if any(not _HOST_LABEL.fullmatch(label) for label in labels): return None
    if parts.netloc != host and parts.netloc != f"{host}:443": return None
    return f"https://{host}"


def allowed_automations_origins() -> frozenset[str]:
    raw = os.getenv("MYFIT_AUTOMATIONS_ALLOWED_ORIGINS", "")
    if not raw or len(raw) > 4096: return frozenset()
    parts = raw.split(",")
    origins = [_valid_origin(part.strip()) for part in parts]
    return frozenset(origins) if all(origins) else frozenset()


def validated_automations_origin(value: str) -> str | None:
    origin = _valid_origin(value)
    return origin if origin and origin in allowed_automations_origins() else None


def valid_target_studio_id(value: str) -> bool:
    return isinstance(value, str) and bool(_OPAQUE.fullmatch(value))


def _safe_receiver_result(status: int, body_bytes: bytes, sent_correlation: str) -> dict:
    if status != 200:
        code = {401:"automations_auth_failed",403:"automations_permission_denied",
            429:"automations_rate_limited"}.get(status)
        if code is None: code = "automations_unavailable" if status >= 500 else "automations_status_unexpected"
        return {"ok":False,"http_status":status,"error":code}
    try: body = json.loads(body_bytes)
    except (ValueError, UnicodeDecodeError): return {"ok":False,"http_status":status,"error":"automations_response_invalid"}
    if not isinstance(body, dict): return {"ok":False,"http_status":status,"error":"automations_response_invalid"}
    correlation = body.get("correlation_id", sent_correlation)
    evaluation = body.get("evaluation_id")
    created = body.get("runs_created", 0)
    reused = body.get("runs_reused", 0)
    if (not isinstance(correlation, str) or not _OPAQUE.fullmatch(correlation)
        or not isinstance(evaluation, str) or not _OPAQUE.fullmatch(evaluation)
        or type(created) is not int or type(reused) is not int
        or not 0 <= created <= 100000 or not 0 <= reused <= 100000):
        return {"ok":False,"http_status":status,"error":"automations_response_invalid"}
    return {"ok":True,"http_status":status,"correlation_id":correlation,
        "evaluation_id":evaluation,"runs_created":created,"runs_reused":reused,"error":None}


class AutomationsClient:
    def __init__(self, transport=None, credential_provider: CredentialProvider | None = None):
        self.transport = transport
        self.credentials = credential_provider or EnvironmentCredentialProvider()

    def connection(self, integration, correlation_id: str | None = None) -> dict:
        return self._request(integration,"GET","connection",None,None,correlation_id)

    def send(self, integration, payload: dict, idempotency_key: str, correlation_id: str) -> dict:
        return self._request(integration,"POST","events",payload,idempotency_key,correlation_id)

    def _request(self,integration,method,suffix,payload,idempotency_key,correlation_id):
        correlation_id = correlation_id if isinstance(correlation_id,str) and _OPAQUE.fullmatch(correlation_id) else str(uuid.uuid4())
        if not integration.integration_enabled: return {"ok":False,"error":"integration_disabled"}
        origin = validated_automations_origin(integration.automations_base_url)
        if not origin or not valid_target_studio_id(integration.automations_studio_id):
            return {"ok":False,"error":"automations_configuration_invalid"}
        token = self.credentials.get_automations_bearer_token(integration)
        if not token: return {"ok":False,"error":"credential_not_configured"}
        url=f"{origin}/internal/v1/studios/{integration.automations_studio_id}/{suffix}"
        headers={"Authorization":f"Bearer {token}","X-MyFit-Event-Version":SCHEMA_VERSION,
                 "X-Correlation-ID":correlation_id,"Accept":"application/json"}
        if idempotency_key: headers["Idempotency-Key"]=idempotency_key
        if payload is not None and len(_canonical(payload).encode("utf-8")) > HTTP_MAX_REQUEST_BYTES:
            return {"ok":False,"error":"automations_payload_invalid"}

        async def perform_request():
            deadline=time.monotonic()+HTTP_TOTAL_BUDGET_SECONDS
            with anyio.fail_after(HTTP_TOTAL_BUDGET_SECONDS):
                async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_IO_TIMEOUT_SECONDS,
                                  connect=HTTP_CONNECT_TIMEOUT_SECONDS),transport=self.transport,
                                  follow_redirects=False,trust_env=False) as client:
                    async with client.stream(method,url,json=payload,headers=headers) as response:
                        if time.monotonic()>deadline:
                            return {"ok":False,"error":"automations_unavailable"}
                        status=response.status_code
                        if suffix == "connection" and status == 200:
                            return {"ok":True,"http_status":200}
                        if status != 200:
                            return _safe_receiver_result(status,b"",correlation_id)
                        chunks=[]; size=0
                        async for chunk in response.aiter_bytes(chunk_size=4096):
                            size+=len(chunk)
                            if size>HTTP_MAX_RESPONSE_BYTES:
                                return {"ok":False,"http_status":status,"error":"automations_response_invalid"}
                            if time.monotonic()>deadline:
                                return {"ok":False,"error":"automations_unavailable"}
                            chunks.append(chunk)
                        if time.monotonic()>deadline:
                            return {"ok":False,"error":"automations_unavailable"}
                        return _safe_receiver_result(status,b"".join(chunks),correlation_id)

        try:
            return anyio.run(perform_request)
        except TimeoutError:
            return {"ok":False,"error":"automations_unavailable"}
        except httpx.RequestError:
            return {"ok":False,"error":"automations_unavailable"}
        except Exception:
            return {"ok":False,"error":"automations_client_error"}


class OutboxService:
    def __init__(self, client: AutomationsClient | None = None): self.client=client or AutomationsClient()

    def enqueue(self, db: Session, integration, fact: Fact):
        from app.models import AutomationsDelivery, AutomationsIntegration
        locked=db.query(AutomationsIntegration).filter_by(id=integration.id,
                analytics_studio_id=integration.analytics_studio_id).populate_existing().with_for_update().one_or_none()
        if locked is None:
            raise ValueError("Integration studio mismatch")
        if fact.payload.get("studio_id") != locked.automations_studio_id:
            raise ValueError("Fact studio mismatch")
        if fact.target_revision is not None and fact.target_revision != locked.target_revision:
            raise ValueError("Fact target revision mismatch")
        existing=db.query(AutomationsDelivery).filter_by(analytics_studio_id=integration.analytics_studio_id,
                                                          idempotency_key=fact.idempotency_key).first()
        if existing:
            if existing.delivery_status == "cancelled" or existing.automations_studio_id != locked.automations_studio_id:
                db.rollback()
                raise ValueError("Existing fact belongs to a changed target")
            db.commit()
            return existing,False
        item=AutomationsDelivery(analytics_studio_id=locked.analytics_studio_id,integration_id=locked.id,
            automations_studio_id=locked.automations_studio_id,event_type=fact.payload["event_type"],
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
        from app.models import AutomationsDelivery
        current=db.query(AutomationsDelivery).filter_by(id=item.id,analytics_studio_id=item.analytics_studio_id,
            integration_id=item.integration_id).with_for_update().one()
        if current.delivery_status != "failed" or current.attempt_count >= MAX_ATTEMPTS:
            raise ValueError("Delivery is not retryable")
        transition(current,"pending"); current.next_retry_at=None; db.commit(); return current

    def deliver(self, db: Session, integration, item):
        if item.analytics_studio_id != integration.analytics_studio_id or item.integration_id != integration.id:
            raise ValueError("Delivery studio mismatch")
        item_id=item.id
        claim=self._claim(db,integration,item_id)
        if claim is None:
            return None
        token,snapshot=claim
        request_integration=SimpleNamespace(id=integration.id,
            analytics_studio_id=integration.analytics_studio_id,
            integration_enabled=integration.integration_enabled,
            automations_base_url=integration.automations_base_url,
            automations_studio_id=integration.automations_studio_id,
            credential_env_var=integration.credential_env_var)
        db.commit()
        try:
            result=self.client.send(request_integration,json.loads(snapshot["payload"]),
                snapshot["key"],snapshot["correlation"])
        except Exception:
            result={"ok":False,"error":"automations_client_error"}
        return self._persist(db,request_integration,item_id,token,result)

    def _claim(self, db: Session, integration, item_id: int):
        from app.models import AutomationsDelivery
        now=datetime.now(timezone.utc); token=str(uuid.uuid4())
        eligible=or_(AutomationsDelivery.delivery_status=="pending",
            and_(AutomationsDelivery.delivery_status=="delivering",
                 AutomationsDelivery.claim_expires_at < now))
        result=db.execute(update(AutomationsDelivery).where(
            AutomationsDelivery.id==item_id,
            AutomationsDelivery.analytics_studio_id==integration.analytics_studio_id,
            AutomationsDelivery.integration_id==integration.id,
            AutomationsDelivery.automations_studio_id==integration.automations_studio_id,
            AutomationsDelivery.attempt_count<MAX_ATTEMPTS, eligible,
            or_(AutomationsDelivery.next_retry_at.is_(None),AutomationsDelivery.next_retry_at<=now),
        ).values(delivery_status="delivering",claim_token=token,
            claim_expires_at=now+timedelta(seconds=CLAIM_SECONDS)))
        db.commit()
        if result.rowcount != 1: return None
        item=db.query(AutomationsDelivery).filter_by(id=item_id,
            analytics_studio_id=integration.analytics_studio_id,integration_id=integration.id,
            claim_token=token).one()
        snapshot={"payload":item.normalized_payload,"key":item.idempotency_key,
            "correlation":item.correlation_id}
        db.commit()
        return token,snapshot

    def _persist(self, db: Session, integration, item_id: int, token: str, result: dict):
        from app.models import AutomationsDelivery, AutomationsDeliveryAttempt
        item=db.query(AutomationsDelivery).filter_by(id=item_id,
            analytics_studio_id=integration.analytics_studio_id,integration_id=integration.id,
            claim_token=token,delivery_status="delivering").with_for_update().one_or_none()
        if item is None: db.rollback(); return None
        item.attempt_count+=1; item.last_attempt_at=datetime.now(timezone.utc)
        item.http_status=result.get("http_status")
        item.safe_error_code=result.get("error")
        item.claim_token=None; item.claim_expires_at=None
        if result.get("ok"):
            item.evaluation_id=result["evaluation_id"]
            item.runs_created=result["runs_created"]; item.runs_reused=result["runs_reused"]
            transition(item,"delivered"); item.delivered_at=datetime.now(timezone.utc)
            item.safe_error_code=None; item.next_retry_at=None
        else:
            transition(item,"failed")
            item.next_retry_at=datetime.now(timezone.utc)+timedelta(seconds=min(300,2**item.attempt_count))
        db.add(AutomationsDeliveryAttempt(delivery_id=item.id,attempt_number=item.attempt_count,
            correlation_id=item.correlation_id,result=item.delivery_status,
            http_status=item.http_status,safe_error_code=item.safe_error_code))
        try: db.commit(); db.refresh(item); return item
        except Exception:
            db.rollback()
            return None

    def deliver_pending(self, db: Session, integration, limit: int = OUTBOX_BATCH_SIZE):
        from app.models import AutomationsDelivery
        now=datetime.now(timezone.utc)
        items=db.query(AutomationsDelivery).filter_by(analytics_studio_id=integration.analytics_studio_id,
            integration_id=integration.id).filter(AutomationsDelivery.attempt_count<MAX_ATTEMPTS,
            or_(AutomationsDelivery.delivery_status=="pending",
                and_(AutomationsDelivery.delivery_status=="delivering",
                     AutomationsDelivery.claim_expires_at<now))).order_by(
                AutomationsDelivery.created_at,AutomationsDelivery.id).limit(min(limit,OUTBOX_BATCH_SIZE)).all()
        db.commit()
        results=[]
        for item in items:
            result=self.deliver(db,integration,item)
            if result is None: break
            results.append(result)
            if result.safe_error_code in BATCH_STOP_CODES: break
        return results
