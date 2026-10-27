---
trigger: model_decision
description: "Apply when working on RAG retrieval, prompts, LLM calls, citations, or essay/subjective answer grading in EduSense."
---
- Keep retrieval, prompt construction, model calls, and output validation in separate units.
- Citations must come only from retrieved chunks; never fabricate sources.
- Validate model output against a schema; handle timeouts, rate limits, and invalid output.
- Control token usage and avoid unnecessary model calls.
- Essay grading is a core feature: grade against the rubric defined in the requirements.
  Do not invent rubric criteria or score scales; record open questions instead.
- Mock model calls in automated tests; never call paid APIs from tests.
