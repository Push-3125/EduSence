---
trigger: glob
globs: "frontend/src/**/*.vue, frontend/src/**/*.ts, frontend/src/**/*.js"
description: "EduSense frontend and UI implementation constraints"
---
- Prioritize writing all new code in TypeScript (.ts, <script setup lang="ts">) with strict typing; avoid `any`.
- Incrementally migrate existing JavaScript files when touched or appropriate, accompanied by tests; do not perform bulk/mass conversions that risk breaking running features.
- Use Vue.js 3 and follow existing project conventions.
- Reuse existing components, tokens, and design-system decisions.
- Preserve the approved EduSense layout and navigation structure.
- Avoid unnecessary gradients, decorative cards, and visual clutter.
- Handle loading, empty, error, and success states.
- Support keyboard navigation and accessible labels.
- Do not introduce unrelated UI redesigns when fixing a feature.
