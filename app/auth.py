from dataclasses import dataclass
from typing import Annotated, Any

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, Request

from app.config import Settings, get_settings
from app.database import SessionDep
from app.models import User

SESSION_USER_ID = "user_id"


class AuthenticationRequired(Exception):
    pass


@dataclass(frozen=True)
class Provider:
    name: str
    label: str


PROVIDERS = {
    "google": Provider("google", "Google"),
    "github": Provider("github", "GitHub"),
}


def _build_oauth(settings: Settings) -> OAuth:
    registry = OAuth()
    if settings.google_client_id and settings.google_client_secret:
        registry.register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={
                "scope": "openid email profile",
                "code_challenge_method": "S256",
            },
        )
    if settings.github_client_id and settings.github_client_secret:
        registry.register(
            name="github",
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret,
            authorize_url="https://github.com/login/oauth/authorize",
            access_token_url="https://github.com/login/oauth/access_token",
            api_base_url="https://api.github.com/",
            client_kwargs={
                "scope": "read:user user:email",
                "code_challenge_method": "S256",
            },
        )
    return registry


oauth = _build_oauth(get_settings())


def configured_providers(settings: Settings) -> list[Provider]:
    configured = []
    if settings.google_client_id and settings.google_client_secret:
        configured.append(PROVIDERS["google"])
    if settings.github_client_id and settings.github_client_secret:
        configured.append(PROVIDERS["github"])
    return configured


def get_current_user(request: Request, session: SessionDep) -> User:
    user_id: Any = request.session.get(SESSION_USER_ID)
    if type(user_id) is not int:
        raise AuthenticationRequired
    user = session.get(User, user_id)
    if user is None:
        request.session.clear()
        raise AuthenticationRequired
    request.state.current_user = user
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
