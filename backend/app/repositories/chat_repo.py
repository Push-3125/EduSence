"""Chat repository and SQLAlchemy models for AI conversations and message history (US 2.12 / US 2.13).

Enforces:
- Invariant 6: Strict Tenant Isolation. Every query filters by user_id.
- Invariant 2: Repository layer isolated from presentation and routers.
- Schema alignment: matches baseline PostgreSQL schema for ai_conversations and ai_messages.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload

from app.core.database import Base, get_db
from app.repositories.course_repo import Course
from app.repositories.user_repo import User


class AIConversation(Base):
    """AI conversation model representing ai_conversations table."""

    __tablename__ = "ai_conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conversation_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="general"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    messages: Mapped[list[AIMessage]] = relationship(
        "AIMessage",
        back_populates="conversation",
        order_by="AIMessage.created_at.asc()",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    user: Mapped[User] = relationship("User", lazy="select")
    course: Mapped[Course | None] = relationship("Course", lazy="select")

    __table_args__ = (
        CheckConstraint(
            "conversation_type IN ('general', 'rag')",
            name="ck_ai_conversations_type",
        ),
        Index("idx_ai_conversations_user_created", "user_id", "created_at"),
    )


class AIMessage(Base):
    """AI message model representing individual user and assistant exchanges in ai_messages."""

    __tablename__ = "ai_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    conversation: Mapped[AIConversation] = relationship(
        "AIConversation", back_populates="messages"
    )

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="ck_ai_messages_role"),
        CheckConstraint("prompt_tokens >= 0", name="ck_ai_messages_prompt_tokens"),
        CheckConstraint("completion_tokens >= 0", name="ck_ai_messages_completion_tokens"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="ck_ai_messages_latency"
        ),
        CheckConstraint("length(btrim(content)) > 0", name="ck_ai_messages_content"),
    )


class ChatRepository:
    """Repository handling database operations for AI conversations and messages."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_conversation(
        self,
        user_id: uuid.UUID,
        title: str | None = None,
        course_id: uuid.UUID | None = None,
        conversation_type: str = "general",
    ) -> AIConversation:
        """Create and persist a new AI conversation for the authenticated user.

        Args:
            user_id: Student user UUID.
            title: Optional conversation title.
            course_id: Optional course reference UUID.
            conversation_type: 'general' or 'rag'.

        Returns:
            Created AIConversation instance.
        """
        conversation = AIConversation(
            user_id=user_id,
            title=title.strip() if title else None,
            course_id=course_id,
            conversation_type=conversation_type,
        )
        self.session.add(conversation)
        await self.session.commit()
        await self.session.refresh(conversation)
        return conversation

    async def get_conversation(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> AIConversation | None:
        """Fetch a conversation strictly ensuring tenant isolation by user_id.

        Args:
            user_id: Authenticated user UUID.
            conversation_id: Conversation UUID.

        Returns:
            AIConversation if found and owned by user, else None.
        """
        stmt = (
            select(AIConversation)
            .options(selectinload(AIConversation.messages))
            .where(
                AIConversation.id == conversation_id,
                AIConversation.user_id == user_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_conversations(
        self,
        user_id: uuid.UUID,
        skip: int = 0,
        limit: int = 50,
        course_id: uuid.UUID | None = None,
    ) -> tuple[list[AIConversation], int]:
        """List paginated conversations for a user with total count.

        Args:
            user_id: Authenticated user UUID.
            skip: Offset number of items.
            limit: Maximum items to return.
            course_id: Optional course filter.

        Returns:
            Tuple of (list of AIConversation, total_count).
        """
        count_stmt = select(func.count(AIConversation.id)).where(
            AIConversation.user_id == user_id
        )
        if course_id is not None:
            count_stmt = count_stmt.where(AIConversation.course_id == course_id)
        total = await self.session.scalar(count_stmt) or 0

        query = (
            select(AIConversation)
            .options(selectinload(AIConversation.messages))
            .where(AIConversation.user_id == user_id)
        )
        if course_id is not None:
            query = query.where(AIConversation.course_id == course_id)

        query = query.order_by(AIConversation.updated_at.desc()).offset(skip).limit(limit)
        result = await self.session.execute(query)
        conversations = list(result.scalars().all())
        return conversations, total

    async def update_conversation_title(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        title: str,
    ) -> AIConversation | None:
        """Update conversation title while enforcing ownership.

        Args:
            user_id: Authenticated user UUID.
            conversation_id: Conversation UUID.
            title: New conversation title.

        Returns:
            Updated AIConversation if found and owned, else None.
        """
        conversation = await self.get_conversation(
            user_id=user_id, conversation_id=conversation_id
        )
        if conversation is None:
            return None

        conversation.title = title.strip()
        conversation.updated_at = datetime.now(UTC)
        await self.session.commit()
        await self.session.refresh(conversation)
        return conversation

    async def delete_conversation(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> bool:
        """Delete conversation and all its messages (cascade) enforcing ownership.

        Args:
            user_id: Authenticated user UUID.
            conversation_id: Conversation UUID.

        Returns:
            True if deleted, False if not found or unauthorized.
        """
        conversation = await self.get_conversation(
            user_id=user_id, conversation_id=conversation_id
        )
        if conversation is None:
            return False

        await self.session.delete(conversation)
        await self.session.commit()
        return True

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        model_name: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int | None = None,
    ) -> AIMessage:
        """Add and persist a message to a conversation, updating conversation updated_at.

        Args:
            conversation_id: Parent conversation UUID.
            role: 'user' or 'assistant'.
            content: Message text content.
            model_name: Optional LLM model identifier.
            prompt_tokens: Token count of prompt.
            completion_tokens: Token count of generated completion.
            latency_ms: Execution latency in ms.

        Returns:
            Created AIMessage instance.
        """
        clean_content = content.strip()
        message = AIMessage(
            conversation_id=conversation_id,
            role=role,
            content=clean_content,
            model_name=model_name,
            prompt_tokens=max(0, prompt_tokens),
            completion_tokens=max(0, completion_tokens),
            latency_ms=max(0, latency_ms) if latency_ms is not None else None,
        )
        self.session.add(message)

        # Touch parent conversation updated_at
        stmt = (
            update(AIConversation)
            .where(AIConversation.id == conversation_id)
            .values(updated_at=func.now())
        )
        await self.session.execute(stmt)

        await self.session.commit()
        await self.session.refresh(message)
        return message

    async def get_messages(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        limit: int = 100,
    ) -> list[AIMessage]:
        """Get messages belonging to a conversation, strictly verifying ownership.

        Args:
            user_id: Authenticated user UUID.
            conversation_id: Conversation UUID.
            limit: Maximum messages to return.

        Returns:
            List of AIMessage in chronological order (created_at ASC).
        """
        # Strict tenant check: verify user owns conversation
        conv = await self.get_conversation(user_id=user_id, conversation_id=conversation_id)
        if conv is None:
            return []

        stmt = (
            select(AIMessage)
            .where(AIMessage.conversation_id == conversation_id)
            .order_by(AIMessage.created_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


async def get_chat_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ChatRepository:
    """FastAPI dependency provider for ChatRepository."""
    return ChatRepository(session=session)


__all__ = [
    "AIConversation",
    "AIMessage",
    "ChatRepository",
    "get_chat_repo",
]
