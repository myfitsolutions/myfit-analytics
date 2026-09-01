from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker


from app.config import settings


DATABASE_CONNECT_TIMEOUT_SECONDS = 8


def database_engine_options(database_url):
    options = {"pool_pre_ping": True}
    if make_url(database_url).get_backend_name() == "postgresql":
        options["connect_args"] = {
            "connect_timeout": DATABASE_CONNECT_TIMEOUT_SECONDS
        }
    return options


engine = create_engine(
    settings.database_url,
    **database_engine_options(settings.database_url)
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()
