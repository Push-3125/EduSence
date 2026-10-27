"""Security primitives, Argon2id password hashing, token utilities, and JWT management."""

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher, Type, exceptions

from app.core.config import settings

# OWASP Recommended Defaults for Argon2id
DEFAULT_ARGON2_TIME_COST: int = 3
DEFAULT_ARGON2_MEMORY_COST: int = 65536  # 64 MiB
DEFAULT_ARGON2_PARALLELISM: int = 4
DEFAULT_ARGON2_HASH_LEN: int = 32
DEFAULT_ARGON2_SALT_LEN: int = 16

# Standard Token Types
TOKEN_TYPE_ACCESS: str = "access"
TOKEN_TYPE_REFRESH: str = "refresh"
TOKEN_TYPE_VERIFICATION: str = "verification"
TOKEN_TYPE_PASSWORD_RESET: str = "password_reset"


class SecurityError(Exception):
    """Base exception for all security and cryptographic operations."""


class PasswordStrengthError(SecurityError):
    """Raised when a password fails complexity or length requirements."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class TokenError(SecurityError):
    """Base exception for JWT and cryptographic token handling errors."""


class TokenExpiredError(TokenError):
    """Raised when a JWT has expired."""


class InvalidTokenError(TokenError):
    """Raised when a token signature, payload, or type validation fails."""


class PasswordHasherService:
    """Argon2id password hashing and verification service aligned with OWASP standards."""

    def __init__(
        self,
        time_cost: int = DEFAULT_ARGON2_TIME_COST,
        memory_cost: int = DEFAULT_ARGON2_MEMORY_COST,
        parallelism: int = DEFAULT_ARGON2_PARALLELISM,
        hash_len: int = DEFAULT_ARGON2_HASH_LEN,
        salt_len: int = DEFAULT_ARGON2_SALT_LEN,
    ) -> None:
        self.time_cost = time_cost
        self.memory_cost = memory_cost
        self.parallelism = parallelism
        self.hash_len = hash_len
        self.salt_len = salt_len
        self._hasher = PasswordHasher(
            time_cost=self.time_cost,
            memory_cost=self.memory_cost,
            parallelism=self.parallelism,
            hash_len=self.hash_len,
            salt_len=self.salt_len,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        """Hash a plain text password with Argon2id.

        Args:
            password: The plain text password to hash.

        Returns:
            The Argon2id hash string.

        Raises:
            ValueError: If password is empty or exceeds maximum safe length (1024 chars).
        """
        if not password:
            raise ValueError("Password cannot be empty.")
        if len(password) > 1024:
            raise ValueError("Password exceeds maximum allowed length of 1024 characters.")
        return self._hasher.hash(password)

    def verify(self, plain_password: str, hashed_password: str) -> bool:
        """Verify a plain text password against an Argon2id hash.

        Args:
            plain_password: The plain text candidate password.
            hashed_password: The stored Argon2id hash string.

        Returns:
            True if verification succeeds, False on mismatch or corrupted hash.
        """
        if not plain_password or not hashed_password:
            return False
        try:
            return self._hasher.verify(hashed_password, plain_password)
        except (
            exceptions.VerifyMismatchError,
            exceptions.InvalidHash,
            exceptions.VerificationError,
            exceptions.HashingError,
            exceptions.Argon2Error,
            ValueError,
        ):
            return False

    def needs_rehash(self, hashed_password: str) -> bool:
        """Check if an existing hash was produced with parameters weaker than current configuration.

        Args:
            hashed_password: The Argon2id hash string to evaluate.

        Returns:
            True if the password should be rehashed upon login, False otherwise.
        """
        try:
            return self._hasher.check_needs_rehash(hashed_password)
        except (exceptions.InvalidHash, exceptions.Argon2Error, ValueError):
            return True


# Default singleton instance configured with OWASP parameters
_default_password_hasher = PasswordHasherService()


def hash_password(password: str) -> str:
    """Hash a password using the default Argon2id configuration."""
    return _default_password_hasher.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against an Argon2id hash safely without timing leaks."""
    return _default_password_hasher.verify(plain_password, hashed_password)


def check_needs_rehash(hashed_password: str) -> bool:
    """Check if an Argon2id hash requires rehashing with updated security parameters."""
    return _default_password_hasher.needs_rehash(hashed_password)


