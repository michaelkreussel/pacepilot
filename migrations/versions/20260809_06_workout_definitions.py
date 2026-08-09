"""add versioned workout definitions

Revision ID: 20260809_06
Revises: 20260808_05
Create Date: 2026-08-09 10:00:00
"""

import json
from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_06"
down_revision: str | None = "20260808_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _node_id(workout_id: int, kind: str, *parts: object) -> str:
    suffix = ":".join(str(part) for part in parts)
    return str(uuid5(NAMESPACE_URL, f"pacepilot:workout:{workout_id}:{kind}:{suffix}"))


def _step_node(workout_id: int, step: sa.RowMapping) -> dict[str, object]:
    end = (
        {"type": "time", "seconds": float(step["duration_value"] or 0)}
        if step["duration_type"] == "time"
        else {"type": "distance", "meters": float(step["duration_value"] or 0)}
    )
    target = (
        {
            "type": "pace_range",
            "fastest_seconds_per_km": float(step["target_min"] or 0),
            "slowest_seconds_per_km": float(step["target_max"] or 0),
        }
        if step["target_type"] == "pace"
        else {"type": "none"}
    )
    return {
        "id": _node_id(workout_id, "step", step["id"]),
        "kind": "step",
        "step_type": step["step_type"],
        "end": end,
        "target": target,
    }


def _definition(workout_id: int, steps: list[sa.RowMapping]) -> dict[str, object]:
    blocks: list[dict[str, object]] = []
    index = 0
    while index < len(steps):
        step = steps[index]
        repetitions = int(step["repeat_count"] or 1)
        next_step = steps[index + 1] if index + 1 < len(steps) else None
        if (
            step["step_type"] == "interval"
            and repetitions > 1
            and next_step is not None
            and next_step["step_type"] == "recovery"
            and int(next_step["repeat_count"] or 1) == repetitions
        ):
            blocks.append(
                {
                    "id": _node_id(workout_id, "pair", step["id"], next_step["id"]),
                    "kind": "repeat",
                    "iterations": repetitions,
                    "children": [
                        _step_node(workout_id, step),
                        _step_node(workout_id, next_step),
                    ],
                }
            )
            index += 2
            continue
        node = _step_node(workout_id, step)
        if repetitions > 1:
            blocks.append(
                {
                    "id": _node_id(workout_id, "single", step["id"]),
                    "kind": "repeat",
                    "iterations": repetitions,
                    "children": [node],
                }
            )
        else:
            blocks.append(node)
        index += 1
    return {"blocks": blocks}


def upgrade() -> None:
    op.add_column("workouts", sa.Column("definition_version", sa.Integer(), nullable=True))
    op.add_column("workouts", sa.Column("definition", sa.JSON(), nullable=True))

    connection = op.get_bind()
    workouts = connection.execute(sa.text("SELECT id FROM workouts ORDER BY id")).mappings()
    for workout in workouts:
        workout_id = int(workout["id"])
        steps = list(
            connection.execute(
                sa.text(
                    "SELECT id, step_type, duration_type, duration_value, target_type, "
                    "target_min, target_max, repeat_count FROM workout_steps "
                    "WHERE workout_id = :workout_id ORDER BY position"
                ),
                {"workout_id": workout_id},
            ).mappings()
        )
        connection.execute(
            sa.text(
                "UPDATE workouts SET definition_version = 1, definition = :definition "
                "WHERE id = :workout_id"
            ),
            {
                "definition": json.dumps(_definition(workout_id, steps), separators=(",", ":")),
                "workout_id": workout_id,
            },
        )

    with op.batch_alter_table("workouts") as batch_op:
        batch_op.alter_column("definition_version", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("definition", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("workouts") as batch_op:
        batch_op.drop_column("definition")
        batch_op.drop_column("definition_version")
