import os
import secrets
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def parse_bool(name, default):
    value = os.getenv(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    session_secret: str
    session_cookie_secure: bool
    openai_api_key: str | None
    smtp_host: str | None
    smtp_port: str | None
    smtp_username: str | None
    smtp_password: str | None
    email_from: str | None
    smtp_use_ssl: str
    smtp_use_tls: str

    @property
    def is_production(self):
        return self.app_env == "production"


def load_settings():
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    if app_env not in {"development", "production"}:
        raise ValueError("APP_ENV must be development or production")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    session_secret = os.getenv("SESSION_SECRET", "")
    cookie_secure = parse_bool("SESSION_COOKIE_SECURE", "false")
    if app_env == "production":
        if len(session_secret) < 32 or session_secret.lower() in {
            "changeme", "change-me", "secret", "development", "password"
        }:
            raise ValueError("Production SESSION_SECRET must be a strong value of at least 32 characters")
        if not cookie_secure:
            raise ValueError("SESSION_COOKIE_SECURE must be true in production")
    elif not session_secret:
        session_secret = secrets.token_urlsafe(48)
        print("WARNING: SESSION_SECRET is not configured; using an ephemeral development secret.")
    return Settings(
        app_env=app_env,
        database_url=database_url,
        session_secret=session_secret,
        session_cookie_secure=cookie_secure,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        smtp_host=os.getenv("SMTP_HOST") or None,
        smtp_port=os.getenv("SMTP_PORT") or None,
        smtp_username=os.getenv("SMTP_USERNAME") or None,
        smtp_password=os.getenv("SMTP_PASSWORD") or None,
        email_from=os.getenv("EMAIL_FROM") or None,
        smtp_use_ssl=os.getenv("SMTP_USE_SSL", "false"),
        smtp_use_tls=os.getenv("SMTP_USE_TLS", "true")
    )


settings = load_settings()
