from pathlib import Path

ROOT = Path(__file__).parents[1]
CSS = (ROOT / "app/static/css/tailwind.css").read_text(encoding="utf-8")


def test_generated_tailwind_contains_semantic_themes() -> None:
    assert "tailwindcss v4.3.3" in CSS
    assert "--color-brand-500:#2f9979" in CSS
    assert "--background:#f7faf9" in CSS
    assert "--background:#0c1210" in CSS
    assert "--info-emphasis:#1d4ed8" in CSS
    assert "--chart-violet:#6757a8" in CSS
    assert ".bg-primary{" in CSS
    assert ".border-border{" in CSS
    assert ".text-muted-foreground{" in CSS
    assert "@apply" not in CSS


def test_generated_tailwind_bundles_feature_styles() -> None:
    for selector in (
        ".activity-heading",
        ".coach-shell",
        ".profile-heading",
        ".sync-progress",
        ".workout-builder",
    ):
        assert selector in CSS


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
