from pathlib import Path
from shutil import copytree

import pytest

from app.services.planning.registry import (
    KNOWLEDGE_ROOT,
    KnowledgeRegistryError,
    _load_yaml,
    load_knowledge_registry,
)
from app.services.planning.registry_models import ConstraintSet, EvidenceIndex


def test_registry_is_deterministic_and_references_resolve() -> None:
    first = load_knowledge_registry()
    second = load_knowledge_registry()

    assert first.version == second.version
    assert first.version.startswith("1.0.0+sha256:")
    assert set(first.workouts) == {
        "easy_run",
        "recovery_run",
        "long_run",
        "strides",
        "threshold_cruise",
        "vo2_intervals",
    }
    assert first.workouts["easy_run"].status == "active"
    assert first.workouts["threshold_cruise"].status == "deferred"
    assert all(
        set(artifact.evidence_refs) <= set(first.evidence)
        for artifact in [*first.workouts.values(), *first.constraints.values()]
    )


def test_active_rules_are_grounded_and_runtime_safety_ids_are_registered() -> None:
    registry = load_knowledge_registry()
    expected_safety_ids = {
        "SAFE-CARDIO-001",
        "SAFE-ILLNESS-001",
        "SAFE-ILLNESS-002",
        "SAFE-ILLNESS-003",
        "SAFE-PAIN-001",
        "SAFE-PAIN-002",
        "SAFE-PAIN-003",
        "READY-SUBJECTIVE-001",
        "TIME-BUDGET-001",
        "RECOVERY-SESSION-001",
    }

    assert expected_safety_ids <= set(registry.constraints)
    assert all(
        rule.evidence_refs or rule.product_heuristic is not None
        for rule in registry.constraints.values()
        if rule.status == "active"
    )


def test_yaml_loader_rejects_python_constructors(tmp_path: Path) -> None:
    document = tmp_path / "unsafe.yaml"
    document.write_text("!!python/object/apply:os.system ['echo unsafe']", encoding="utf-8")

    with pytest.raises(KnowledgeRegistryError):
        _load_yaml(document, EvidenceIndex)


def test_yaml_loader_rejects_duplicate_keys_and_non_finite_parameters(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text("version: 1.0.0\nversion: 2.0.0\nentries: []\n", encoding="utf-8")
    non_finite = tmp_path / "non-finite.yaml"
    non_finite.write_text(
        """id: test
version: 1.0.0
rules:
  - id: TEST-001
    status: deferred
    enforcement: heuristic
    implementation: progression.change_one_axis
    description: Test
    parameters: {value: .nan}
""",
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeRegistryError):
        _load_yaml(duplicate, EvidenceIndex)
    with pytest.raises(KnowledgeRegistryError):
        _load_yaml(non_finite, ConstraintSet)


def test_constraint_set_version_contributes_to_registry_version(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    copytree(KNOWLEDGE_ROOT, root)
    original = load_knowledge_registry(root)
    path = root / "constraints" / "quality_density.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("1.0.0", "1.0.1", 1), encoding="utf-8")

    changed = load_knowledge_registry(root)

    assert original.version != changed.version
