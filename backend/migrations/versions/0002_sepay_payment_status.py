"""Allow 'completed' status in payment_transactions and enable pro plan.

Revision ID: 0002_sepay_payment_status
Revises: 0001_initial_baseline
Create Date: 2026-10-28 20:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_sepay_payment_status"
down_revision: str | None = "0001_initial_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE payment_transactions DROP CONSTRAINT IF EXISTS ck_payment_transactions_status;
        ALTER TABLE payment_transactions ADD CONSTRAINT ck_payment_transactions_status
            CHECK (status IN ('pending', 'completed', 'succeeded', 'failed', 'refunded'));

        -- Activate Pro plan with approved pricing (50,000 VND / 30 days) per DECISION-PO-02
        UPDATE subscription_plans
        SET price_vnd = 50000, duration_days = 30, is_active = TRUE, updated_at = CURRENT_TIMESTAMP
        WHERE code = 'pro';
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE payment_transactions DROP CONSTRAINT IF EXISTS ck_payment_transactions_status;
        ALTER TABLE payment_transactions ADD CONSTRAINT ck_payment_transactions_status
            CHECK (status IN ('pending', 'succeeded', 'failed', 'refunded'));

        UPDATE subscription_plans
        SET price_vnd = NULL, duration_days = 30, is_active = FALSE, updated_at = CURRENT_TIMESTAMP
        WHERE code = 'pro';
    """)
