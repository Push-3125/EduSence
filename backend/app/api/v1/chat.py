"""AI Chatbot API router for multi-turn conversations and message history (US 2.12 / US 2.13).

Adheres strictly to:
- Invariant 2: Router stays thin; delegates all logic to ChatService. Never imports repositories.
- Invariant 6: Strict Tenant Isolation. Every operation uses authenticated current_user.id.
- Invariant 7: Handles QuotaExceededError (429) and QuotaServiceUnavailableError (503 fail-closed).
- Standard Envelope: { success: true, data: ..., message: ... } for all successful responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from app.core.security import get_current_active_user
from app.providers.gemini_adapter import (
    GeminiAdapterError,
    GeminiCircuitOpenError,
    GeminiRateLimitError,
    GeminiTimeoutError,
)
from app.services.auth_service import UserDTO
from app.services.chat_service import (
    ChatResponseDTO,
    ChatService,
    ConversationDTO,
    ConversationNotFoundError,
    InvalidMessageError,
    MessageDTO,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    get_chat_service,
)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------


class ConversationCreateRequest(BaseModel):
    """Payload schema for initiating a new AI conversation."""

    title: str | None = Field(
        default=None,
        max_length=255,
        description="Optional title for the conversation",
    )
    course_id: uuid.UUID | None = Field(
        default=None,
        description="Optional linked university course UUID",
    )
    conversation_type: str = Field(
        default="general",
        pattern="^(general|rag)$",
        description="Type of conversation ('general' or 'rag')",
    )

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str | None) -> str | None:
        """Strip surrounding whitespace from optional title."""
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None


class ConversationUpdateTitleRequest(BaseModel):
    """Payload schema for renaming conversation title."""

    title: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Updated conversation title",
    )

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Ensure title is not empty or whitespace only."""
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "value_error", "Conversation title cannot be empty or whitespace."
            )
        return stripped


class SendMessageRequest(BaseModel):
    """Payload schema for sending a user message to an AI conversation."""

    content: str = Field(
        ...,
        min_length=1,
        description="Student prompt / message text",
    )
    document_ids: list[uuid.UUID] | None = Field(
        default=None,
        description="Optional list of document UUIDs for scoping RAG retrieval",
    )

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        """Ensure prompt content is not empty or whitespace only."""
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "value_error", "Message content cannot be empty or whitespace."
            )
        return stripped


# ---------------------------------------------------------------------------
# Response Schemas (Standard Envelopes)
# ---------------------------------------------------------------------------


