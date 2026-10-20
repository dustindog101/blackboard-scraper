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

## Explicit answer saves and starts

Observed save route: `PATCH /learn/api/v1/courses/{courseId}/gradebook/attempts/{attemptId}/assessment/answers/{questionAttemptId}`
with only `questionType` and `givenAnswer`. No submit route is allowed.
Shorthand maps bb's provider-order letters to stable choice IDs before using this pipeline.

Creation route (observed live from the Ultra UI, 201 Created; full body in docs/specs/save-draft-answers-protocol.md):
`POST /learn/api/v1/courses/{courseId}/gradebook/columns/{columnId}/attempts`.
Only explicit `--new-attempt` permits its fixed native-test IN_PROGRESS body.
The HTTP creation adapter allows no other body/route, redirects or retries. Browser
fallback uses shared candidate order and clicks the exact start button behind the
same route/body allowlist. GET reconciles the complete attempt inventory and ID.
See [protocol](../../../../docs/specs/save-draft-answers-protocol.md) and
[ADR-0007](../../../../docs/adr/0007-explicit-untimed-attempt-creation.md).

Verified answer types: multiple answer (select all); single choice (multiple choice
with one answer, encoded by Ultra as `multipleanswer` with `answerSelectionLimit: 1`);
and true/false (`eitherOr` / `true_false`). Essay, short answer, fill-in-the-blank,
matching, ordering, calculated, file response, and anything else are unsupported:
**answer in Blackboard**. Type support does not establish live acceptance of every clear case.
