---
trigger: glob
globs: "backend/**/*.py"
description: "FastAPI backend implementation conventions"
---
- Follow existing FastAPI router, service, schema, and persistence patterns.
- Use Pydantic for request and response validation where appropriate.
- Use dependency injection for shared services and authorization.
- Handle expected errors explicitly; never expose stack traces to clients.
- Keep database transactions and external API calls controlled.
- Add pytest coverage for changed business behavior.
- Keep secrets and environment-specific credentials out of source code.
