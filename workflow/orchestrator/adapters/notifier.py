"""Notification channel adapter with secret sanitization and webhook support."""

import json
import os
import re
from typing import Optional
import urllib.request


def redact_secrets(text: str) -> str:
    """Sanitize secrets, API keys, passwords, and sensitive tokens from messages and reports."""
    patterns = [
        (r"(AIza[0-9A-Za-z-_]{35})", "[REDACTED_GEMINI_KEY]"),
        (r"(ghp_[0-9A-Za-z]{36})", "[REDACTED_GITHUB_TOKEN]"),
        (r"(github_pat_[0-9A-Za-z_]{82})", "[REDACTED_GITHUB_PAT]"),
        (r"(eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*)", "[REDACTED_JWT]"),
        (r"(?i)(password|secret|key|token|minioadmin|edusense_dev_password)\s*[:=]\s*['\"]?(?!\[REDACTED)([^'\"\s]+)['\"]?", r"\1: [REDACTED]"),
    ]
    sanitized = text
    for pat, repl in patterns:
        sanitized = re.sub(pat, repl, sanitized)
    return sanitized


class Notifier:
    """Multi-channel notification dispatcher for task reports and approval requests."""

    def __init__(self, webhook_url: Optional[str] = None) -> None:
        self.webhook_url = webhook_url or os.getenv("NOTIFICATION_WEBHOOK_URL")

    def notify(self, title: str, message: str, level: str = "INFO") -> None:
        """Send notification via configured channels."""
        safe_title = redact_secrets(title)
        safe_msg = redact_secrets(message)

        # 1. Console Output (always active)
        divider = "=" * 80
        print(f"\n{divider}")
        print(f" [NOTIFICATION - {level.upper()}] {safe_title}")
        print(divider)
        print(safe_msg)
        print(f"{divider}\n")

        # 2. Webhook Notification (Discord / Slack / Generic)
        if self.webhook_url:
            self._send_webhook(safe_title, safe_msg, level)
        else:
            # Inform user how to configure external channel
            pass

    def _send_webhook(self, title: str, message: str, level: str) -> None:
        """Send JSON payload to configured webhook."""
        if not self.webhook_url:
            return

        payload = {
            "title": f"[{level.upper()}] {title}",
            "text": message,
            "content": f"**[{level.upper()}] {title}**\n\n{message[:1800]}",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "EduSense-Orchestrator"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                _ = resp.read()
        except Exception as e:
            print(f"[WARN] Failed to send webhook notification: {e}")

    @staticmethod
    def get_setup_instructions() -> str:
        """Explain exact setup required for external notification channels."""
        return (
            "Notification Configuration:\n"
            "  - Current active channel: Console / Standard Output\n"
            "  - To enable Discord / Slack Webhook notifications:\n"
            "      Add to .env: NOTIFICATION_WEBHOOK_URL=https://discord.com/api/webhooks/... or Slack webhook URL\n"
            "  - To enable Email alerts (SMTP):\n"
            "      Add to .env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, NOTIFICATION_EMAIL_RECIPIENT\n"
        )
