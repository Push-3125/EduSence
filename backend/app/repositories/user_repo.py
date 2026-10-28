"""User repository and SQLAlchemy models for User, UserProfile, Role, UserRole, RefreshSession, and VerificationToken."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload

from app.core.database import Base, get_db


class SubscriptionPlan(Base):
    """Subscription plan catalog model representing subscription_plans table."""

    __tablename__ = "subscription_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price_vnd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(Base):
    """User account model representing users table."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_plan_code: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("subscription_plans.code", onupdate="CASCADE", ondelete="RESTRICT"),
        default="free",
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    profile: Mapped["UserProfile"] = relationship(
        "UserProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    roles: Mapped[list["Role"]] = relationship(
        "Role",
        secondary="user_roles",
        back_populates="users",
        lazy="selectin",
    )
    refresh_sessions: Mapped[list["RefreshSession"]] = relationship(
        "RefreshSession",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="select",
    )
    verification_tokens: Mapped[list["VerificationToken"]] = relationship(
        "VerificationToken",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="select",
    )


class UserProfile(Base):
    """Personal profile information model representing user_profiles table."""

    __tablename__ = "user_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    university: Mapped[str | None] = mapped_column(String(255), nullable=True)
    major: Mapped[str | None] = mapped_column(String(255), nullable=True)
    academic_year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="profile")


