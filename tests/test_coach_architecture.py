import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
APP = ROOT / "app"
PROVIDER = APP / "services" / "coach" / "provider.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_provider_framework_imports_are_isolated_to_provider_adapter() -> None:
    violations = {
        str(path.relative_to(ROOT)): sorted(
            name for name in _imports(path) if name.startswith(("langchain", "langgraph"))
        )
        for path in APP.rglob("*.py")
        if path != PROVIDER
        and any(name.startswith(("langchain", "langgraph")) for name in _imports(path))
    }

    assert violations == {}


def test_domain_services_do_not_depend_on_coach_implementation() -> None:
    domain_roots = (
        APP / "services" / "analytics",
        APP / "services" / "garmin",
        APP / "services" / "planning",
    )
    violations = {
        str(path.relative_to(ROOT)): sorted(
            name
            for name in _imports(path)
            if name.startswith(("app.models.coach", "app.services.coach"))
        )
        for domain_root in domain_roots
        for path in domain_root.rglob("*.py")
        if any(
            name.startswith(("app.models.coach", "app.services.coach")) for name in _imports(path)
        )
    }

    assert violations == {}


def test_coach_http_adapter_does_not_import_provider_implementation() -> None:
    assert "app.services.coach.provider" not in _imports(APP / "routes" / "coach.py")
