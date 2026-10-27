---
name: frontend-design
description: Designs and implements Vue 3 UI for EduSense following the approved layout and design decisions, including all UI states and accessibility. Use when building or changing screens and components.
---

# Frontend Design (EduSense)

## Workflow
1. Look in docs/ for approved mockups, layout, and design decisions; follow them. If none exist, propose a minimal set of design tokens (colors, type scale, spacing) and ask for approval before building many screens.
2. Inspect existing components and reuse them; create a new component only when none fits.
3. Use Vue 3 Composition API; keep presentation components free of business logic and put API calls in a dedicated layer.
4. Implement every state: loading, empty, error, success, and disabled.
5. Accessibility: semantic HTML, labels for inputs, visible focus, keyboard navigation, sufficient contrast.
6. Responsive behavior: check narrow and wide layouts; avoid horizontal overflow.
7. If a browser tool is available, check the result visually and report what was actually seen.

## Constraints
- Do not redesign layout or navigation that was approved.
- Avoid decorative gradients, extra cards, and visual clutter.
- Do not claim visual checks that were not performed.

## Output
- Components changed or added, states covered, accessibility notes, what was verified
