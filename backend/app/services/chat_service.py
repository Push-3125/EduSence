"""AI Chat Service handling multi-turn conversations, quota reservation, and telemetry (US 2.12 / US 2.13).

Adheres strictly to:
- Invariant 2 (Layering): Routers -> Services -> Repositories.
- Invariant 4: Call Gemini ONLY through GeminiAdapter in app/providers/gemini_adapter.py.
- Invariant 6: Strict Tenant Isolation. All operations scoped to user_id.
- Invariant 7: Atomic quota reservation before LLM call via quota_service.reserve.
  If quota is exhausted -> QuotaExceededError (429).
  If Redis is down -> QuotaServiceUnavailableError (503 fail-closed).
- Telemetry: Log token metrics and cost via usage_logger.log_ai_call without storing prompts.
- US 2.13: Responses labeled "AI-Generated" with disclaimer.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends

from app.providers.gemini_adapter import (
    ChatMessage,
    GeminiAdapter,
    get_gemini_adapter,
)
from app.providers.usage_logger import (
    AIUsageLogger,
    get_ai_usage_logger,
)
from app.repositories.chat_repo import (
    AIConversation,
    AIMessage,
    ChatRepository,
    get_chat_repo,
)
from app.services.quota_service import (
    AIQuotaService,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    get_quota_service,
)

# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class ChatServiceError(Exception):
    """Base exception for all chat service domain errors."""


class ConversationNotFoundError(ChatServiceError):
    """Raised when conversation does not exist or does not belong to the user."""


class InvalidMessageError(ChatServiceError):
    """Raised when message content or input is invalid."""


# ---------------------------------------------------------------------------
# Domain Data Transfer Objects (DTOs)
# ---------------------------------------------------------------------------


@dataclass
class MessageDTO:
    """Decoupled DTO representing an AI chat message."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    model_name: str | None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int | None
    created_at: datetime


@dataclass
class ConversationDTO:
    """Decoupled DTO representing an AI conversation header and summary."""

    id: uuid.UUID
    user_id: uuid.UUID
    title: str | None
    course_id: uuid.UUID | None
    conversation_type: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    last_message_preview: str | None = None


@dataclass
class ChatResponseDTO:
    """Standardized DTO for AI-generated assistant replies."""

    message: MessageDTO
    is_ai_generated: bool = True
    disclaimer: str = (
        "AI-Generated response. Please verify critical academic information."
    )


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


def _to_message_dto(msg: AIMessage) -> MessageDTO:
    """Map AIMessage ORM model to MessageDTO."""
    return MessageDTO(
        id=msg.id,
        conversation_id=msg.conversation_id,
        role=msg.role,
        content=msg.content,
        model_name=msg.model_name,
        prompt_tokens=msg.prompt_tokens,
        completion_tokens=msg.completion_tokens,
        latency_ms=msg.latency_ms,
        created_at=msg.created_at,
    )


def _to_conversation_dto(conv: AIConversation) -> ConversationDTO:
    """Map AIConversation ORM model to ConversationDTO with message metrics."""
    messages = conv.messages if hasattr(conv, "messages") and conv.messages is not None else []
    message_count = len(messages)
    last_preview = None
    if messages:
        last_msg = messages[-1]
        last_preview = (
            last_msg.content[:100] + "..." if len(last_msg.content) > 100 else last_msg.content
        )

    return ConversationDTO(
        id=conv.id,
        user_id=conv.user_id,
        title=conv.title,
        course_id=conv.course_id,
        conversation_type=conv.conversation_type,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        message_count=message_count,
        last_message_preview=last_preview,
    )


# ---------------------------------------------------------------------------
# Chat Service Implementation
# ---------------------------------------------------------------------------


