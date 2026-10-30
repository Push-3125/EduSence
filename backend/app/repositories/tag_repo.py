"""Tag repository and SQLAlchemy models for document tags management (US 2.11)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, get_db


class Tag(Base):
    """User-scoped tag model for organizing study documents."""

    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "normalized_name", name="uq_tags_user_normalized_name"),
        UniqueConstraint("id", "user_id", name="uq_tags_id_user"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_tags_name"),
    )


class DocumentTag(Base):
    """Associative model linking documents to tags with composite user_id foreign keys."""

    __tablename__ = "document_tags"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint("document_id", "tag_id", name="pk_document_tags"),
        ForeignKeyConstraint(
            ["document_id", "user_id"],
            ["documents.id", "documents.user_id"],
            name="fk_document_tags_document_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tag_id", "user_id"],
            ["tags.id", "tags.user_id"],
            name="fk_document_tags_tag_owner",
            ondelete="CASCADE",
        ),
    )


class TagRepository:
    """Repository handling database operations for Tag and DocumentTag models."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_tag(self, user_id: uuid.UUID, name: str) -> Tag:
        """Create and persist a new tag with stripped and normalized names.

        Args:
            user_id: Owner user UUID.
            name: Human-readable tag name.

        Returns:
            Created Tag instance.
        """
        clean_name = name.strip()
        normalized = clean_name.lower()
        tag = Tag(
            id=uuid.uuid4(),
            user_id=user_id,
            name=clean_name,
            normalized_name=normalized,
        )
        self.session.add(tag)
        await self.session.flush()
        return tag

    async def get_tag_by_id(self, user_id: uuid.UUID, tag_id: uuid.UUID) -> Tag | None:
        """Fetch a tag by ID ensuring user tenant isolation.

        Args:
            user_id: Authenticated user UUID.
            tag_id: Tag UUID.

        Returns:
            Tag instance if found and owned by user_id, else None.
        """
        stmt = select(Tag).where(Tag.id == tag_id, Tag.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_tag_by_normalized_name(
        self, user_id: uuid.UUID, normalized_name: str
    ) -> Tag | None:
        """Fetch a tag by normalized name for a specific user.

        Args:
            user_id: Authenticated user UUID.
            normalized_name: Lowercased stripped tag name.

        Returns:
            Tag instance if found, else None.
        """
        stmt = select(Tag).where(
            Tag.user_id == user_id,
            Tag.normalized_name == normalized_name.strip().lower(),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_or_create_tag(self, user_id: uuid.UUID, name: str) -> Tag:
        """Retrieve existing tag by normalized name or create a new one.

        Args:
            user_id: Authenticated user UUID.
            name: Tag name.

        Returns:
            Existing or newly created Tag instance.
        """
        clean_name = name.strip()
        normalized = clean_name.lower()
        existing = await self.get_tag_by_normalized_name(user_id, normalized)
        if existing:
            return existing
        return await self.create_tag(user_id=user_id, name=clean_name)

    async def list_tags_by_user(self, user_id: uuid.UUID) -> list[tuple[Tag, int]]:
        """List all tags belonging to user alongside their document counts.

        Args:
            user_id: Authenticated user UUID.

        Returns:
            List of tuples of (Tag, document_count) ordered alphabetically by name.
        """
        stmt = (
            select(Tag, func.count(DocumentTag.document_id))
            .outerjoin(
                DocumentTag,
                (DocumentTag.tag_id == Tag.id) & (DocumentTag.user_id == Tag.user_id),
            )
            .where(Tag.user_id == user_id)
            .group_by(Tag.id)
            .order_by(Tag.name.asc())
        )
        result = await self.session.execute(stmt)
        return [(row[0], int(row[1])) for row in result.all()]

    async def delete_tag(self, user_id: uuid.UUID, tag_id: uuid.UUID) -> bool:
        """Delete a tag owned by the user.

        Args:
            user_id: Authenticated user UUID.
            tag_id: Tag UUID to delete.

        Returns:
            True if tag existed and was deleted, False otherwise.
        """
        tag = await self.get_tag_by_id(user_id, tag_id)
        if not tag:
            return False
        await self.session.delete(tag)
        await self.session.flush()
        return True

    async def tag_document(
        self, user_id: uuid.UUID, document_id: uuid.UUID, tag_id: uuid.UUID
    ) -> DocumentTag:
        """Attach a tag to a document enforcing user ownership.

        Args:
            user_id: Authenticated user UUID.
            document_id: Document UUID.
            tag_id: Tag UUID.

        Returns:
            DocumentTag association instance.
        """
        stmt = select(DocumentTag).where(
            DocumentTag.document_id == document_id,
            DocumentTag.tag_id == tag_id,
            DocumentTag.user_id == user_id,
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing:
            return existing

        doc_tag = DocumentTag(
            document_id=document_id,
            tag_id=tag_id,
            user_id=user_id,
        )
        self.session.add(doc_tag)
        await self.session.flush()
        return doc_tag

    async def untag_document(
        self, user_id: uuid.UUID, document_id: uuid.UUID, tag_id: uuid.UUID
    ) -> bool:
        """Remove a tag association from a document.

        Args:
            user_id: Authenticated user UUID.
            document_id: Document UUID.
            tag_id: Tag UUID.

        Returns:
            True if association was removed, False if it was not found.
        """
        stmt = select(DocumentTag).where(
            DocumentTag.document_id == document_id,
            DocumentTag.tag_id == tag_id,
            DocumentTag.user_id == user_id,
        )
        doc_tag = (await self.session.execute(stmt)).scalar_one_or_none()
        if not doc_tag:
            return False
        await self.session.delete(doc_tag)
        await self.session.flush()
        return True

    async def get_tags_for_document(
        self, user_id: uuid.UUID, document_id: uuid.UUID
    ) -> list[Tag]:
        """Fetch all tags attached to a specific document.

        Args:
            user_id: Authenticated user UUID.
            document_id: Document UUID.

        Returns:
            List of Tag instances ordered alphabetically by name.
        """
        stmt = (
            select(Tag)
            .join(
                DocumentTag,
                (DocumentTag.tag_id == Tag.id) & (DocumentTag.user_id == Tag.user_id),
            )
            .where(
                DocumentTag.document_id == document_id,
                DocumentTag.user_id == user_id,
            )
            .order_by(Tag.name.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_tags_for_documents(
        self, user_id: uuid.UUID, document_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[Tag]]:
        """Fetch attached tags in batch for multiple documents.

        Args:
            user_id: Authenticated user UUID.
            document_ids: List of Document UUIDs.

        Returns:
            Dictionary mapping Document UUID to list of Tag instances.
        """
        if not document_ids:
            return {}

        mapping: dict[uuid.UUID, list[Tag]] = {doc_id: [] for doc_id in document_ids}
        stmt = (
            select(DocumentTag.document_id, Tag)
            .join(
                Tag,
                (Tag.id == DocumentTag.tag_id) & (Tag.user_id == DocumentTag.user_id),
            )
            .where(
                DocumentTag.document_id.in_(document_ids),
                DocumentTag.user_id == user_id,
            )
            .order_by(Tag.name.asc())
        )
        result = await self.session.execute(stmt)
        for doc_id, tag in result.all():
            mapping[doc_id].append(tag)
        return mapping


def get_tag_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TagRepository:
    """FastAPI dependency provider for TagRepository."""
    return TagRepository(session)


__all__ = [
    "DocumentTag",
    "Tag",
    "TagRepository",
    "get_tag_repo",
]