# Password Policy and Strength Validation
_SPECIAL_CHAR_REGEX = re.compile(r"""[!@#$%^&*()_+\-=\[\]{}|;':",.<>/?~`\\]""")


def validate_password_strength(
    password: str,
    min_length: int = 8,
    max_length: int = 128,
) -> tuple[bool, list[str]]:
    """Validate password strength against university security requirements.

    Requirements:
        - Minimum length (default 8 characters)
        - Maximum length (default 128 characters)
        - At least one uppercase letter (A-Z)
        - At least one lowercase letter (a-z)
        - At least one digit (0-9)
        - At least one special character

    Args:
        password: The candidate password.
        min_length: Minimum character length required.
        max_length: Maximum character length allowed.

    Returns:
        Tuple of (is_valid, list of human-readable violation descriptions).
    """
    errors: list[str] = []

    if len(password) < min_length:
        errors.append(f"Password must be at least {min_length} characters long.")
    if len(password) > max_length:
        errors.append(f"Password must not exceed {max_length} characters.")
    if not any(c.isupper() for c in password):
        errors.append("Password must contain at least one uppercase letter.")
    if not any(c.islower() for c in password):
        errors.append("Password must contain at least one lowercase letter.")
    if not any(c.isdigit() for c in password):
        errors.append("Password must contain at least one numeric digit.")
    if not _SPECIAL_CHAR_REGEX.search(password):
        errors.append("Password must contain at least one special character.")

    return len(errors) == 0, errors


def enforce_password_strength(
    password: str,
    min_length: int = 8,
    max_length: int = 128,
) -> None:
    """Enforce password strength policy, raising PasswordStrengthError if invalid.

    Args:
        password: The candidate password.
        min_length: Minimum required length.
        max_length: Maximum allowed length.

    Raises:
        PasswordStrengthError: If one or more complexity requirements are not met.
    """
    is_valid, errors = validate_password_strength(
        password,
        min_length=min_length,
        max_length=max_length,
    )
    if not is_valid:
        error_summary = "Password does not meet security requirements: " + "; ".join(errors)
        raise PasswordStrengthError(error_summary, errors=errors)


# Cryptographic Random Token Primitives
def generate_secure_token(nbytes: int = 32) -> str:
    """Generate a cryptographically secure URL-safe random string.

    Args:
        nbytes: Number of random bytes of entropy.

    Returns:
        A URL-safe base64-encoded string.
    """
    return secrets.token_urlsafe(nbytes)


def generate_numeric_otp(length: int = 6) -> str:
    """Generate a cryptographically secure numeric one-time verification code.

    Args:
        length: Length of the numeric code (default: 6 digits).

    Returns:
        A numeric string of the requested length.
    """
    if length <= 0:
        raise ValueError("OTP length must be a positive integer.")
    digits = "0123456789"
    return "".join(secrets.choice(digits) for _ in range(length))


