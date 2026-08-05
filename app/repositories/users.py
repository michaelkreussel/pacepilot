from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GarminAccount, User


def get_or_create_default_user(session: Session) -> User:
    user = session.scalar(select(User).order_by(User.id).limit(1))
    if user is None:
        user = User(display_name="Athlet")
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def get_or_create_garmin_account(session: Session, user: User) -> GarminAccount:
    account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user.id))
    if account is None:
        account = GarminAccount(user_id=user.id)
        session.add(account)
        session.commit()
        session.refresh(account)
    return account
