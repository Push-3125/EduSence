---
trigger: model_decision
description: "Apply when changing database schema, migrations, repositories, vector search, or persistence logic in EduSense."
---
- PostgreSQL with pgvector is the approved baseline.
- Check the existing schema and migrations before proposing changes.
- Preserve foreign keys, constraints, and transaction integrity.
- Review indexes against actual query and retrieval patterns.
- Consider vector filtering and retrieval quality when changing RAG.
- Never drop production data or rewrite applied migrations without approval.
- Update database documentation when the schema changes.
