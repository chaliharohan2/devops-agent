"""create items"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
    )
    op.bulk_insert(
        sa.table("items", sa.column("name", sa.String), sa.column("price", sa.Numeric)),
        [{"name": "Coffee beans", "price": 14.5}, {"name": "Grinder", "price": 89.0}],
    )


def downgrade() -> None:
    op.drop_table("items")
