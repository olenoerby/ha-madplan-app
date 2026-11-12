"""pantry flow, restocking and assistant proposals

Revision ID: 0002
Revises: 0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    op.add_column('pantry_items', sa.Column('expires_estimated', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('pantry_items', sa.Column('min_quantity', sa.Float(), nullable=True))
    op.add_column('recipes', sa.Column('steps_en', _JSON, nullable=True))
    op.add_column('planned_meals', sa.Column('cooked', sa.Boolean(), nullable=True))
    op.add_column('planned_meals', sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('planned_meals', sa.Column('consumed', _JSON, nullable=True))
    op.add_column('chat_messages', sa.Column('proposals', _JSON, nullable=True))


def downgrade() -> None:
    op.drop_column('chat_messages', 'proposals')
    op.drop_column('planned_meals', 'consumed')
    op.drop_column('planned_meals', 'consumed_at')
    op.drop_column('planned_meals', 'cooked')
    op.drop_column('recipes', 'steps_en')
    op.drop_column('pantry_items', 'min_quantity')
    op.drop_column('pantry_items', 'expires_estimated')
