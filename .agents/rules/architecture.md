---
trigger: model_decision
description: "Apply when adding modules, changing layers, dependencies, or cross-module interactions in EduSense."
---
- Follow approved architecture docs in docs/ if they exist.
- Keep routes thin; business logic in services; persistence in the established data layer.
- Avoid circular dependencies and hidden cross-module coupling.
- Review architectural impact before introducing a new layer or package.
- Report missing or contradictory architecture docs instead of guessing.
