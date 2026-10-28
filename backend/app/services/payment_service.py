"""Payment service handling VietQR generation, status polling, and SePay webhook processing."""

from __future__ import annotations

import hmac
import re
import secrets
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends

from app.core.config import Settings, get_settings
from app.repositories.payment_repo import (
    PaymentRepository,
    PaymentTransaction,
    get_payment_repo,
)


class PaymentServiceError(Exception):
    """Base exception for payment operations."""


class PaymentNotFoundError(PaymentServiceError):
    """Raised when transaction or order is not found."""


class PaymentValidationError(PaymentServiceError):
    """Raised when transfer amount or parameters fail validation."""


class PaymentAuthenticationError(PaymentServiceError):
    """Raised when webhook authorization fails."""


@dataclass(frozen=True)
class PaymentOrderDTO:
    """Payment order and VietQR display details."""

    payment_id: uuid.UUID
    reference_code: str
    amount_vnd: int
    qr_url: str
    account_number: str
    bank_name: str
    status: str


@dataclass(frozen=True)
class PaymentStatusDTO:
    """Polling status of a payment transaction."""

    payment_id: uuid.UUID
    reference_code: str
    amount_vnd: int
    status: str
    paid_at: datetime | None = None
    plan_code: str = "pro"


class PaymentService:
    """Business logic for SePay QR orders, status inquiries, and webhook execution."""

    def __init__(
        self,
        payment_repo: PaymentRepository,
        settings_obj: Settings | None = None,
    ) -> None:
        self.payment_repo = payment_repo
        self.settings = settings_obj or get_settings()

    def verify_webhook_auth(self, auth_header: str | None) -> None:
        """Validate SePay webhook API key if configured; allow bypass in dev mode if None."""
        configured_key = self.settings.sepay_api_key
        if not configured_key:
            # Dev bypass allowed when no API key configured
            return

        if not auth_header:
            raise PaymentAuthenticationError("Missing SePay webhook authorization header.")

        token = auth_header.strip()
        if token.lower().startswith("apikey ") or token.lower().startswith("bearer "):
            token = token[7:].strip()

        if not hmac.compare_digest(token, configured_key):
            raise PaymentAuthenticationError("Invalid SePay webhook authorization token.")

    async def create_payment_qr(
        self, user_id: uuid.UUID, plan_code: str = "pro"
    ) -> PaymentOrderDTO:
        """Create a pending transaction order and generate standard VietQR URL."""
        plan = await self.payment_repo.get_plan_by_code(plan_code)
        if not plan:
            if plan_code == "pro":
                # Approved fallback per DECISION-PO-02: 50,000 VND / 30 days
                amount_vnd = 50000
                plan_id = uuid.uuid4()
            else:
                raise PaymentValidationError(f"Invalid subscription plan code: '{plan_code}'.")
        else:
            amount_vnd = plan.price_vnd if plan.price_vnd is not None else 50000
            plan_id = plan.id

        # Unique reference code: e.g. EDU + 8 hex chars (uppercase alphanumeric)
        reference_code = f"EDU{secrets.token_hex(4).upper()}"

        account = self.settings.sepay_account_number
        bank = self.settings.sepay_bank_name
        quoted_bank = urllib.parse.quote(bank)
        quoted_des = urllib.parse.quote(reference_code)

        # Standard VietQR format: https://qr.sepay.vn/img?acc={account}&bank={bank}&amount={amount}&des={reference_code}
        qr_url = (
            f"https://qr.sepay.vn/img?acc={account}&bank={quoted_bank}&amount={amount_vnd}&des={quoted_des}"
        )

        transaction, _ = await self.payment_repo.create_payment_order(
            user_id=user_id,
            plan_id=plan_id,
            amount_vnd=amount_vnd,
            reference_code=reference_code,
        )

        return PaymentOrderDTO(
            payment_id=transaction.id,
            reference_code=reference_code,
            amount_vnd=amount_vnd,
            qr_url=qr_url,
            account_number=account,
            bank_name=bank,
            status=transaction.status,
        )

    async def get_payment_status(
        self, user_id: uuid.UUID, payment_id: uuid.UUID
    ) -> PaymentStatusDTO:
        """Query payment transaction status with strict tenant isolation."""
        transaction = await self.payment_repo.get_transaction(
            user_id=user_id, transaction_id=payment_id
        )
        if not transaction:
            raise PaymentNotFoundError(
                f"Payment transaction '{payment_id}' not found for user."
            )

        return PaymentStatusDTO(
            payment_id=transaction.id,
            reference_code=transaction.reference_code or "",
            amount_vnd=transaction.amount_vnd,
            status=transaction.status,
            paid_at=transaction.paid_at,
            plan_code="pro",
        )

    async def process_sepay_webhook(
        self, payload: dict[str, Any], auth_header: str | None = None
    ) -> bool:
        """Validate and apply SePay webhook payment event atomically with idempotency."""
        # 1. Authenticate webhook request
        self.verify_webhook_auth(auth_header)

        # 2. Extract transfer details
        raw_id = payload.get("id")
        if raw_id is None:
            raise PaymentValidationError("Missing required 'id' in SePay webhook payload.")
        sepay_transaction_id = str(raw_id).strip()

        # Idempotency check 1: Has this SePay transaction ID already been processed?
        existing_tx = await self.payment_repo.get_transaction_by_sepay_id(sepay_transaction_id)
        if existing_tx and existing_tx.status == "completed":
            return True

        # Extract transfer amount
        raw_amount = (
            payload.get("transferAmount")
            or payload.get("amount")
            or payload.get("transfer_amount")
            or 0
        )
        try:
            transfer_amount = int(float(raw_amount))
        except (ValueError, TypeError):
            raise PaymentValidationError(f"Invalid transfer amount in payload: {raw_amount}")

        # Extract transfer content/code to match reference_code
        content_field = str(payload.get("content") or "").strip()
        code_field = str(payload.get("code") or "").strip()
        desc_field = str(payload.get("description") or "").strip()

        tx: PaymentTransaction | None = None

        # 1) Try matching code field directly
        if code_field:
            tx = await self.payment_repo.get_transaction_by_reference(code_field)

        # 2) Try matching content field directly
        if not tx and content_field:
            tx = await self.payment_repo.get_transaction_by_reference(content_field)

        # 3) Try regex search in combined content, description, code for EDU[A-Za-z0-9]+
        if not tx:
            search_text = f"{content_field} {desc_field} {code_field}"
            matches = re.findall(r"EDU[A-Za-z0-9]+", search_text, re.IGNORECASE)
            for cand in matches:
                tx = await self.payment_repo.get_transaction_by_reference(cand.upper())
                if tx:
                    break

        if not tx:
            # Invalid or unknown reference code handled gracefully
            return False

        # Idempotency check 2: Is matched transaction already completed?
        if tx.status == "completed":
            return True

        # Verify sufficient payment amount
        if transfer_amount < tx.amount_vnd:
            raise PaymentValidationError(
                f"Transfer amount {transfer_amount} VND is less than required {tx.amount_vnd} VND."
            )

        # Execute atomic payment completion and subscription activation
        duration_days = 30
        await self.payment_repo.complete_payment_and_activate_subscription(
            transaction_id=tx.id,
            sepay_transaction_id=sepay_transaction_id,
            provider_payload=payload,
            duration_days=duration_days,
        )

        return True


def get_payment_service(
    payment_repo: Annotated[PaymentRepository, Depends(get_payment_repo)],
) -> PaymentService:
    """FastAPI dependency provider for PaymentService."""
    return PaymentService(payment_repo=payment_repo)
