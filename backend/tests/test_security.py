"""Comprehensive unit tests for security primitives, Argon2id hashing, and JWT utilities."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    InvalidTokenError,
    PasswordHasherService,
    PasswordStrengthError,
    TokenExpiredError,
    check_needs_rehash,
    create_access_token,
    create_refresh_token,
    decode_jwt_token,
    enforce_password_strength,
    generate_numeric_otp,
    generate_secure_token,
    hash_password,
    hash_token,
    validate_password_strength,
    verify_jwt_token,
    verify_password,
    verify_token_hash,
)

# A test secret key satisfying the minimum length of 32 bytes (256 bits) for HMAC-SHA256
TEST_SECRET_KEY = "test-secret-key-for-unit-testing-at-least-32-chars-long"


# ---------------------------------------------------------------------------
# Argon2id Password Hashing & Verification Tests
# ---------------------------------------------------------------------------


def test_hash_password_produces_argon2id_format() -> None:
    """Verify that hash_password generates an Argon2id formatted hash with version 19."""
    password = "StudentSecurePassword123!"
    hashed = hash_password(password)

    assert isinstance(hashed, str)
    assert hashed.startswith("$argon2id$v=19$")


def test_verify_password_success() -> None:
    """Verify that a correct password successfully verifies against its Argon2id hash."""
    password = "CorrectHorseBatteryStaple99#"
    hashed = hash_password(password)

    assert verify_password(password, hashed) is True


def test_verify_password_mismatch() -> None:
    """Verify that an incorrect password fails verification and returns False."""
    password = "CorrectPassword123!"
    hashed = hash_password(password)

    assert verify_password("WrongPassword123!", hashed) is False


def test_verify_password_empty_inputs() -> None:
    """Verify that empty candidate password or empty hash returns False safely."""
    assert verify_password("", "$argon2id$v=19$dummyhash") is False
    assert verify_password("Password123!", "") is False
    assert verify_password("", "") is False


def test_verify_password_corrupted_hash() -> None:
    """Verify that malformed or corrupted hashes do not raise exceptions but return False."""
    corrupted_hashes = [
        "not_an_argon2_hash",
        "$argon2id$v=19$invalid_encoding$bad_payload",
        "1234567890",
    ]
    for bad_hash in corrupted_hashes:
        assert verify_password("Password123!", bad_hash) is False


def test_hash_password_empty_raises_value_error() -> None:
    """Verify that attempting to hash an empty password raises ValueError."""
    with pytest.raises(ValueError, match="Password cannot be empty"):
        hash_password("")


def test_hash_password_exceeds_max_length() -> None:
    """Verify that passwords exceeding 1024 characters raise ValueError to prevent DoS."""
    oversized_password = "A" * 1025
    with pytest.raises(ValueError, match="exceeds maximum allowed length"):
        hash_password(oversized_password)


def test_salt_uniqueness() -> None:
    """Verify that two hashes of the same password produce distinct results due to salt."""
    password = "SamePasswordEveryTime456!"
    hash1 = hash_password(password)
    hash2 = hash_password(password)

    assert hash1 != hash2
    assert verify_password(password, hash1) is True
    assert verify_password(password, hash2) is True


def test_check_needs_rehash_default() -> None:
    """Verify that freshly hashed passwords do not need rehashing under current parameters."""
    hashed = hash_password("FreshPassword123!")
    assert check_needs_rehash(hashed) is False


def test_check_needs_rehash_weaker_parameters() -> None:
    """Verify that a hash generated with weaker parameters triggers check_needs_rehash."""
    weaker_hasher = PasswordHasherService(time_cost=1, memory_cost=16384)
    weak_hash = weaker_hasher.hash("OldSystemPassword123!")

    # Checking with the default hasher should identify that re-hashing is needed
    assert check_needs_rehash(weak_hash) is True


def test_check_needs_rehash_invalid_hash() -> None:
    """Verify that corrupted hash string returns True for rehash check."""
    assert check_needs_rehash("invalid-hash-string") is True


# ---------------------------------------------------------------------------
# Password Strength & Complexity Validation Tests
# ---------------------------------------------------------------------------


def test_validate_password_strength_valid() -> None:
    """Verify that strong passwords satisfy all complexity rules."""
    valid_passwords = [
        "Str0ngP@ssword",
        "EduSense#2026!",
        "V@lid!Passw0rd",
        "Super*Secret123",
    ]
    for pwd in valid_passwords:
        is_valid, errors = validate_password_strength(pwd)
        assert is_valid is True, f"Failed for {pwd}: {errors}"
        assert errors == []


def test_validate_password_strength_too_short() -> None:
    """Verify rejection when password is shorter than min_length."""
    is_valid, errors = validate_password_strength("Ab1!xyz", min_length=8)
    assert is_valid is False
    assert any("at least 8 characters" in err for err in errors)


def test_validate_password_strength_too_long() -> None:
    """Verify rejection when password exceeds max_length."""
    too_long = "A1!" + "a" * 126
    is_valid, errors = validate_password_strength(too_long, max_length=128)
    assert is_valid is False
    assert any("must not exceed 128 characters" in err for err in errors)


def test_validate_password_strength_missing_uppercase() -> None:
    """Verify rejection when password lacks uppercase letters."""
    is_valid, errors = validate_password_strength("lowercase123!@#")
    assert is_valid is False
    assert any("uppercase letter" in err for err in errors)


def test_validate_password_strength_missing_lowercase() -> None:
    """Verify rejection when password lacks lowercase letters."""
    is_valid, errors = validate_password_strength("UPPERCASE123!@#")
    assert is_valid is False
    assert any("lowercase letter" in err for err in errors)


def test_validate_password_strength_missing_digit() -> None:
    """Verify rejection when password lacks numeric digits."""
    is_valid, errors = validate_password_strength("NoDigitsPassword!@#")
    assert is_valid is False
    assert any("numeric digit" in err for err in errors)


def test_validate_password_strength_missing_special() -> None:
    """Verify rejection when password lacks special characters."""
    is_valid, errors = validate_password_strength("NoSpecialChar12345")
    assert is_valid is False
    assert any("special character" in err for err in errors)


def test_enforce_password_strength_valid() -> None:
    """Verify that enforce_password_strength completes silently for valid passwords."""
    enforce_password_strength("StrongPassw0rd!")


def test_enforce_password_strength_raises() -> None:
    """Verify that enforce_password_strength raises PasswordStrengthError with violations."""
    with pytest.raises(PasswordStrengthError) as exc_info:
        enforce_password_strength("weak")

    assert len(exc_info.value.errors) > 0
    assert "at least 8 characters" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Cryptographic Token Generation & Hashing Tests
# ---------------------------------------------------------------------------


def test_generate_secure_token() -> None:
    """Verify generation of cryptographically secure URL-safe tokens."""
    token1 = generate_secure_token(32)
    token2 = generate_secure_token(32)

    assert isinstance(token1, str)
    assert len(token1) >= 32
    assert token1 != token2


def test_generate_numeric_otp_valid() -> None:
    """Verify numeric OTP generation matches requested digit count."""
    otp = generate_numeric_otp(6)

    assert len(otp) == 6
    assert otp.isdigit()

    otp_8 = generate_numeric_otp(8)
    assert len(otp_8) == 8
    assert otp_8.isdigit()


def test_generate_numeric_otp_invalid_length() -> None:
    """Verify that zero or negative OTP length raises ValueError."""
    with pytest.raises(ValueError, match="positive integer"):
        generate_numeric_otp(0)

    with pytest.raises(ValueError, match="positive integer"):
        generate_numeric_otp(-3)


def test_hash_token_deterministic() -> None:
    """Verify that hash_token produces a consistent 64-char lowercase SHA-256 hex string."""
    raw_token = "verification-token-example-12345"
    hash1 = hash_token(raw_token)
    hash2 = hash_token(raw_token)

    assert len(hash1) == 64
    assert hash1.islower()
    assert hash1 == hash2


def test_verify_token_hash_success() -> None:
    """Verify constant-time comparison succeeds for matching token and hash."""
    raw_token = "secure-random-token-xyz"
    hashed = hash_token(raw_token)

    assert verify_token_hash(raw_token, hashed) is True


def test_verify_token_hash_mismatch() -> None:
    """Verify constant-time comparison fails for mismatched token or corrupted hash."""
    raw_token = "secure-random-token-xyz"
    hashed = hash_token(raw_token)

    assert verify_token_hash("wrong-token", hashed) is False
    assert verify_token_hash(raw_token, "wrong-hash") is False


# ---------------------------------------------------------------------------
# JWT Management Primitives Tests
# ---------------------------------------------------------------------------


def test_create_access_token_structure() -> None:
    """Verify standard claims present in created access token."""
    user_id = str(uuid.uuid4())
    token = create_access_token(
        subject=user_id,
        secret_key=TEST_SECRET_KEY,
    )

    payload = decode_jwt_token(token, secret_key=TEST_SECRET_KEY)
    assert payload["sub"] == user_id
    assert payload["type"] == TOKEN_TYPE_ACCESS
    assert "iat" in payload
    assert "exp" in payload
    assert "jti" in payload
    assert payload["exp"] > payload["iat"]


def test_create_refresh_token_structure() -> None:
    """Verify standard claims present in created refresh token."""
    user_id = str(uuid.uuid4())
    token = create_refresh_token(
        subject=user_id,
        secret_key=TEST_SECRET_KEY,
    )

    payload = decode_jwt_token(token, secret_key=TEST_SECRET_KEY)
    assert payload["sub"] == user_id
    assert payload["type"] == TOKEN_TYPE_REFRESH
    assert "iat" in payload
    assert "exp" in payload
    assert "jti" in payload
    assert payload["exp"] > payload["iat"]


def test_create_token_with_extra_claims() -> None:
    """Verify that extra claims (roles, email) are merged safely into the token payload."""
    user_id = str(uuid.uuid4())
    extra = {"role": "student", "email": "student@university.edu"}

    token = create_access_token(
        subject=user_id,
        extra_claims=extra,
        secret_key=TEST_SECRET_KEY,
    )

    payload = decode_jwt_token(token, secret_key=TEST_SECRET_KEY)
    assert payload["sub"] == user_id
    assert payload["role"] == "student"
    assert payload["email"] == "student@university.edu"


def test_create_token_custom_expires_delta() -> None:
    """Verify that custom expires_delta accurately sets expiration timestamp."""
    user_id = "test-user-custom-expiry"
    delta = timedelta(minutes=5)
    token = create_access_token(
        subject=user_id,
        expires_delta=delta,
        secret_key=TEST_SECRET_KEY,
    )

    payload = decode_jwt_token(token, secret_key=TEST_SECRET_KEY)
    assert payload["exp"] - payload["iat"] == 300


def test_decode_jwt_token_expired() -> None:
    """Verify that an expired token raises TokenExpiredError."""
    user_id = "expired-user"
    token = create_access_token(
        subject=user_id,
        expires_delta=timedelta(seconds=-10),
        secret_key=TEST_SECRET_KEY,
    )

    with pytest.raises(TokenExpiredError, match="Token has expired"):
        decode_jwt_token(token, secret_key=TEST_SECRET_KEY)


def test_decode_jwt_token_invalid_signature() -> None:
    """Verify that a token signed with an untrusted key raises InvalidTokenError."""
    user_id = "user-wrong-key"
    token = create_access_token(
        subject=user_id,
        secret_key=TEST_SECRET_KEY,
    )

    untrusted_key = "untrusted-secret-key-at-least-32-chars-long"
    with pytest.raises(InvalidTokenError, match="Token is invalid"):
        decode_jwt_token(token, secret_key=untrusted_key)


def test_decode_jwt_token_malformed() -> None:
    """Verify that malformed token strings raise InvalidTokenError."""
    with pytest.raises(InvalidTokenError):
        decode_jwt_token("this.is.not.a.valid.jwt", secret_key=TEST_SECRET_KEY)


def test_verify_jwt_token_success() -> None:
    """Verify verify_jwt_token validates subject and expected token type."""
    user_id = "valid-subject-123"
    token = create_access_token(
        subject=user_id,
        secret_key=TEST_SECRET_KEY,
    )

    payload = verify_jwt_token(
        token=token,
        expected_type=TOKEN_TYPE_ACCESS,
        secret_key=TEST_SECRET_KEY,
    )
    assert payload["sub"] == user_id
    assert payload["type"] == TOKEN_TYPE_ACCESS


def test_verify_jwt_token_type_mismatch() -> None:
    """Verify that a token type mismatch raises InvalidTokenError."""
    user_id = "valid-subject-123"
    token = create_access_token(
        subject=user_id,
        secret_key=TEST_SECRET_KEY,
    )

    with pytest.raises(InvalidTokenError, match="Invalid token type: expected 'refresh'"):
        verify_jwt_token(
            token=token,
            expected_type=TOKEN_TYPE_REFRESH,
            secret_key=TEST_SECRET_KEY,
        )


def test_verify_jwt_token_missing_subject() -> None:
    """Verify that token missing 'sub' claim raises InvalidTokenError."""
    # Construct token deliberately omitting 'sub'
    raw_payload = {
        "type": TOKEN_TYPE_ACCESS,
        "exp": int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
    }
    bad_token = jwt.encode(raw_payload, TEST_SECRET_KEY, algorithm="HS256")

    with pytest.raises(InvalidTokenError, match="missing subject claim"):
        verify_jwt_token(token=bad_token, secret_key=TEST_SECRET_KEY)
