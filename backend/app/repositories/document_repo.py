"""Document repository and SQLAlchemy model for study documents catalog (US 2.10)."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, get_db
from app.repositories.course_repo import Course
from app.repositories.user_repo import User


class Document(Base):
    """Document metadata model representing documents table."""

    __tablename__ = "documents"

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
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    original_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunks_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processing_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship("User", lazy="select")
    course: Mapped["Course | None"] = relationship("Course", lazy="select")

    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_documents_id_user"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'embedded', 'failed')",
            name="ck_documents_status",
        ),
        CheckConstraint("file_size_bytes >= 0", name="ck_documents_file_size"),
        CheckConstraint(
            "file_type IN ('application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document') OR file_type LIKE 'image/%'",
            name="ck_documents_file_type",
        ),
        CheckConstraint("page_count IS NULL OR page_count > 0", name="ck_documents_page_count"),
        CheckConstraint(
            "total_chunks >= 0 AND chunks_processed >= 0 AND chunks_processed <= total_chunks",
            name="ck_documents_chunk_counts",
        ),
        CheckConstraint(
            "status <> 'embedded' OR (total_chunks > 0 AND chunks_processed = total_chunks)",
            name="ck_documents_embedded_complete",
        ),
    )


class DocumentRepository:
    """Repository handling database operations for Document model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: uuid.UUID,
        title: str,
        original_file_name: str,
        storage_object_key: str,
        file_type: str,
        file_size_bytes: int,
        course_id: uuid.UUID | None = None,
        page_count: int | None = None,
        status: str = "pending",
        id: uuid.UUID | None = None,
    ) -> Document:
        """Create and persist a new document metadata record.

        Args:
            user_id: Owner student UUID.
            title: Human-readable document title.
            original_file_name: Original upload filename.
            storage_object_key: Object storage key in private MinIO bucket.
            file_type: Verified MIME type string.
            file_size_bytes: Size of the file in bytes.
            course_id: Optional UUID of the associated course.
            page_count: Optional extracted page count.
            status: Document processing status ('pending', 'processing', 'embedded', 'failed').
            id: Optional pre-generated Document UUID.

        Returns:
            The created Document instance.
        """
        doc = Document(
            id=id or uuid.uuid4(),
            user_id=user_id,
            course_id=course_id,
            title=title.strip(),
            original_file_name=original_file_name.strip(),
            storage_object_key=storage_object_key.strip(),
            file_type=file_type.strip(),
            file_size_bytes=file_size_bytes,
            page_count=page_count,
            status=status,
            total_chunks=0,
            chunks_processed=0,
            processing_error_code=None,
        )
        self.session.add(doc)
        await self.session.flush()
        return doc

    async def get_by_id_and_user(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Document | None:
        """Fetch a document by ID enforcing strict tenant isolation.

        Args:
            document_id: Document UUID.
            user_id: Authenticated user UUID.

        Returns:
            Document instance if found and owned by user_id, else None.
        """
        stmt = select(Document).where(
            Document.id == document_id,
            Document.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        status: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Document], int]:
        """List documents owned by a student with optional course/status filters.

        Args:
            user_id: Authenticated user UUID.
            course_id: Optional course filter UUID.
            status: Optional processing status filter.
            skip: Pagination offset.
            limit: Pagination limit.

        Returns:
            Tuple of (list of documents, total matching count).
        """
        stmt = select(Document).where(Document.user_id == user_id)
        count_stmt = select(func.count()).select_from(Document).where(Document.user_id == user_id)

        if course_id is not None:
            stmt = stmt.where(Document.course_id == course_id)
            count_stmt = count_stmt.where(Document.course_id == course_id)

        if status is not None:
            stmt = stmt.where(Document.status == status)
            count_stmt = count_stmt.where(Document.status == status)

        stmt = stmt.order_by(Document.created_at.desc()).offset(skip).limit(limit)

        total_res = await self.session.execute(count_stmt)
        total = total_res.scalar() or 0

        result = await self.session.execute(stmt)
        documents = list(result.scalars().all())
        return documents, total

    async def update(self, document: Document, **kwargs: Any) -> Document:
        """Update fields on a document instance and flush changes.

        Args:
            document: Existing Document instance.
            **kwargs: Attributes to update.

        Returns:
            Updated Document instance.
        """
        allowed_fields = {
            "title",
            "course_id",
            "page_count",
            "status",
            "total_chunks",
            "chunks_processed",
            "processing_error_code",
        }
        for key, value in kwargs.items():
            if key in allowed_fields:
                setattr(document, key, value)

        document.updated_at = datetime.now(UTC)
        await self.session.flush()
        return document

    async def delete(self, document: Document) -> None:
        """Delete a document record from the database.

        Args:
            document: Document instance to remove.
        """
        await self.session.delete(document)
        await self.session.flush()


def get_document_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> DocumentRepository:
    """FastAPI dependency provider for DocumentRepository."""
    return DocumentRepository(session)


__all__ = [
    "Document",
    "DocumentRepository",
    "get_document_repo",
]
