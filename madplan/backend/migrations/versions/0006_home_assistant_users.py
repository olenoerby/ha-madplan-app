"""users come from Home Assistant

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('ha_user_id', sa.String(length=64), nullable=True))
        batch.create_unique_constraint('uq_users_ha_user_id', ['ha_user_id'])


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.drop_constraint('uq_users_ha_user_id', type_='unique')
        batch.drop_column('ha_user_id')
