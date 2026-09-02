"""replace_is_first_login_with_walkover_completed

Revision ID: d7e2f9a4b6c1
Revises: c5d8e1a3b9f7
Create Date: 2026-05-15 12:30:00.000000

The original ``is_first_login`` flag was flipped server-side on the user's
first successful login. That made the flag impossible to recover: if the
frontend never actually rendered the walkthrough (crash, refresh, network
drop, etc.) the user would never see it again because the server had
already marked them as "no longer first login".

We replace that flag with ``walkover_completed`` which is *only* flipped
when the frontend explicitly confirms the user finished the walkover via
``POST /complete-walkover``. The flag is read on every authenticated
session via ``GET /get-walkover-status``.

``server_default='false'`` is used so all pre-existing rows start as
"walkover not yet completed" — they will see the walkover the next time
they log in. Adjust the server_default to ``'true'`` here if you want to
skip the walkover for existing users.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7e2f9a4b6c1'
down_revision: Union[str, None] = 'c5d8e1a3b9f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop ``is_first_login`` and add ``walkover_completed`` on ``users``."""
    op.drop_column('users', 'is_first_login')
    op.add_column(
        'users',
        sa.Column(
            'walkover_completed',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
            comment=(
                "False until the user explicitly finishes the in-app walkover/"
                "walkthrough. Frontend reads this via GET /get-walkover-status "
                "and flips it to True via POST /complete-walkover once the tour "
                "is finished."
            ),
        ),
    )


def downgrade() -> None:
    """Reverse the column swap.

    We restore ``is_first_login`` with ``server_default='false'`` (matching
    the original migration's choice of "existing rows have already logged
    in") and drop ``walkover_completed``.
    """
    op.drop_column('users', 'walkover_completed')
    op.add_column(
        'users',
        sa.Column(
            'is_first_login',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
            comment=(
                "Set to True on the user's first signup. Flipped back to False "
                "automatically on the first successful login so the frontend can "
                "show the app walkthrough once."
            ),
        ),
    )
