"""rename ai_threads to sites

Revision ID: 6b1e0d4c9a21
Revises: 2c52bed41613
Create Date: 2026-10-15 20:00:00.000000

A site is now its repo (the code) and its AI thread, so the table that says whose thread it is becomes the site's
own: renamed (keeping its rows), with the name the user gave the site and one site per repo.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6b1e0d4c9a21'
down_revision: Union[str, Sequence[str], None] = '2c52bed41613'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.rename_table('ai_threads', 'sites')
    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.drop_index('ix_ai_threads_user_id')
        batch_op.create_index(batch_op.f('ix_sites_user_id'), ['user_id'], unique=False)
        batch_op.add_column(sa.Column('name', sa.String(length=100), nullable=False, server_default=''))
        batch_op.create_unique_constraint('uq_sites_user_id_repo_name', ['user_id', 'repo_name'])
    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.alter_column('name', server_default=None)

    # One batch each: SQLite rebuilds the table per batch and can't index a column renamed in the same one.
    with op.batch_alter_table('ai_edit_requests', schema=None) as batch_op:
        batch_op.drop_index('ix_ai_edit_requests_thread_id')
    with op.batch_alter_table('ai_edit_requests', schema=None) as batch_op:
        batch_op.alter_column('thread_id', new_column_name='site_id')
    with op.batch_alter_table('ai_edit_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ai_edit_requests_site_id'), ['site_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('ai_edit_requests', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ai_edit_requests_site_id'))
    with op.batch_alter_table('ai_edit_requests', schema=None) as batch_op:
        batch_op.alter_column('site_id', new_column_name='thread_id')
    with op.batch_alter_table('ai_edit_requests', schema=None) as batch_op:
        batch_op.create_index('ix_ai_edit_requests_thread_id', ['thread_id'], unique=False)

    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.drop_constraint('uq_sites_user_id_repo_name', type_='unique')
        batch_op.drop_column('name')
        batch_op.drop_index(batch_op.f('ix_sites_user_id'))
        batch_op.create_index('ix_ai_threads_user_id', ['user_id'], unique=False)
    op.rename_table('sites', 'ai_threads')
