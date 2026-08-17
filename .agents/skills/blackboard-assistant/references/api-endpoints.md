# Blackboard Ultra REST API Endpoints Reference

All endpoints query `https://blackboard.umbc.edu/learn/api/public/v1` using session cookies stored in `.session/cookies.json`.

### 1. User Profile & Health Probe
- **Endpoint**: `GET /learn/api/public/v1/users/me`
- **Purpose**: Ultra-fast session verification (<120ms). Returns student ID (`BH69617`), user ID (`_148386_1`), and name (`Amanuel Hailie`).
- **Response Shape**:
```json
{
  "id": "_148386_1",
  "userName": "BH69617",
  "name": {
    "given": "Amanuel",
    "family": "Hailie"
  },
  "studentId": "BH69617"
}
```

### 2. Academic Terms
- **Endpoint**: `GET /learn/api/public/v1/terms`
- **Purpose**: Retrieves all semester definitions (`Fall 2026`, `Spring 2026`, `Fall 2025`, etc.) with start and end dates.

### 3. User Course Memberships
- **Endpoint**: `GET /learn/api/public/v1/users/{userId}/courses?expand=course`
- **Purpose**: Retrieves all lifetime course enrollments and joins term IDs to isolate the active semester.

### 4. Direct Course Contents
- **Endpoint**: `GET /learn/api/public/v1/courses/{courseId}/contents`
- **Purpose**: Queries course folders, documents, and syllabus links without launching a browser.
