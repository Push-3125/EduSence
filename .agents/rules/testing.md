---
trigger: glob
globs: "**/tests/**, **/test_*.py, **/*.test.*, **/*.spec.*"
description: "EduSense test design and verification standards"
---
- Follow the testing framework already configured in the repository.
- Cover success, validation, boundary, failure, and permission cases.
- Mock external AI and email services when isolation is required.
- Test authorization at the API boundary.
- Avoid tests that depend on real credentials or paid API calls.
- Report executed commands, failures, and untested areas accurately.
