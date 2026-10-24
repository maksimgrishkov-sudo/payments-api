"""notifications outbox + payments.callback_url

Revision ID: b7e1d2c3f4a5
Revises: a4c64e483887
Create Date: 2026-10-24 17:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b7e1d2c3f4a5'
down_revision: Union[str, Sequence[str], None] = 'a4c64e483887'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('payments', sa.Column('callback_url', sa.String(length=500), nullable=True))
    op.create_table('notifications',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('payment_id', sa.Uuid(), nullable=False),
    sa.Column('event', sa.String(length=32), nullable=False),
    sa.Column('url', sa.String(length=500), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.Enum('pending', 'sent', 'failed', name='notification_status'), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.String(length=300), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notifications_payment_id'), 'notifications', ['payment_id'], unique=False)
    # sweep() ищет «pending, срок прошёл» каждую минуту — индекс только по ждущим, он маленький
    op.create_index('ix_notifications_pending', 'notifications', ['next_attempt_at'], unique=False,
                    postgresql_where=sa.text("status = 'pending'"))


def downgrade() -> None:
    op.drop_index('ix_notifications_pending', table_name='notifications')
    op.drop_index(op.f('ix_notifications_payment_id'), table_name='notifications')
    op.drop_table('notifications')
    # enum-тип Postgres сам не удаляется (см. первую миграцию) — иначе повторный upgrade падает
    sa.Enum(name='notification_status').drop(op.get_bind(), checkfirst=True)
    op.drop_column('payments', 'callback_url')