class ChatService:
    """Orchestrates AI chatbot conversation workflows, quota management, and telemetry."""

    DEFAULT_SYSTEM_INSTRUCTION = (
        "You are EduSense AI, an intelligent, helpful, and supportive academic assistant "
        "for university students. Provide clear, accurate, and structured educational explanations. "
        "Help students understand concepts, structure their thoughts, solve academic problems, and study effectively."
    )

    def __init__(
        self,
        chat_repo: ChatRepository,
        gemini_adapter: GeminiAdapter,
        quota_service: AIQuotaService,
        usage_logger: AIUsageLogger,
    ) -> None:
        self.chat_repo = chat_repo
        self.gemini_adapter = gemini_adapter
        self.quota_service = quota_service
        self.usage_logger = usage_logger

    async def create_conversation(
        self,
        user_id: uuid.UUID,
        title: str | None = None,
        course_id: uuid.UUID | None = None,
        conversation_type: str = "general",
    ) -> ConversationDTO:
        """Create a new chat conversation for a student.

        Args:
            user_id: Authenticated student UUID.
            title: Optional conversation title.
            course_id: Optional linked course UUID.
            conversation_type: 'general' or 'rag'.

        Returns:
            ConversationDTO of the newly created conversation.
        """
        clean_title = title.strip() if title else None
        conv = await self.chat_repo.create_conversation(
            user_id=user_id,
            title=clean_title,
            course_id=course_id,
            conversation_type=conversation_type,
        )
        return _to_conversation_dto(conv)

    async def get_conversation(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> tuple[ConversationDTO, list[MessageDTO]]:
        """Retrieve conversation and its messages enforcing tenant ownership.

        Args:
            user_id: Authenticated student UUID.
            conversation_id: Conversation UUID.

        Returns:
            Tuple of (ConversationDTO, list of MessageDTO).

        Raises:
            ConversationNotFoundError: If conversation is not found or unauthorized.
        """
        conv = await self.chat_repo.get_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if conv is None:
            raise ConversationNotFoundError("Conversation not found.")

        messages = await self.chat_repo.get_messages(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        msg_dtos = [_to_message_dto(m) for m in messages]
        return _to_conversation_dto(conv), msg_dtos

    async def list_conversations(
        self,
        user_id: uuid.UUID,
        skip: int = 0,
        limit: int = 50,
        course_id: uuid.UUID | None = None,
    ) -> tuple[list[ConversationDTO], int]:
        """List paginated conversations for the authenticated user.

        Args:
            user_id: Authenticated student UUID.
            skip: Number of records to skip.
            limit: Maximum records to return.
            course_id: Optional course filter.

        Returns:
            Tuple of (list of ConversationDTO, total_count).
        """
        convs, total = await self.chat_repo.list_conversations(
            user_id=user_id,
            skip=skip,
            limit=limit,
            course_id=course_id,
        )
        return [_to_conversation_dto(c) for c in convs], total

    async def update_title(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        title: str,
    ) -> ConversationDTO:
        """Update conversation title enforcing ownership and validation.

        Args:
            user_id: Authenticated student UUID.
            conversation_id: Conversation UUID.
            title: New conversation title.

        Returns:
            Updated ConversationDTO.

        Raises:
            InvalidMessageError: If title is empty or too long.
            ConversationNotFoundError: If conversation does not exist or unauthorized.
        """
        clean_title = title.strip() if title else ""
        if not clean_title:
            raise InvalidMessageError("Conversation title cannot be empty or whitespace.")
        if len(clean_title) > 255:
            raise InvalidMessageError("Conversation title cannot exceed 255 characters.")

        conv = await self.chat_repo.update_conversation_title(
            user_id=user_id,
            conversation_id=conversation_id,
            title=clean_title,
        )
        if conv is None:
            raise ConversationNotFoundError("Conversation not found.")

        return _to_conversation_dto(conv)

    async def delete_conversation(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> None:
        """Permanently delete a conversation enforcing tenant isolation.

        Args:
            user_id: Authenticated student UUID.
            conversation_id: Conversation UUID.

        Raises:
            ConversationNotFoundError: If conversation not found or unauthorized.
        """
        deleted = await self.chat_repo.delete_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if not deleted:
            raise ConversationNotFoundError("Conversation not found.")

    async def send_message(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        content: str,
        plan_code: str = "free",
    ) -> ChatResponseDTO:
        """Send a message to an AI conversation, execute multi-turn LLM generation, and track quota.

        Workflow:
        1. Validates conversation exists and belongs to user_id.
        2. Validates content is non-empty.
        3. Wraps with quota_service.reserve(user_id, plan_code):
           - If quota exceeded -> raises QuotaExceededError.
           - If Redis down -> raises QuotaServiceUnavailableError.
        4. Saves student user message in DB (role="user").
        5. Retrieves recent messages history (format as ChatMessage for GeminiAdapter).
        6. Measures latency, calls gemini_adapter.agenerate_chat(...).
        7. Saves assistant message in DB (role="assistant", model_name, tokens, latency).
        8. Logs usage asynchronously via usage_logger.log_ai_call(...).
        9. If conversation has default/no title and this is first exchange, generates or sets a short title.
        10. Returns ChatResponseDTO.

        Args:
            user_id: Authenticated student UUID.
            conversation_id: Conversation UUID.
            content: User message prompt content.
            plan_code: Subscription tier ('free' or 'pro').

        Returns:
            ChatResponseDTO containing generated assistant message and AI disclosure.

        Raises:
            ConversationNotFoundError: If conversation does not exist or unauthorized.
            InvalidMessageError: If message content is empty or whitespace.
            QuotaExceededError: If daily AI generation quota is reached.
            QuotaServiceUnavailableError: If Redis quota counter is unavailable.
        """
        # 1. Validates conversation exists and belongs to user_id
        conv = await self.chat_repo.get_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if conv is None:
            raise ConversationNotFoundError("Conversation not found.")

        # 2. Validates content is non-empty
        clean_content = content.strip() if content else ""
        if not clean_content:
            raise InvalidMessageError("Message content cannot be empty or whitespace.")

        # 3. Wraps with atomic quota reservation (auto refunds on failure)
        async with self.quota_service.reserve(user_id=user_id, plan_code=plan_code):
            # 4. Saves student user message in DB (role="user")
            await self.chat_repo.add_message(
                conversation_id=conversation_id,
                role="user",
                content=clean_content,
            )

            # 5. Retrieves recent messages history (format as ChatMessage for GeminiAdapter)
            history = await self.chat_repo.get_messages(
                user_id=user_id,
                conversation_id=conversation_id,
                limit=50,
            )
            chat_messages: list[ChatMessage] = []
            for msg in history:
                role = "model" if msg.role == "assistant" else "user"
                chat_messages.append(ChatMessage(role=role, content=msg.content))

            # 6. Measures latency, calls gemini_adapter.agenerate_chat(...)
            start_time = time.perf_counter()
            gemini_response = await self.gemini_adapter.agenerate_chat(
                messages=chat_messages,
                system_instruction=self.DEFAULT_SYSTEM_INSTRUCTION,
            )
            latency_ms = int((time.perf_counter() - start_time) * 1000)

            # 7. Saves assistant message in DB
            assistant_msg = await self.chat_repo.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=gemini_response.text,
                model_name=gemini_response.model,
                prompt_tokens=gemini_response.prompt_tokens,
                completion_tokens=gemini_response.completion_tokens,
                latency_ms=latency_ms,
            )

            # 8. Logs usage asynchronously via usage_logger.log_ai_call(...)
            await self.usage_logger.log_ai_call(
                user_id=user_id,
                feature="chat",
                model_name=gemini_response.model,
                prompt_tokens=gemini_response.prompt_tokens,
                completion_tokens=gemini_response.completion_tokens,
                total_tokens=gemini_response.total_tokens,
                latency_ms=latency_ms,
                request_status="success",
                conversation_id=conversation_id,
                message_id=assistant_msg.id,
            )

            # 9. If conversation has default/no title, set short title from first prompt
            if not conv.title or conv.title.strip() in ("", "New Conversation", "Untitled"):
                first_line = clean_content.splitlines()[0].strip()
                auto_title = (first_line[:47] + "...") if len(first_line) > 50 else first_line
                await self.chat_repo.update_conversation_title(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    title=auto_title,
                )

            # 10. Returns ChatResponseDTO
            return ChatResponseDTO(
                message=_to_message_dto(assistant_msg),
                is_ai_generated=True,
                disclaimer="AI-Generated response. Please verify critical academic information.",
            )


# ---------------------------------------------------------------------------
# Dependency Provider
# ---------------------------------------------------------------------------


def get_chat_service(
    chat_repo: Annotated[ChatRepository, Depends(get_chat_repo)],
    gemini_adapter: Annotated[GeminiAdapter, Depends(get_gemini_adapter)],
    quota_service: Annotated[AIQuotaService, Depends(get_quota_service)],
    usage_logger: Annotated[AIUsageLogger, Depends(get_ai_usage_logger)],
) -> ChatService:
    """FastAPI dependency provider for ChatService with fully resolved dependencies."""
    return ChatService(
        chat_repo=chat_repo,
        gemini_adapter=gemini_adapter,
        quota_service=quota_service,
        usage_logger=usage_logger,
    )


__all__ = [
    "ChatResponseDTO",
    "ChatService",
    "ChatServiceError",
    "ConversationDTO",
    "ConversationNotFoundError",
    "InvalidMessageError",
    "MessageDTO",
    "QuotaExceededError",
    "QuotaServiceUnavailableError",
    "get_chat_service",
]
