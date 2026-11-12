"""leftovers: portions cooked for later, and the meal that eats them

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('planned_meals', sa.Column('leftover_portions', sa.Float(), nullable=False, server_default='0'))
    with op.batch_alter_table('planned_meals') as batch:
        batch.add_column(sa.Column('leftover_of', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_planned_meals_leftover_of', 'planned_meals', ['leftover_of'], ['id'], ondelete='CASCADE')


def downgrade() -> None:
    with op.batch_alter_table('planned_meals') as batch:
        batch.drop_constraint('fk_planned_meals_leftover_of', type_='foreignkey')
        batch.drop_column('leftover_of')
    op.drop_column('planned_meals', 'leftover_portions')
