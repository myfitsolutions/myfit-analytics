import getpass

from sqlalchemy.exc import SQLAlchemyError

from app.auth import hash_password, normalize_email
from app.database import SessionLocal
from app.models import Studio, User


def main():
    try:
        studio_id = int(input("Studio ID: ").strip())
    except ValueError:
        print("Studio ID must be a number.")
        return

    email = normalize_email(input("Email: "))
    role = input("Role (owner/manager/staff): ").strip().lower()

    if role not in {"owner", "manager", "staff"}:
        print("Role must be owner, manager, or staff.")
        return

    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")

    if password != confirmation:
        print("Passwords do not match.")
        return

    try:
        password_hash = hash_password(password)
    except ValueError as error:
        print(str(error))
        return

    db = SessionLocal()

    try:
        if not db.query(Studio).filter(Studio.id == studio_id).first():
            print("Studio not found.")
            return

        if db.query(User).filter(User.email == email).first():
            print("Email is already registered.")
            return

        db.add(User(
            studio_id=studio_id,
            email=email,
            password_hash=password_hash,
            role=role,
            is_active=True
        ))
        db.commit()
        print("User created successfully.")
    except SQLAlchemyError:
        db.rollback()
        print("User could not be created.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
