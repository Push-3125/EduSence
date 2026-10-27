---
name: database-design
description: Designs PostgreSQL and pgvector schemas for EduSense: tables, relationships, constraints, and indexes derived from requirements. Use when designing or changing the data model, before writing any migration.
---

# Database Design (EduSense)

## Workflow
1. Read requirements in docs/ and any existing schema or migrations.
2. Identify entities, relationships, and the access patterns (queries) the features need.
3. Define tables with primary keys, foreign keys, NOT NULL, UNIQUE, and CHECK constraints; choose clear snake_case names.
4. Decide indexes from real query patterns, not by habit.
5. For vectors: the embedding dimension and distance metric depend on the chosen embedding model and must be confirmed with the user, not assumed. Plan metadata columns needed for filtering and citation (source document, chunk position).
6. Consider data integrity, deletion behavior (cascade or restrict), and auditability.
7. Present the design as a Mermaid ER diagram plus table definitions, with open questions.

## Constraints
- Design only; do not create migrations or touch the database until the design is approved.
- Do not assume a migration tool; check what the project uses.
- Never plan destructive changes to existing data without an explicit rollback plan and approval.

## Output
- ER diagram, table definitions, index rationale, open questions, risks