class Role(Base):
    """RBAC Role model representing roles table."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    users: Mapped[list["User"]] = relationship(
        "User",
        secondary="user_roles",
        back_populates="roles",
        lazy="selectin",
    )


class UserRole(Base):
    """User to Role association model representing user_roles table."""

    __tablename__ = "user_roles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RefreshSession(Base):
    """Refresh session persistence model representing refresh_sessions table."""

    __tablename__ = "refresh_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(
        INET().with_variant(String(45), "sqlite"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="refresh_sessions")


class VerificationToken(Base):
    """Verification and password reset token model representing verification_tokens table."""

    __tablename__ = "verification_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        SmallInteger, default=0, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="verification_tokens")


class UserRepository:
    """Repository handling database operations for User, UserProfile, RefreshSession, and VerificationToken."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        """Fetch user by primary UUID with profile and roles eagerly loaded."""
        stmt = (
            select(User)
            .options(selectinload(User.profile), selectinload(User.roles))
            .where(User.id == user_id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        """Fetch user by email (case-insensitive) with profile and roles eagerly loaded."""
        stmt = (
            select(User)
            .options(selectinload(User.profile), selectinload(User.roles))
            .where(func.lower(User.email) == email.lower().strip())
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_role_by_name(self, name: str) -> Role | None:
        """Fetch an RBAC role by its unique name."""
        stmt = select(Role).where(Role.name == name)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_user(
        self,
        email: str,
        password_hash: str,
        full_name: str,
        role_name: str = "student",
        current_plan_code: str = "free",
        is_email_verified: bool = False,
        avatar_url: str | None = None,
    ) -> User:
        """Create a user, associated user profile, and assign default RBAC role.

        Args:
            email: Unique user email address.
            password_hash: Secure Argon2id hashed password.
            full_name: Student full name.
            role_name: RBAC role name (defaults to 'student').
            current_plan_code: Subscription plan code (defaults to 'free').
            is_email_verified: Whether the email is pre-verified (e.g. via OAuth).
            avatar_url: Optional initial profile avatar URL.

        Returns:
            The newly created User model instance with profile and roles loaded.
        """
        role = await self.get_role_by_name(role_name)
        if not role:
            role = Role(name=role_name, description=f"Default {role_name} role.")
            self.session.add(role)
            await self.session.flush()

        user = User(
            email=email.lower().strip(),
            password_hash=password_hash,
            current_plan_code=current_plan_code,
            is_active=True,
            is_email_verified=is_email_verified,
        )
        self.session.add(user)
        await self.session.flush()

        profile = UserProfile(
            user_id=user.id,
            full_name=full_name.strip(),
            avatar_url=avatar_url,
        )
        self.session.add(profile)

        user_role = UserRole(
            user_id=user.id,
            role_id=role.id,
        )
        self.session.add(user_role)
        await self.session.flush()

        # Link instances in memory so caller has immediate access
        user.profile = profile
        user.roles = [role]

        return user

    async def create_oauth_user(
        self,
        email: str,
        password_hash: str,
        full_name: str,
        avatar_url: str | None = None,
        role_name: str = "student",
        current_plan_code: str = "free",
    ) -> User:
        """Helper to create an OAuth-authenticated user with verified email."""
        return await self.create_user(
            email=email,
            password_hash=password_hash,
            full_name=full_name,
            role_name=role_name,
            current_plan_code=current_plan_code,
            is_email_verified=True,
            avatar_url=avatar_url,
        )

    async def mark_email_verified(self, user_id: uuid.UUID) -> None:
        """Mark user's email address as verified."""
        stmt = select(User).where(User.id == user_id)
        result = await self.session.execute(stmt)
        user = result.scalar_one_or_none()
        if user:
            user.is_email_verified = True
            await self.session.flush()

    async def get_profile_by_user_id(
        self, user_id: uuid.UUID
    ) -> UserProfile | None:
        """Fetch user profile by user UUID with user relationship loaded."""
        stmt = (
            select(UserProfile)
            .options(selectinload(UserProfile.user))
            .where(UserProfile.user_id == user_id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_profile(
        self, user_id: uuid.UUID, **fields: Any
    ) -> UserProfile | None:
        """Update fields on a user profile record and flush changes."""
        stmt = (
            select(UserProfile)
            .options(selectinload(UserProfile.user))
            .where(UserProfile.user_id == user_id)
        )
        result = await self.session.execute(stmt)
        profile = result.scalar_one_or_none()
        if not profile:
            return None

        allowed_fields = {
            "full_name",
            "display_name",
            "avatar_url",
            "phone_number",
            "university",
            "major",
            "academic_year",
        }
        for key, value in fields.items():
            if key in allowed_fields:
                setattr(profile, key, value)

        profile.updated_at = datetime.now(UTC)
        await self.session.flush()
        return profile

    async def update_profile_avatar(
        self, user_id: uuid.UUID, avatar_url: str
    ) -> None:
        """Update user profile avatar URL."""
        stmt = select(UserProfile).where(UserProfile.user_id == user_id)
        result = await self.session.execute(stmt)
        profile = result.scalar_one_or_none()
        if profile:
            profile.avatar_url = avatar_url
            await self.session.flush()

    async def update_last_login(
        self,
        user_id: uuid.UUID,
        login_at: datetime | None = None,
    ) -> None:
        """Update last_login_at timestamp on a user record."""
        stmt = select(User).where(User.id == user_id)
        result = await self.session.execute(stmt)
        user = result.scalar_one_or_none()
        if user:
            user.last_login_at = login_at or datetime.now(UTC)
            await self.session.flush()

    async def create_refresh_session(
        self,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshSession:
        """Create and persist a new refresh session."""
        session = RefreshSession(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self.session.add(session)
        await self.session.flush()
        return session

    async def get_refresh_session_by_hash(
        self, token_hash: str
    ) -> RefreshSession | None:
        """Lookup a refresh session by its SHA-256 token hash."""
        stmt = select(RefreshSession).where(RefreshSession.token_hash == token_hash)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def revoke_refresh_session(
        self,
        token_hash: str,
        revoked_at: datetime | None = None,
    ) -> bool:
        """Revoke an active refresh session by its token hash."""
        stmt = select(RefreshSession).where(
            RefreshSession.token_hash == token_hash,
            RefreshSession.revoked_at.is_(None),
        )
        result = await self.session.execute(stmt)
        session = result.scalar_one_or_none()
        if not session:
            return False
        session.revoked_at = revoked_at or datetime.now(UTC)
        await self.session.flush()
        return True

    async def revoke_all_user_sessions(
        self,
        user_id: uuid.UUID,
        revoked_at: datetime | None = None,
    ) -> int:
        """Revoke all active refresh sessions for a specific user."""
        now = revoked_at or datetime.now(UTC)
        stmt = select(RefreshSession).where(
            RefreshSession.user_id == user_id,
            RefreshSession.revoked_at.is_(None),
        )
        result = await self.session.execute(stmt)
        sessions = result.scalars().all()
        for s in sessions:
            s.revoked_at = now
        if sessions:
            await self.session.flush()
        return len(sessions)

    async def update_session_last_used(
        self,
        session_id: uuid.UUID,
        last_used_at: datetime | None = None,
    ) -> None:
        """Update last_used_at timestamp on a refresh session."""
        stmt = select(RefreshSession).where(RefreshSession.id == session_id)
        result = await self.session.execute(stmt)
        session = result.scalar_one_or_none()
        if session:
            session.last_used_at = last_used_at or datetime.now(UTC)
            await self.session.flush()

    async def create_verification_token(
        self,
        user_id: uuid.UUID,
        purpose: str,
        token_hash: str,
        expires_at: datetime,
    ) -> VerificationToken:
        """Create and persist a single-use verification or password reset token."""
        token_record = VerificationToken(
            user_id=user_id,
            purpose=purpose,
            token_hash=token_hash,
            expires_at=expires_at,
            attempt_count=0,
        )
        self.session.add(token_record)
        await self.session.flush()
        return token_record

    async def get_valid_verification_token(
        self,
        token_hash: str,
        purpose: str,
    ) -> VerificationToken | None:
        """Lookup an unconsumed, unexpired verification token by token hash and purpose."""
        now = datetime.now(UTC)
        stmt = select(VerificationToken).where(
            VerificationToken.token_hash == token_hash,
            VerificationToken.purpose == purpose,
            VerificationToken.consumed_at.is_(None),
            VerificationToken.expires_at > now,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_verification_token_by_hash(
        self,
        token_hash: str,
        purpose: str | None = None,
    ) -> VerificationToken | None:
        """Lookup any verification token by its hash regardless of expiration or consumption."""
        stmt = select(VerificationToken).where(VerificationToken.token_hash == token_hash)
        if purpose:
            stmt = stmt.where(VerificationToken.purpose == purpose)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def consume_verification_token(
        self,
        token_id: uuid.UUID,
        consumed_at: datetime | None = None,
    ) -> bool:
        """Mark a verification token as consumed at current or specified timestamp."""
        stmt = select(VerificationToken).where(VerificationToken.id == token_id)
        result = await self.session.execute(stmt)
        token_record = result.scalar_one_or_none()
        if not token_record:
            return False
        token_record.consumed_at = consumed_at or datetime.now(UTC)
        await self.session.flush()
        return True

    async def increment_token_attempts(self, token_id: uuid.UUID) -> int:
        """Increment attempt_count for a verification token."""
        stmt = select(VerificationToken).where(VerificationToken.id == token_id)
        result = await self.session.execute(stmt)
        token_record = result.scalar_one_or_none()
        if token_record:
            token_record.attempt_count += 1
            await self.session.flush()
            return token_record.attempt_count
        return 0

    async def update_user_password(
        self,
        user_id: uuid.UUID,
        password_hash: str,
    ) -> bool:
        """Update password_hash for a user."""
        stmt = select(User).where(User.id == user_id)
        result = await self.session.execute(stmt)
        user = result.scalar_one_or_none()
        if not user:
            return False
        user.password_hash = password_hash
        user.updated_at = datetime.now(UTC)
        await self.session.flush()
        return True


def get_user_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> UserRepository:
    """FastAPI dependency provider for UserRepository with an active database session."""
    return UserRepository(session)
