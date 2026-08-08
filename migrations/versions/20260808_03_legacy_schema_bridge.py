"""bridge databases created before migration consolidation

Revision ID: 20260808_03
Revises: 20260808_01
Create Date: 2026-08-08 21:00:00

The original migration chain ended at revision 20260808_03. Its resulting
schema is equivalent to the consolidated 20260808_01 baseline, so this bridge
is intentionally empty and preserves upgrade compatibility for existing data.
"""

from collections.abc import Sequence

revision: str = "20260808_03"
down_revision: str | None = "20260808_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
