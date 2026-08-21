import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from app.config import settings


class EmailServiceError(Exception):
    """Base exception for controlled email service failures."""


class EmailConfigurationError(EmailServiceError):
    """Raised when SMTP configuration is missing or invalid."""


class EmailSendError(EmailServiceError):
    """Raised when the SMTP provider cannot send an email."""


def _safe_exception_message(error):
    message = str(error)

    for variable in (
        "SMTP_PASSWORD",
        "OPENAI_API_KEY",
        "SMTP_USERNAME",
        "EMAIL_FROM"
    ):
        sensitive_value = {
            "SMTP_PASSWORD": settings.smtp_password,
            "OPENAI_API_KEY": settings.openai_api_key,
            "SMTP_USERNAME": settings.smtp_username,
            "EMAIL_FROM": settings.email_from
        }[variable]

        if sensitive_value:
            message = message.replace(sensitive_value, "[redacted]")

    return message


def _get_smtp_config():
    required_variables = (
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "EMAIL_FROM"
    )
    values = dict(zip(required_variables, (
        settings.smtp_host, settings.smtp_port, settings.smtp_username,
        settings.smtp_password, settings.email_from
    )))

    if any(not value for value in values.values()):
        raise EmailConfigurationError(
            "Email service is not configured"
        )

    try:
        port = int(values["SMTP_PORT"])
    except (TypeError, ValueError):
        raise EmailConfigurationError(
            "Email service configuration is invalid"
        )

    if not 1 <= port <= 65535:
        raise EmailConfigurationError(
            "Email service configuration is invalid"
        )

    use_tls_value = settings.smtp_use_tls.strip().lower()
    use_ssl_value = settings.smtp_use_ssl.strip().lower()

    if (
        use_tls_value not in {"true", "false"}
        or use_ssl_value not in {"true", "false"}
    ):
        raise EmailConfigurationError(
            "Email service configuration is invalid"
        )

    use_tls = use_tls_value == "true"
    use_ssl = use_ssl_value == "true"

    if use_tls and use_ssl:
        raise EmailConfigurationError(
            "SMTP SSL and STARTTLS cannot both be enabled"
        )

    return {
        "host": values["SMTP_HOST"],
        "port": port,
        "username": values["SMTP_USERNAME"],
        "password": values["SMTP_PASSWORD"],
        "from_email": values["EMAIL_FROM"],
        "use_tls": use_tls,
        "use_ssl": use_ssl
    }


def send_email(to_email, subject, body, sender_name=None):
    config = _get_smtp_config()
    email = EmailMessage()
    email["From"] = (
        formataddr((sender_name, config["from_email"]))
        if sender_name
        else config["from_email"]
    )
    email["To"] = to_email
    email["Subject"] = subject
    email.set_content(body)

    try:
        if config["use_ssl"]:
            smtp_connection = smtplib.SMTP_SSL(
                config["host"],
                config["port"],
                timeout=15,
                context=ssl.create_default_context()
            )
        else:
            smtp_connection = smtplib.SMTP(
                config["host"],
                config["port"],
                timeout=15
            )

        with smtp_connection as smtp:
            smtp.ehlo()

            if config["use_tls"]:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()

            smtp.login(
                config["username"],
                config["password"]
            )
            smtp.send_message(email)
    except (
        OSError,
        smtplib.SMTPException,
        TimeoutError,
        ValueError
    ) as error:
        print(f"{type(error).__name__}: {_safe_exception_message(error)}")
        raise EmailSendError("Email could not be sent") from error
