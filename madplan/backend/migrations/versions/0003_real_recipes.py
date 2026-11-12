"""recipes from real pages, and sending to Mealie on request

Revision ID: 0003
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    op.add_column('recipes', sa.Column('image_url', sa.String(length=1000), nullable=True))
    op.add_column('recipes', sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('planned_meals', sa.Column('mealie_note_id', sa.String(length=60), nullable=True))
    op.add_column('planned_meals', sa.Column('mealie_sent', _JSON, nullable=True))


def downgrade() -> None:
    op.drop_column('planned_meals', 'mealie_sent')
    op.drop_column('planned_meals', 'mealie_note_id')
    op.drop_column('recipes', 'dismissed_at')
    op.drop_column('recipes', 'image_url')
