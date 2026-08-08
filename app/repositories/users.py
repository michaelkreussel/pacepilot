from sqlalchemy import func, select
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
    legacy_user_email: str | None = None,
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

    user = None
    can_adopt_legacy_user = (
        user is None
        and legacy_user_email is not None
        and email is not None
        and email_verified
        and legacy_user_email.casefold() == email.casefold()
        and session.scalar(select(func.count()).select_from(User)) == 1
        and session.scalar(select(func.count()).select_from(OAuthIdentity)) == 0
    )
    if can_adopt_legacy_user:
        user = session.scalar(select(User).order_by(User.id).limit(1))
    if user is None:
        user = User(display_name=display_name)
        session.add(user)
        session.flush()
    else:
        user.display_name = display_name

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
    session.commit()
    return user


def get_or_create_garmin_account(session: Session, user: User) -> GarminAccount:
    account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user.id))
    if account is None:
        account = GarminAccount(user_id=user.id)
        session.add(account)
        session.commit()
        session.refresh(account)
    return account
