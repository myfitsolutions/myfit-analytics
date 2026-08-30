PLATFORMS = {
    "hapana": {
        "name": "Hapana",
        "description": "Import exports from your Hapana account.",
        "profiles": {
            "revenue": {
                "v1": {
                    "version": 1,
                    "required": ["Net Revenue"],
                    "mapping": {
                        "Full Name": "customer_name", "Email": "customer_email",
                        "Payment Method": "payment_method", "Revenue Type": "revenue_type",
                        "Transaction Category": "transaction_category",
                        "Description": "description", "Transaction Id": "external_transaction_id",
                        "Processed By": "processed_by", "Sale Referred By": "sale_referred_by",
                        "Payment Status": "source_status", "Invoice Date": "invoice_date",
                        "Payment Date": "payment_date", "Admin Fee": "admin_fee",
                        "Dishonour Fee": "dishonour_fee", "Transaction Fee": "transaction_fee",
                        "Tax": "tax", "Discount": "discount",
                        "Gross Revenue": "gross_revenue", "Net Revenue": "net_revenue",
                    },
                    "aliases": {
                        "Customer Name": "customer_name",
                        "Customer Email": "customer_email",
                    },
                }
            }
        },
    },
    "bsport": {
        "name": "bsport",
        "description": "Import exports from your bsport account.",
        "profiles": {},
    },
    "other": {
        "name": "Other",
        "description": "Use CSV exports from another studio platform.",
        "profiles": {},
    },
}

PLATFORM_KEYS = tuple(PLATFORMS)


def get_platform(platform):
    return PLATFORMS.get(platform)


def get_import_profile(platform, import_type, version="v1"):
    definition = get_platform(platform)
    return definition and definition.get("profiles", {}).get(import_type, {}).get(version)
