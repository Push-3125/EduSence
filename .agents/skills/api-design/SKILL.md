---
name: api-design
description: Designs FastAPI REST endpoints for EduSense: resources, request/response schemas, status codes, error format, and per-endpoint authorization. Use when adding or changing API endpoints.
---

# API Design (EduSense)

## Workflow
1. Check existing routers, schemas, and error conventions; follow them. If none exist, propose conventions and ask.
2. Model endpoints as resources with consistent naming and HTTP methods.
3. Define Pydantic request and response schemas; never return internal fields (for example password hashes).
4. Specify status codes and one consistent error response shape.
5. State the authentication requirement and the allowed roles for every endpoint.
6. Define pagination, filtering, and sorting for list endpoints.
7. For slow or AI-backed operations, define timeout, rate-limit, and failure behavior.
8. List the tests needed per endpoint, including authorization and validation failures.

## Constraints
- Do not break existing API contracts silently; call out any breaking change.
- Authorization is enforced on the server, never only in the frontend.

## Output
- Endpoint table (method, path, auth/roles, request, response, errors)
- Schema definitions and test list
