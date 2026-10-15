"""name sites left without a name

Revision ID: 44dbf7c5f618
Revises: 6b1e0d4c9a21
Create Date: 2026-10-16 12:00:00.000000

The rename to `sites` gave the rows it kept an empty name: they're named after their repo (`ada-s-bakery` is "Ada S
Bakery"), or "My site" without one, so the list shows them and a creation over one doesn't ask for its name again.
"""
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '44dbf7c5f618'
down_revision: Union[str, Sequence[str], None] = '6b1e0d4c9a21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

sites = sa.table(
    'sites',
    sa.column('id', sa.Uuid()),
    sa.column('name', sa.String(length=100)),
    sa.column('repo_name', sa.String(length=100)),
)


def _name_for(repo_name: str | None) -> str:
    words = re.sub(r'[-_.]+', ' ', repo_name or '').strip()
    return words.title()[:100] if words else 'My site'


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    unnamed = conn.execute(sa.select(sites.c.id, sites.c.repo_name).where(sites.c.name == '')).all()
    for site_id, repo_name in unnamed:
        conn.execute(sites.update().where(sites.c.id == site_id).values(name=_name_for(repo_name)))


def downgrade() -> None:
    """Downgrade schema."""
    # The names stay: which ones were empty isn't known any more, and an empty name is never needed.
