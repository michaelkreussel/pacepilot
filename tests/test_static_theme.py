import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
CSS = (ROOT / "app/static/css/tailwind.css").read_text(encoding="utf-8")
SOURCE = (ROOT / "app/static/css/tailwind.input.css").read_text(encoding="utf-8")
PROFILE_JS = (ROOT / "app/static/js/profile.js").read_text(encoding="utf-8")
ACTIVITY_DETAIL_JS = (ROOT / "app/static/js/activity-detail.js").read_text(encoding="utf-8")


def test_generated_tailwind_contains_semantic_themes() -> None:
    assert "tailwindcss v4.3.3" in CSS
    assert "--color-brand-500:#2f9979" in CSS
    assert "--background:#f7faf9" in CSS
    assert "--background:#0f1413" in CSS
    assert "--info-emphasis:#1d4ed8" in CSS
    assert "--chart-violet:#6757a8" in CSS
    assert "--map-route:#4f46e5" in CSS
    assert "--map-start:#f59e0b" in CSS
    assert ".bg-primary{" in CSS
    assert ".border-border{" in CSS
    assert ".text-muted-foreground{" in CSS
    assert "@apply" not in CSS
    assert "@keyframes readiness-fill" in CSS
    assert "@keyframes coach-activity-wave" in CSS
    assert "prefers-reduced-motion:reduce" in CSS


def test_generated_tailwind_keeps_only_runtime_components() -> None:
    for obsolete_selector in (
        ".activity-heading",
        ".coach-shell",
        ".profile-heading",
        ".workout-builder",
    ):
        assert obsolete_selector not in CSS

    for runtime_selector in (
        ".sidebar.is-open",
        ".sync-metric.success",
        ".readiness-score",
        ".definition-step.warmup",
        ".calendar-workout.running",
    ):
        assert runtime_selector in CSS

    assert "layer(legacy)" not in SOURCE
    assert "./app.css" not in SOURCE


def test_templates_use_the_bundled_stylesheet() -> None:
    templates = ROOT / "app/templates"
    html = "\n".join(path.read_text(encoding="utf-8") for path in templates.rglob("*.html"))

    assert "/css/tailwind.css" in html
    for legacy_stylesheet in (
        "/css/app.css",
        "/css/activity-detail.css",
        "/css/coach.css",
        "/css/onboarding.css",
        "/css/profile.css",
        "/css/profile-accessibility.css",
        "/css/sync.css",
        "/css/workout-builder.css",
        "/css/workout-definition.css",
    ):
        assert legacy_stylesheet not in html
        assert not (ROOT / "app/static" / legacy_stylesheet.removeprefix("/")).exists()


def test_profile_chart_colors_allow_multi_color_datasets() -> None:
    assert "if (!color) return undefined;" in PROFILE_JS


def test_profile_charts_can_span_configured_data_gaps() -> None:
    assert "spanGaps: Boolean(config.span_gaps)" in PROFILE_JS
    assert "spanGaps: false" not in PROFILE_JS


def test_activity_charts_end_at_the_last_data_point() -> None:
    assert 'bounds: "data"' in ACTIVITY_DETAIL_JS


def test_all_post_forms_include_the_shared_csrf_field() -> None:
    template_root = ROOT / "app" / "templates"
    for path in template_root.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        forms = re.findall(r'<form\b[^>]*method="post"[^>]*>.*?</form>', source, re.DOTALL)
        for form in forms:
            assert "csrf_field()" in form, path


def test_htmx_unsafe_requests_receive_csrf_header() -> None:
    source = (ROOT / "app" / "static" / "js" / "csrf.js").read_text(encoding="utf-8")
    assert 'document.addEventListener("htmx:configRequest"' in source
    assert 'event.detail.headers["X-CSRF-Token"] = token' in source
