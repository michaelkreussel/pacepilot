from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from httpx import HTTPError

from app.auth import PROVIDERS, SESSION_USER_ID, configured_providers, oauth
from app.config import get_settings
from app.database import SessionDep
from app.repositories.users import get_or_create_oauth_user
from app.web import context, templates

router = APIRouter()


@dataclass(frozen=True)
class IdentityProfile:
    subject: str
    display_name: str
    email: str | None
    email_verified: bool
    username: str | None
    avatar_url: str | None


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None) -> Response:
    if request.session.get(SESSION_USER_ID) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        context(
            request,
            providers=configured_providers(get_settings()),
            next=_safe_next(request.query_params.get("next")),
            error=error,
        ),
    )


@router.get("/auth/{provider}/login", name="oauth_login")
async def oauth_login(provider: str, request: Request, next: str | None = None) -> RedirectResponse:
    client = oauth.create_client(provider)
    if provider not in PROVIDERS or client is None:
        raise HTTPException(status_code=404, detail="Anmeldeanbieter nicht konfiguriert")
    request.session.clear()
    request.session["post_login_redirect"] = _safe_next(next)
    redirect_uri = request.url_for("oauth_callback", provider=provider)
    return await client.authorize_redirect(request, redirect_uri)


async def _google_profile(token: dict[str, Any]) -> IdentityProfile:
    claims = token.get("userinfo")
    if not isinstance(claims, dict) or not claims.get("sub"):
        raise OAuthError(description="Google hat kein gültiges Benutzerprofil geliefert")
    return IdentityProfile(
        subject=str(claims["sub"]),
        display_name=str(claims.get("name") or claims.get("email") or "Athlet"),
        email=str(claims["email"]) if claims.get("email") else None,
        email_verified=claims.get("email_verified") is True,
        username=None,
        avatar_url=str(claims["picture"]) if claims.get("picture") else None,
    )


async def _github_profile(client: Any, token: dict[str, Any]) -> IdentityProfile:
    profile_response = await client.get("user", token=token)
    profile_response.raise_for_status()
    profile = profile_response.json()
    emails_response = await client.get("user/emails", token=token)
    emails_response.raise_for_status()
    emails = emails_response.json()
    verified_emails = [item for item in emails if item.get("verified") and item.get("email")]
    selected_email = next((item for item in verified_emails if item.get("primary")), None)
    if selected_email is None and verified_emails:
        selected_email = verified_emails[0]
    email = str(selected_email["email"]) if selected_email else None
    if not profile.get("id"):
        raise OAuthError(description="GitHub hat kein gültiges Benutzerprofil geliefert")
    return IdentityProfile(
        subject=str(profile["id"]),
        display_name=str(profile.get("name") or profile.get("login") or "Athlet"),
        email=email,
        email_verified=selected_email is not None,
        username=str(profile["login"]) if profile.get("login") else None,
        avatar_url=str(profile["avatar_url"]) if profile.get("avatar_url") else None,
    )


@router.get("/auth/{provider}/callback", name="oauth_callback")
async def oauth_callback(
    provider: str,
    request: Request,
    session: SessionDep,
) -> RedirectResponse:
    client = oauth.create_client(provider)
    if provider not in PROVIDERS or client is None:
        raise HTTPException(status_code=404, detail="Anmeldeanbieter nicht konfiguriert")
    try:
        token = await client.authorize_access_token(request)
        profile = (
            await _google_profile(token)
            if provider == "google"
            else await _github_profile(client, token)
        )
    except (OAuthError, HTTPError, ValueError, TypeError):
        query = urlencode({"error": "Die Anmeldung konnte nicht abgeschlossen werden."})
        return RedirectResponse(f"/login?{query}", status_code=303)

    settings = get_settings()
    user = get_or_create_oauth_user(
        session,
        provider=provider,
        subject=profile.subject,
        display_name=profile.display_name,
        email=profile.email,
        email_verified=profile.email_verified,
        username=profile.username,
        avatar_url=profile.avatar_url,
        legacy_user_email=settings.auth_legacy_user_email,
    )
    redirect_target = _safe_next(request.session.pop("post_login_redirect", "/"))
    request.session.clear()
    request.session[SESSION_USER_ID] = user.id
    return RedirectResponse(redirect_target, status_code=303)


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
