"""Email notification service for EduSense using SMTP and test mode recording.

Adheres to EduSense architecture:
- Section 9 & 10: Mock SMTP in tests; NEVER connect to real external SMTP or send real emails.
- Graceful error handling for all SMTP network and authentication failures.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from app.core.config import settings

logger = logging.getLogger("edusense.services.email")


class EmailService:
    """Service responsible for sending transactional emails via SMTP or in-memory recording."""

    def __init__(self, test_mode: bool = False) -> None:
        """Initialize email service with test mode awareness.

        Args:
            test_mode: Explicit flag to record emails in memory without network I/O.
        """
        self.test_mode: bool = test_mode or (settings.app_env == "test")
        self.sent_emails: list[dict[str, Any]] = []

    def send_email(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> bool:
        """Send an email to a recipient or record it in test mode.

        Args:
            to_email: Recipient email address.
            subject: Subject line.
            body_text: Plain-text content body.
            body_html: Optional HTML formatted content body.

        Returns:
            True if sent/recorded successfully, False on error.
        """
        normalized_recipient = to_email.strip()
        if not normalized_recipient or "@" not in normalized_recipient:
            logger.warning("Attempted to send email to invalid address: '%s'", to_email)
            return False

        # 1. In-memory recording when in test environment or test_mode is enabled
        if self.test_mode or settings.app_env == "test":
            record: dict[str, Any] = {
                "to_email": normalized_recipient,
                "subject": subject,
                "body_text": body_text,
                "body_html": body_html,
                "sent_at": datetime.now(UTC),
            }
            self.sent_emails.append(record)
            logger.info(
                "Test mode: Email recorded for %s with subject '%s'",
                normalized_recipient,
                subject,
            )
            return True

        # 2. Live SMTP dispatch with graceful failure handling
        try:
            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
            message["To"] = normalized_recipient

            # Attach plain text part
            part_text = MIMEText(body_text, "plain", "utf-8")
            message.attach(part_text)

            # Attach optional HTML part
            if body_html:
                part_html = MIMEText(body_html, "html", "utf-8")
                message.attach(part_html)

            with smtplib.SMTP(
                host=settings.smtp_host,
                port=settings.smtp_port,
                timeout=settings.smtp_timeout,
            ) as server:
                if settings.smtp_tls:
                    server.starttls()
                if settings.smtp_user and settings.smtp_password:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(message)

            logger.info("Successfully sent email to %s: '%s'", normalized_recipient, subject)
            return True

        except smtplib.SMTPException as exc:
            logger.error(
                "SMTP error occurred while sending email to %s: %s",
                normalized_recipient,
                exc,
            )
            return False
        except Exception:
            logger.exception(
                "Unexpected failure sending email to %s",
                normalized_recipient,
            )
            return False

    def clear_sent_emails(self) -> None:
        """Clear the in-memory recorded emails buffer."""
        self.sent_emails.clear()


# Module-level singleton
_email_service_instance: EmailService | None = None


def get_email_service(test_mode: bool = False) -> EmailService:
    """Dependency provider returning singleton EmailService instance."""
    global _email_service_instance
    if _email_service_instance is None:
        _email_service_instance = EmailService(test_mode=test_mode)
    return _email_service_instance


def set_email_service(service: EmailService | None) -> None:
    """Override or reset the singleton EmailService instance (useful for unit tests)."""
    global _email_service_instance
    _email_service_instance = service


__all__ = [
    "EmailService",
    "get_email_service",
    "set_email_service",
]