def hash_token(token: str) -> str:
    """Compute a deterministic SHA-256 hex digest for sensitive tokens.

    Safe for persistent storage in verification_tokens and refresh_sessions tables.

    Args:
        token: Plain text token.

    Returns:
        64-character lowercase hexadecimal SHA-256 digest.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token_hash(token: str, expected_hash: str) -> bool:
    """Verify a plain token against its stored hash using constant-time comparison.

    Args:
        token: Plain text candidate token.
        expected_hash: Stored SHA-256 hex digest.

    Returns:
        True if the token matches the expected hash, False otherwise.
    """
    candidate_hash = hash_token(token)
    return hmac.compare_digest(candidate_hash, expected_hash)


# JWT Management Primitives
def create_access_token(
    subject: str | Any,
    extra_claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
    secret_key: str | None = None,
    algorithm: str | None = None,
) -> str:
    """Create a signed JWT access token.

    Args:
        subject: Subject identifier (typically user UUID as string).
        extra_claims: Additional claims to include in the token payload.
        expires_delta: Optional custom lifespan (defaults to settings.access_token_expire_minutes).
        secret_key: Optional override for signing key (defaults to settings.secret_key).
        algorithm: Optional override for signing algorithm (defaults to settings.algorithm).

    Returns:
        Encoded and signed JWT string.
    """
    key = secret_key or settings.secret_key
    algo = algorithm or settings.algorithm

    now = datetime.now(UTC)
    delta = expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    expire = now + delta

    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": TOKEN_TYPE_ACCESS,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": str(uuid.uuid4()),
    }

    if extra_claims:
        for claim_key, claim_val in extra_claims.items():
            if claim_key not in ("sub", "type", "iat", "exp", "jti"):
                payload[claim_key] = claim_val

    return jwt.encode(payload, key, algorithm=algo)


def create_refresh_token(
    subject: str | Any,
    extra_claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
    secret_key: str | None = None,
    algorithm: str | None = None,
) -> str:
    """Create a signed JWT refresh token with unique JTI for session tracking and revocation.

    Args:
        subject: Subject identifier (typically user UUID as string).
        extra_claims: Additional claims to include in the token payload.
        expires_delta: Optional custom lifespan (defaults to settings.refresh_token_expire_days).
        secret_key: Optional override for signing key (defaults to settings.secret_key).
        algorithm: Optional override for signing algorithm (defaults to settings.algorithm).

    Returns:
        Encoded and signed JWT refresh token string.
    """
    key = secret_key or settings.secret_key
    algo = algorithm or settings.algorithm

    now = datetime.now(UTC)
    delta = expires_delta or timedelta(days=settings.refresh_token_expire_days)
    expire = now + delta

    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": TOKEN_TYPE_REFRESH,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": str(uuid.uuid4()),
    }

    if extra_claims:
        for claim_key, claim_val in extra_claims.items():
            if claim_key not in ("sub", "type", "iat", "exp", "jti"):
                payload[claim_key] = claim_val

    return jwt.encode(payload, key, algorithm=algo)


def decode_jwt_token(
    token: str,
    secret_key: str | None = None,
    algorithm: str | None = None,
    verify_exp: bool = True,
) -> dict[str, Any]:
    """Decode and cryptographically verify a JWT.

    Args:
        token: Encoded JWT string.
        secret_key: Optional override for verification key (defaults to settings.secret_key).
        algorithm: Optional override for expected algorithm (defaults to settings.algorithm).
        verify_exp: Whether to verify expiration timestamp.

    Returns:
        Decoded token payload as a dictionary.

    Raises:
        TokenExpiredError: If the token has expired.
        InvalidTokenError: If signature, algorithm, format, or claims are invalid.
    """
    key = secret_key or settings.secret_key
    algo = algorithm or settings.algorithm

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[algo],
            options={"verify_exp": verify_exp},
        )
        return payload
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Token has expired.") from exc
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(f"Token is invalid: {exc}") from exc


def verify_jwt_token(
    token: str,
    expected_type: str | None = None,
    secret_key: str | None = None,
    algorithm: str | None = None,
) -> dict[str, Any]:
    """Verify and decode a JWT, validating subject presence and optional expected type.

    Args:
        token: Encoded JWT string.
        expected_type: Expected token type (e.g., 'access' or 'refresh').
        secret_key: Key for signature verification.
        algorithm: Expected algorithm.

    Returns:
        Decoded token payload.

    Raises:
        TokenExpiredError: If the token has expired.
        InvalidTokenError: If subject is missing or token type does not match.
    """
    payload = decode_jwt_token(token, secret_key=secret_key, algorithm=algorithm)

    if not payload.get("sub"):
        raise InvalidTokenError("Token is missing subject claim ('sub').")

    if expected_type is not None:
        token_type = payload.get("type")
        if token_type != expected_type:
            raise InvalidTokenError(
                f"Invalid token type: expected '{expected_type}', got '{token_type}'."
            )

    return payload


__all__ = [
    "DEFAULT_ARGON2_HASH_LEN",
    "DEFAULT_ARGON2_MEMORY_COST",
    "DEFAULT_ARGON2_PARALLELISM",
    "DEFAULT_ARGON2_SALT_LEN",
    "DEFAULT_ARGON2_TIME_COST",
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_PASSWORD_RESET",
    "TOKEN_TYPE_REFRESH",
    "TOKEN_TYPE_VERIFICATION",
    "InvalidTokenError",
    "PasswordHasherService",
    "PasswordStrengthError",
    "SecurityError",
    "TokenError",
    "TokenExpiredError",
    "check_needs_rehash",
    "create_access_token",
    "create_refresh_token",
    "decode_jwt_token",
    "enforce_password_strength",
    "generate_numeric_otp",
    "generate_secure_token",
    "hash_password",
    "hash_token",
    "validate_password_strength",
    "verify_jwt_token",
    "verify_password",
    "verify_token_hash",
]
