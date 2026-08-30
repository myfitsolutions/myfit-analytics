import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


MONEY_FIELDS = (
    "gross_revenue", "net_revenue", "tax", "discount",
    "admin_fee", "dishonour_fee", "transaction_fee",
)


def parse_money(value, required=False):
    text = (value or "").strip().replace(",", "").replace("$", "")
    if not text:
        if required: raise ValueError("Missing monetary value")
        return None
    negative_parentheses = text.startswith("(") and text.endswith(")")
    if negative_parentheses: text = f"-{text[1:-1]}"
    try: amount = Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError): raise ValueError("Invalid monetary value") from None
    if not amount.is_finite(): raise ValueError("Invalid monetary value")
    return amount


HAPANA_DATE_FORMATS = (
    "%d/%m/%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
)


def parse_revenue_date(value, default_timezone=timezone.utc):
    text = (value or "").strip()
    if not text: return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for date_format in HAPANA_DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, date_format)
                break
            except ValueError:
                continue
        if parsed is None: raise ValueError("Invalid date") from None
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=default_timezone)
    return parsed.astimezone(timezone.utc)


def transaction_kind(source_status, revenue_type):
    combined = f"{source_status or ''} {revenue_type or ''}".casefold()
    if "refund" in combined: return "refund"
    if "revenue" in combined or combined.strip(): return "revenue"
    return "other"


def revenue_identity(row):
    external_id = (row.get("external_transaction_id") or "").strip().casefold()
    if external_id: return f"tx:{external_id}"
    stable = "\x1f".join(str(row.get(key) or "").strip().casefold() for key in (
        "customer_email", "analytics_date", "source_status", "revenue_type",
        "transaction_category", "description", "gross_revenue", "net_revenue"
    ))
    return "fp:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()


def normalize_revenue_row(row):
    normalized = {key: (value or "").strip() for key, value in row.items()}
    normalized["gross_revenue"] = parse_money(normalized.get("gross_revenue")) or Decimal("0.00")
    normalized["net_revenue"] = parse_money(normalized.get("net_revenue"), required=True)
    for field in ("tax", "discount", "admin_fee", "dishonour_fee", "transaction_fee"):
        normalized[field] = parse_money(normalized.get(field))
    normalized["invoice_date"] = parse_revenue_date(normalized.get("invoice_date"))
    normalized["payment_date"] = parse_revenue_date(normalized.get("payment_date"))
    normalized["analytics_date"] = normalized["payment_date"] or normalized["invoice_date"]
    if normalized["analytics_date"] is None: raise ValueError("Payment Date or Invoice Date is required")
    normalized["transaction_kind"] = transaction_kind(normalized.get("source_status"), normalized.get("revenue_type"))
    if normalized["transaction_kind"] == "refund":
        if normalized["net_revenue"] > 0: normalized["net_revenue"] = -normalized["net_revenue"]
        if normalized["gross_revenue"] > 0: normalized["gross_revenue"] = -normalized["gross_revenue"]
    normalized["identity_key"] = revenue_identity(normalized)
    return normalized
