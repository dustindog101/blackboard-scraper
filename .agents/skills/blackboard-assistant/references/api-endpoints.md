# REST inspection notes

Use these only when diagnosing or extending the scraper; routine questions should
use the CLI. Requests use the shared cookie filter and REST client. Do not print
identity responses or auth data.

- `GET /learn/api/public/v1/users/me`: verify authentication and intended identity.
- `GET /learn/api/public/v1/terms`: term windows.
- `GET /learn/api/public/v1/users/{userId}/courses?expand=course`: enrolled memberships.
- `GET /learn/api/public/v2/courses/{courseId}/gradebook/columns`: visible gradable items.
- `GET /learn/api/public/v2/courses/{courseId}/gradebook/users/{userId}`: student records
  in one request per course; do not assume columns include scores.
- `GET /learn/api/public/v1/courses/{courseId}/gradebook/schemas`: visible grade schemas.

Endpoint paths and payloads may vary. Follow paging and distinguish expired auth,
student visibility restrictions, missing data and network failure. Use the shared
Gradable Item model rather than inferring completion from a raw discussion status.
See [coding standards](../../../../docs/agents/coding-standards.md) and implementation
in `core/gradebook.py` and `core/rest.py` for the maintained behavior.
