"""Payments REST API router for VietQR generation, status polling, and SePay webhooks."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status
from pydantic import BaseModel, Field

from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.payment_service import (
    PaymentAuthenticationError,
    PaymentNotFoundError,
    PaymentService,
    PaymentValidationError,
    get_payment_service,
)

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------


class CreatePaymentQRRequest(BaseModel):
    """Request payload for creating a payment QR code."""

    plan_code: str = Field(default="pro", description="Target subscription plan code (e.g. 'pro').")


class PaymentQRData(BaseModel):
    """Payment order and VietQR details."""

    payment_id: uuid.UUID
    reference_code: str
    amount_vnd: int
    qr_url: str
    account_number: str
    bank_name: str
    status: str


class PaymentQRResponse(BaseModel):
    """Standardized response envelope for payment QR creation."""

    success: bool = True
    data: PaymentQRData
    message: str = "Payment QR code generated successfully."


class PaymentStatusData(BaseModel):
    """Payment order polling status."""

    payment_id: uuid.UUID
    reference_code: str
    amount_vnd: int
    status: str
    paid_at: datetime | None = None
    plan_code: str = "pro"


class PaymentStatusResponse(BaseModel):
    """Standardized response envelope for payment status inquiries."""

    success: bool = True
    data: PaymentStatusData
    message: str = "Payment status retrieved successfully."


class WebhookResultData(BaseModel):
    """Webhook execution result flag."""

    processed: bool


class WebhookResponse(BaseModel):
    """Standardized response envelope for SePay webhook event."""

    success: bool = True
    data: WebhookResultData
    message: str = "Webhook processed successfully."


# ---------------------------------------------------------------------------
# Router Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/create-qr",
    response_model=PaymentQRResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create VietQR payment order",
)
async def create_payment_qr(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    service: Annotated[PaymentService, Depends(get_payment_service)],
    request_data: CreatePaymentQRRequest | None = None,
) -> PaymentQRResponse:
    """Create a pending subscription order and VietQR payment URL for the authenticated user."""
    plan_code = request_data.plan_code if request_data else "pro"
    try:
        order = await service.create_payment_qr(user_id=current_user.id, plan_code=plan_code)
    except PaymentValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    return PaymentQRResponse(
        data=PaymentQRData(
            payment_id=order.payment_id,
            reference_code=order.reference_code,
            amount_vnd=order.amount_vnd,
            qr_url=order.qr_url,
            account_number=order.account_number,
            bank_name=order.bank_name,
            status=order.status,
        )
    )


@router.get(
    "/{payment_id}/status",
    response_model=PaymentStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Poll payment order status",
)
async def get_payment_status(
    payment_id: Annotated[uuid.UUID, Path(description="Payment transaction ID")],
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    service: Annotated[PaymentService, Depends(get_payment_service)],
) -> PaymentStatusResponse:
    """Poll transaction status with strict tenant isolation (user owns transaction)."""
    try:
        dto = await service.get_payment_status(user_id=current_user.id, payment_id=payment_id)
    except PaymentNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e

    return PaymentStatusResponse(
        data=PaymentStatusData(
            payment_id=dto.payment_id,
            reference_code=dto.reference_code,
            amount_vnd=dto.amount_vnd,
            status=dto.status,
            paid_at=dto.paid_at,
            plan_code=dto.plan_code,
        )
    )


@router.post(
    "/sepay/webhook",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive and verify SePay bank transfer webhook",
)
async def handle_sepay_webhook(
    payload: dict[str, Any],
    service: Annotated[PaymentService, Depends(get_payment_service)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> WebhookResponse:
    """Process SePay payment gateway webhook notification."""
    try:
        processed = await service.process_sepay_webhook(
            payload=payload, auth_header=authorization
        )
    except PaymentAuthenticationError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        ) from e
    except PaymentValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    if not processed:
        return WebhookResponse(
            success=False,
            data=WebhookResultData(processed=False),
            message="Transfer content did not match any pending payment transaction.",
        )

    return WebhookResponse(
        success=True,
        data=WebhookResultData(processed=True),
        message="SePay webhook processed and subscription activated successfully.",
    )
