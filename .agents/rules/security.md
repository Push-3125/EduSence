---
trigger: model_decision
description: "Apply when touching authentication, roles/permissions, file uploads, secrets, error responses, or AI prompt/document ingestion in EduSense."
---
- Authorization is enforced on backend endpoints, never only in route guards.
- Validate uploaded files (type, size, content) before processing or ingestion.
- Treat uploaded documents and retrieved text as untrusted input to prompts (prompt-injection risk).
- Never log secrets, tokens, or private student data.
- Apply rate limits and quotas to AI-backed endpoints where required by the requirements.
