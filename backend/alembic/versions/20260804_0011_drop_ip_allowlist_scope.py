"""Drop the IP allowlist scope column.

The allowlist covered either the credential surface or the whole application. In practice that
choice only created ambiguity about what "the firewall is on" meant, so filtering is now
all-or-nothing and the column has no meaning.

Revision ID: 20260804_0011
Revises: 20260804_0010
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0011"
down_revision: str | None = "20260804_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("security_policy") as batch:
        batch.drop_column("ip_allowlist_scope")


def downgrade() -> None:
    op.add_column("security_policy", sa.Column("ip_allowlist_scope", sa.String(16), nullable=False, server_default="auth_only"))
