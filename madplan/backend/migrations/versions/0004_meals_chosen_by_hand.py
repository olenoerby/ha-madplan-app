"""dishes put on a day by hand, and whether their food is bought

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The default fills the rows that are already there, so existing plans keep working.
    op.add_column('planned_meals', sa.Column('bought', sa.String(length=8), nullable=False, server_default='none'))
    with op.batch_alter_table('planned_meals') as batch:
        batch.add_column(sa.Column('added_by', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_planned_meals_added_by_users', 'users', ['added_by'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('planned_meals') as batch:
        batch.drop_constraint('fk_planned_meals_added_by_users', type_='foreignkey')
        batch.drop_column('added_by')
    op.drop_column('planned_meals', 'bought')
