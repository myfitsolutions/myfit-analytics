from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal
from app.models import Studio


def main():
    name = input("Studio name: ").strip()
    timezone = input("Timezone [Asia/Manila]: ").strip() or "Asia/Manila"
    currency = (input("Currency [PHP]: ").strip() or "PHP").upper()
    if not name or len(name) > 150:
        print("Studio name must be between 1 and 150 characters."); return
    if len(currency) != 3 or not currency.isalpha():
        print("Currency must be a three-letter code."); return
    db = SessionLocal()
    try:
        studio = Studio(name=name, timezone=timezone, currency=currency)
        db.add(studio); db.commit(); db.refresh(studio)
        print(f"Studio created successfully. Studio ID: {studio.id}")
    except SQLAlchemyError:
        db.rollback(); print("Studio could not be created.")
    finally:
        db.close()


if __name__ == "__main__": main()
