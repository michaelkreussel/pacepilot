from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import GarminAccount, OAuthIdentity, User
from app.models.user import utcnow


def get_or_create_oauth_user(
    session: Session,
    *,
    provider: str,
    subject: str,
    display_name: str,
    email: str | None,
    email_verified: bool,
    username: str | None,
    avatar_url: str | None,
) -> User:
    identity = session.scalar(
        select(OAuthIdentity).where(
            OAuthIdentity.provider == provider,
            OAuthIdentity.subject == subject,
        )
    )
    if identity is not None:
        identity.email = email
        identity.email_verified = email_verified
        identity.username = username
        identity.avatar_url = avatar_url
        identity.last_login_at = utcnow()
        identity.user.display_name = display_name
        session.commit()
        return identity.user

    user = User(display_name=display_name)
    session.add(user)
    session.flush()

    user.oauth_identities.append(
        OAuthIdentity(
            provider=provider,
            subject=subject,
            email=email,
            email_verified=email_verified,
            username=username,
            avatar_url=avatar_url,
        )
    )
    try:
        session.commit()
        return user
    except IntegrityError:
        session.rollback()
        identity = session.scalar(
            select(OAuthIdentity).where(
                OAuthIdentity.provider == provider,
                OAuthIdentity.subject == subject,
            )
        )
        if identity is None:
            raise
        return identity.user


def get_or_create_garmin_account(session: Session, user: User) -> GarminAccount:
    account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user.id))
    if account is None:
        account = GarminAccount(user_id=user.id)
        session.add(account)
        try:
            session.commit()
            session.refresh(account)
        except IntegrityError:
            session.rollback()
            account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user.id))
            if account is None:
                raise
    return account
