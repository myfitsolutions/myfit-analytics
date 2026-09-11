"""Narrow GoHighLevel boundary for connection checks and contact upserts."""
from __future__ import annotations

import os
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

import httpx


GHL_API_BASE_URL = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "v3"
GHL_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
GHL_LOCATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
GHL_CONTACT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
CONTACT_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_GHL_LOG_LOCATION_PATTERN = re.compile(
    r"(https://services\.leadconnectorhq\.com/locations/)[^\s'\"]+"
)
_GHL_LOG_PATH_PATTERN = re.compile(r"(/locations/)[A-Za-z0-9_-]{1,100}")
_GHL_LOG_REDACTION_ACTIVE = ContextVar("ghl_log_redaction_active", default=False)


class _GhlLocationRedactionFilter(logging.Filter):
    """Redact only GHL location path segments while preserving unrelated HTTP logs."""

    def filter(self, record):
        if not _GHL_LOG_REDACTION_ACTIVE.get():
            return True
        message = record.getMessage()
        redacted = _GHL_LOG_LOCATION_PATTERN.sub(r"\1[redacted]", message)
        redacted = _GHL_LOG_PATH_PATTERN.sub(r"\1[redacted]", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def _install_location_log_redaction():
    for logger_name in ("httpx", "httpcore"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(item, _GhlLocationRedactionFilter) for item in logger.filters):
            logger.addFilter(_GhlLocationRedactionFilter())


_install_location_log_redaction()


@dataclass(frozen=True)
class GhlCredentials:
    token: str | None
    location_id: str | None


class CredentialProvider(Protocol):
    def resolve(self, integration) -> GhlCredentials: ...


class EnvironmentCredentialProvider:
    def resolve(self, integration) -> GhlCredentials:
        token_reference = (integration.token_env_var or "").strip()
        location_reference = (integration.location_env_var or "").strip()
        token = os.getenv(token_reference, "").strip() if token_reference else ""
        location_id = os.getenv(location_reference, "") if location_reference else ""
        return GhlCredentials(token=token or None, location_id=location_id or None)


SAFE_ERROR_MESSAGES = {
    "integration_disabled": "The GoHighLevel integration is disabled.",
    "token_not_configured": "The GoHighLevel token is not configured.",
    "location_not_configured": "The GoHighLevel location is not configured.",
    "location_invalid": "The GoHighLevel location configuration is invalid.",
    "authentication_failed": "GoHighLevel rejected the configured credentials.",
    "access_forbidden": "The configured credentials cannot access this GoHighLevel location.",
    "location_not_found": "The configured GoHighLevel location was not found.",
    "rate_limited": "GoHighLevel is temporarily rate limiting connection tests.",
    "invalid_response": "GoHighLevel returned an invalid response.",
    "ghl_unavailable": "GoHighLevel is temporarily unavailable.",
    "contact_conflict": "GoHighLevel could not safely upsert this contact.",
    "contact_invalid": "GoHighLevel rejected the contact data.",
    "sync_interrupted": "The contact synchronization was interrupted and can be retried.",
    "upstream_status_unexpected": "GoHighLevel returned an unexpected success status.",
    "upstream_json_invalid": "GoHighLevel returned unreadable confirmation data.",
    "upstream_contact_missing": "GoHighLevel did not return the expected contact confirmation.",
    "upstream_contact_id_invalid": "GoHighLevel returned an invalid contact confirmation identifier.",
    "result_persistence_failed": "The synchronization result could not be saved and can be retried.",
}


def safe_error_message(code: str | None) -> str | None:
    return SAFE_ERROR_MESSAGES.get(code) if code else None


class GhlClient:
    """A fixed-host client limited to connection checks and contact upserts."""

    def __init__(self, transport=None, credentials: CredentialProvider | None = None):
        self.transport = transport
        self.credentials = credentials or EnvironmentCredentialProvider()

    def test_connection(self, integration) -> dict[str, object]:
        if not integration.integration_enabled:
            return {"ok": False, "error": "integration_disabled"}
        credentials = self.credentials.resolve(integration)
        if not credentials.token:
            return {"ok": False, "error": "token_not_configured"}
        if not credentials.location_id:
            return {"ok": False, "error": "location_not_configured"}
        if not GHL_LOCATION_ID_PATTERN.fullmatch(credentials.location_id):
            return {"ok": False, "error": "location_invalid"}

        log_redaction_token = _GHL_LOG_REDACTION_ACTIVE.set(True)
        try:
            with httpx.Client(
                base_url=GHL_API_BASE_URL,
                timeout=GHL_TIMEOUT,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = client.get(
                    f"/locations/{credentials.location_id}",
                    headers={
                        "Authorization": f"Bearer {credentials.token}",
                        "Version": GHL_API_VERSION,
                        "Accept": "application/json",
                    },
                )
        except (httpx.TimeoutException, httpx.RequestError):
            return {"ok": False, "error": "ghl_unavailable"}
        finally:
            _GHL_LOG_REDACTION_ACTIVE.reset(log_redaction_token)

        error_by_status = {
            401: "authentication_failed",
            403: "access_forbidden",
            404: "location_not_found",
            429: "rate_limited",
        }
        if response.status_code in error_by_status:
            return {"ok": False, "error": error_by_status[response.status_code]}
        if not 200 <= response.status_code < 300:
            return {"ok": False, "error": "ghl_unavailable"}
        try:
            payload = response.json()
        except ValueError:
            return {"ok": False, "error": "invalid_response"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "invalid_response"}
        return {"ok": True, "error": None}

    def upsert_contact(self, integration, contact: dict[str, str | None]) -> dict[str, object]:
        if not integration.integration_enabled:
            return {"ok": False, "error": "integration_disabled"}
        credentials = self.credentials.resolve(integration)
        if not credentials.token:
            return {"ok": False, "error": "token_not_configured"}
        if not credentials.location_id:
            return {"ok": False, "error": "location_not_configured"}
        if not GHL_LOCATION_ID_PATTERN.fullmatch(credentials.location_id):
            return {"ok": False, "error": "location_invalid"}
        payload = build_contact_payload(contact, credentials.location_id)
        if payload is None:
            return {"ok": False, "error": "contact_invalid"}

        log_redaction_token = _GHL_LOG_REDACTION_ACTIVE.set(True)
        try:
            with httpx.Client(
                base_url=GHL_API_BASE_URL,
                timeout=GHL_TIMEOUT,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    "/contacts/upsert",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {credentials.token}",
                        "Version": GHL_API_VERSION,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                )
        except (httpx.TimeoutException, httpx.RequestError):
            return {"ok": False, "error": "ghl_unavailable"}
        finally:
            _GHL_LOG_REDACTION_ACTIVE.reset(log_redaction_token)

        error_by_status = {
            400: "contact_invalid", 401: "authentication_failed", 403: "access_forbidden",
            404: "location_not_found", 409: "contact_conflict", 422: "contact_invalid",
            429: "rate_limited",
        }
        if response.status_code in error_by_status:
            return {"ok": False, "error": error_by_status[response.status_code]}
        if response.status_code >= 500:
            return {"ok": False, "error": "ghl_unavailable"}
        if response.status_code not in {200, 201}:
            return {"ok": False, "error": "upstream_status_unexpected"}
        try:
            body = response.json()
        except ValueError:
            return {"ok": False, "error": "upstream_json_invalid"}
        if not isinstance(body, dict) or not isinstance(body.get("new"), bool):
            return {"ok": False, "error": "upstream_contact_missing"}
        # The published v3 contract documents HTTP 200 for both outcomes. GHL's
        # create path is also observed returning HTTP 201; accept that narrowly
        # only when the documented `new` discriminator confirms creation.
        if response.status_code == 201 and body["new"] is not True:
            return {"ok": False, "error": "upstream_status_unexpected"}
        contact_body = body.get("contact")
        if not isinstance(contact_body, dict):
            return {"ok": False, "error": "upstream_contact_missing"}
        contact_id = contact_body.get("id") if isinstance(contact_body, dict) else None
        if not isinstance(contact_id, str) or not GHL_CONTACT_ID_PATTERN.fullmatch(contact_id):
            return {"ok": False, "error": "upstream_contact_id_invalid"}
        return {"ok": True, "error": None, "ghl_contact_id": contact_id}


def normalize_contact_email(value: object) -> str | None:
    email = str(value or "").strip().casefold()
    return email if len(email) <= 320 and CONTACT_EMAIL_PATTERN.fullmatch(email) else None


def normalize_contact_phone(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    digits = re.sub(r"[^0-9]", "", raw)
    if not 7 <= len(digits) <= 15:
        return None
    return f"+{digits}" if raw.startswith("+") else digits


def build_contact_payload(contact: dict[str, str | None], location_id: str) -> dict[str, object] | None:
    if not GHL_LOCATION_ID_PATTERN.fullmatch(location_id):
        return None
    email = normalize_contact_email(contact.get("email"))
    phone = normalize_contact_phone(contact.get("phone"))
    if not email and not phone:
        return None
    payload: dict[str, object] = {
        "locationId": location_id,
        "createNewIfDuplicateAllowed": False,
    }
    first_name = str(contact.get("first_name") or "").strip()[:150]
    last_name = str(contact.get("last_name") or "").strip()[:150]
    if first_name:
        payload["firstName"] = first_name
    if last_name:
        payload["lastName"] = last_name
    if email:
        payload["email"] = email
    if phone:
        payload["phone"] = phone
    return payload
