from fastapi import Depends, HTTPException, Request
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User


password_hasher = PasswordHasher()

ROLE_PERMISSIONS = {
    "owner": {
        "team_manage",
        "settings_write",
        "email_send",
        "action_status_write",
        "member_import",
        "booking_import",
        "payment_import",
        "import_history"
        ,"data_source_write"
    },
    "manager": {
        "email_send",
        "action_status_write",
        "member_import",
        "booking_import",
        "payment_import",
        "import_history"
        ,"data_source_write"
    },
    "staff": set()
}


def normalize_email(email):
    return email.strip().casefold()


def validate_password(password):
    if not 10 <= len(password) <= 128:
        raise ValueError("Password must be between 10 and 128 characters")


def hash_password(password):
    validate_password(password)
    return password_hasher.hash(password)


def verify_password(password, password_hash):
    try:
        return password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def get_current_user(request: Request, db: Session):
    user_id = request.session.get("user_id")

    if not isinstance(user_id, int):
        return None

    return (
        db.query(User)
        .filter(User.id == user_id, User.is_active.is_(True))
        .first()
    )


def require_current_user(
    request: Request,
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)

    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    return user


def require_studio_user(
    studio_id: int,
    current_user: User = Depends(require_current_user)
):
    if current_user.studio_id != studio_id:
        raise HTTPException(status_code=403, detail="Studio access forbidden")

    return current_user


def require_studio_permission(permission):
    def authorize(
        studio_id: int,
        current_user: User = Depends(require_current_user)
    ):
        if current_user.studio_id != studio_id:
            raise HTTPException(
                status_code=403,
                detail="Studio access forbidden"
            )

        if permission not in ROLE_PERMISSIONS.get(current_user.role, set()):
            raise HTTPException(
                status_code=403,
                detail="Insufficient permissions"
            )

        return current_user

    return authorize


require_owner = require_studio_permission("team_manage")
require_settings_write = require_studio_permission("settings_write")
require_email_permission = require_studio_permission("email_send")
require_action_status_permission = require_studio_permission(
    "action_status_write"
)
require_member_import = require_studio_permission("member_import")
require_booking_import = require_studio_permission("booking_import")
require_payment_import = require_studio_permission("payment_import")
require_import_history = require_studio_permission("import_history")
require_data_source_write = require_studio_permission("data_source_write")
