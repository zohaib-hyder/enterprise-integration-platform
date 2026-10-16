"""Explicit self-reference fence omitted by PostgreSQL CREATE TABLE use_alter."""

from alembic import op

revision = "7e8a5c21b406"
down_revision = "626a6f2a839d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_execution_root_tenant",
        "workflow_executions",
        "workflow_executions",
        ["root_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint("fk_execution_root_tenant", "workflow_executions", type_="foreignkey")
