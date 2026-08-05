from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.web import context, templates

router = APIRouter(prefix="/coach")


@router.get("", response_class=HTMLResponse)
def coach(request: Request) -> HTMLResponse:
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "coach.html",
        context(
            request,
            active_page="coach",
            configured=bool(settings.llm_api_key and settings.llm_base_url),
        ),
    )
