---
name: requirements-analysis
description: Analyzes requirement documents (proposal, SRS, user stories) to extract features, actors, and acceptance criteria and to find contradictions or gaps. Use before planning or implementing a feature, or when asked to review requirements.
---

# Requirements Analysis

## Workflow
1. Read the relevant documents in docs/ completely before concluding anything.
2. Extract actors/roles, features, business rules, and constraints. Give each requirement a stable ID (for example FR-01) that cites its source file and section.
3. For each feature, list acceptance criteria stated in the documents. Mark missing ones as "MISSING"; do not write your own as if they were approved.
4. Compare documents with each other and with the current code. List every contradiction with both sources quoted by location.
5. List assumptions and open questions separately from confirmed facts.

## Constraints
- Never invent requirements, thresholds, or acceptance criteria.
- Do not resolve a contradiction silently; report it and ask.
- Do not modify files unless asked.

## Output
- Requirement table (ID, description, actor, source, acceptance criteria status)
- Contradictions and gaps
- Assumptions and open questions
- Suggested priority order (marked as suggestion)