class MessageData(BaseModel):
    """Data payload for an individual message exchange."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    model_name: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int | None = None
    created_at: datetime


class ConversationData(BaseModel):
    """Data payload representing conversation header and summary."""

    id: uuid.UUID
    user_id: uuid.UUID
    title: str | None = None
    course_id: uuid.UUID | None = None
    conversation_type: str = "general"
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    last_message_preview: str | None = None


class ConversationWithMessagesData(BaseModel):
    """Data payload representing a full conversation with message history."""

    conversation: ConversationData
    messages: list[MessageData]


class ChatResponseData(BaseModel):
    """Data payload for AI assistant response with transparency labels."""

    message: MessageData
    is_ai_generated: bool = True
    disclaimer: str = (
        "AI-Generated response. Please verify critical academic information."
    )


class ConversationResponse(BaseModel):
    """Standardized API single conversation success response envelope."""

    success: bool = True
    data: ConversationData
    message: str = "Conversation operation successful."


class ConversationListData(BaseModel):
    """Data payload for paginated conversation listings."""

    items: list[ConversationData]
    total: int
    skip: int
    limit: int


class ConversationListResponse(BaseModel):
    """Standardized API paginated conversation list response envelope."""

    success: bool = True
    data: ConversationListData
    message: str = "Conversations retrieved successfully."


class ConversationDetailResponse(BaseModel):
    """Standardized API full conversation details with messages response envelope."""

    success: bool = True
    data: ConversationWithMessagesData
    message: str = "Conversation retrieved successfully."


class SendMessageResponse(BaseModel):
    """Standardized API response envelope for AI message generation."""

    success: bool = True
    data: ChatResponseData
    message: str = "Response generated successfully."


class ChatActionResponse(BaseModel):
    """Standardized API mutation action success response envelope."""

    success: bool = True
    message: str


# ---------------------------------------------------------------------------
# Mapper Helpers
# ---------------------------------------------------------------------------


def _to_conversation_data(dto: ConversationDTO) -> ConversationData:
    """Map ConversationDTO to presentation ConversationData schema."""
    return ConversationData(
        id=dto.id,
        user_id=dto.user_id,
        title=dto.title,
        course_id=dto.course_id,
        conversation_type=dto.conversation_type,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
        message_count=dto.message_count,
        last_message_preview=dto.last_message_preview,
    )


def _to_message_data(dto: MessageDTO) -> MessageData:
    """Map MessageDTO to presentation MessageData schema."""
    return MessageData(
        id=dto.id,
        conversation_id=dto.conversation_id,
        role=dto.role,
        content=dto.content,
        model_name=dto.model_name,
        prompt_tokens=dto.prompt_tokens,
        completion_tokens=dto.completion_tokens,
        latency_ms=dto.latency_ms,
        created_at=dto.created_at,
    )


def _to_chat_response_data(dto: ChatResponseDTO) -> ChatResponseData:
    """Map ChatResponseDTO to presentation ChatResponseData schema."""
    return ChatResponseData(
        message=_to_message_data(dto.message),
        is_ai_generated=dto.is_ai_generated,
        disclaimer=dto.disclaimer,
    )


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    payload: ConversationCreateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    chat_service: Annotated[ChatService, Depends(get_chat_service)],
) -> ConversationResponse:
    """Create a new AI chat conversation for the authenticated student."""
    dto = await chat_service.create_conversation(
        user_id=current_user.id,
        title=payload.title,
        course_id=payload.course_id,
        conversation_type=payload.conversation_type,
    )
    return ConversationResponse(
        success=True,
        data=_to_conversation_data(dto),
        message="Conversation created successfully.",
    )


@router.get(
    "/conversations",
    response_model=ConversationListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_conversations(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    chat_service: Annotated[ChatService, Depends(get_chat_service)],
    skip: Annotated[int, Query(ge=0, description="Offset items")] = 0,
    limit: Annotated[int, Query(ge=1, le=100, description="Page limit")] = 50,
    course_id: Annotated[uuid.UUID | None, Query(description="Course filter")] = None,
) -> ConversationListResponse:
    """List student conversations with message previews, paginated."""
    dtos, total = await chat_service.list_conversations(
        user_id=current_user.id,
        skip=skip,
        limit=limit,
        course_id=course_id,
    )
    return ConversationListResponse(
        success=True,
        data=ConversationListData(
            items=[_to_conversation_data(d) for d in dtos],
            total=total,
            skip=skip,
            limit=limit,
        ),
        message="Conversations retrieved successfully.",
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    status_code=status.HTTP_200_OK,
)
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    chat_service: Annotated[ChatService, Depends(get_chat_service)],
) -> ConversationDetailResponse:
    """Retrieve full conversation details with complete chronological message history."""
    try:
        conv_dto, msg_dtos = await chat_service.get_conversation(
            user_id=current_user.id,
            conversation_id=conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc

    return ConversationDetailResponse(
        success=True,
        data=ConversationWithMessagesData(
            conversation=_to_conversation_data(conv_dto),
            messages=[_to_message_data(m) for m in msg_dtos],
        ),
        message="Conversation retrieved successfully.",
    )


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    status_code=status.HTTP_200_OK,
)
async def update_conversation_title(
    conversation_id: uuid.UUID,
    payload: ConversationUpdateTitleRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    chat_service: Annotated[ChatService, Depends(get_chat_service)],
) -> ConversationResponse:
    """Rename a conversation title."""
    try:
        dto = await chat_service.update_title(
            user_id=current_user.id,
            conversation_id=conversation_id,
            title=payload.title,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc
    except InvalidMessageError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return ConversationResponse(
        success=True,
        data=_to_conversation_data(dto),
        message="Conversation title updated successfully.",
    )


@router.delete(
    "/conversations/{conversation_id}",
    response_model=ChatActionResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    chat_service: Annotated[ChatService, Depends(get_chat_service)],
) -> ChatActionResponse:
    """Permanently delete a conversation and its messages."""
    try:
        await chat_service.delete_conversation(
            user_id=current_user.id,
            conversation_id=conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc

    return ChatActionResponse(
        success=True,
        message="Conversation deleted successfully.",
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=SendMessageResponse,
    status_code=status.HTTP_200_OK,
)
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    chat_service: Annotated[ChatService, Depends(get_chat_service)],
) -> SendMessageResponse:
    """Send student prompt, execute Gemini multi-turn generation, track quota and telemetry."""
    plan_code = current_user.current_plan_code or "free"
    try:
        response_dto = await chat_service.send_message(
            user_id=current_user.id,
            conversation_id=conversation_id,
            content=payload.content,
            plan_code=plan_code,
            document_ids=payload.document_ids,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc
    except InvalidMessageError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except QuotaServiceUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except GeminiCircuitOpenError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI generation service is temporarily unavailable due to high error rates. Please try again shortly.",
        ) from exc
    except GeminiRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Upstream AI rate limit reached. Please try again shortly.",
        ) from exc
    except GeminiTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI generation timed out. Please try again.",
        ) from exc
    except GeminiAdapterError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI service error: {exc}",
        ) from exc

    return SendMessageResponse(
        success=True,
        data=_to_chat_response_data(response_dto),
        message="Response generated successfully.",
    )


__all__ = [
    "ChatActionResponse",
    "ChatResponseData",
    "ConversationCreateRequest",
    "ConversationData",
    "ConversationDetailResponse",
    "ConversationListData",
    "ConversationListResponse",
    "ConversationResponse",
    "ConversationUpdateTitleRequest",
    "ConversationWithMessagesData",
    "MessageData",
    "SendMessageRequest",
    "SendMessageResponse",
    "router",
]
