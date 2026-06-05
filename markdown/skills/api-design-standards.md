# API Design Standards Skill

You have been given this skill because the user's query involves designing, reviewing, or implementing REST APIs. Follow these conventions for consistent, production-quality API design.

## URL Structure

- **Plural nouns** for collections: `/users`, `/orders`, `/products`
- **Singular sub-resources** via nesting: `/users/{id}/profile`
- **No verbs in URLs** — use HTTP methods instead
  - `POST /orders` (not `POST /createOrder`)
  - `DELETE /users/{id}` (not `POST /deleteUser`)
- **Kebab-case** for multi-word paths: `/order-items`, `/user-preferences`
- **Version in path**: `/api/v1/users` (not headers or query params)

## HTTP Methods

| Method   | Usage                         | Idempotent | Response Code     |
|----------|-------------------------------|------------|-------------------|
| GET      | Retrieve resource(s)          | Yes        | 200               |
| POST     | Create new resource           | No         | 201 + Location    |
| PUT      | Full replacement              | Yes        | 200 or 204        |
| PATCH    | Partial update                | No*        | 200               |
| DELETE   | Remove resource               | Yes        | 204               |

## Error Responses (RFC 7807)

Always return errors in a consistent format:

```json
{
  "type": "https://api.example.com/errors/validation",
  "title": "Validation Error",
  "status": 422,
  "detail": "Field 'email' must be a valid email address",
  "instance": "/users/123",
  "errors": [
    { "field": "email", "message": "Invalid email format" }
  ]
}
```

## Pagination

Use cursor-based pagination for large datasets:

```
GET /users?cursor=eyJpZCI6MTIzfQ&limit=25

Response:
{
  "data": [...],
  "pagination": {
    "next_cursor": "eyJpZCI6MTQ4fQ",
    "has_more": true,
    "total_count": 1423  // optional, can be expensive
  }
}
```

Offset-based (`?page=3&per_page=25`) is acceptable for admin/internal APIs
where total count is needed.

## Filtering & Sorting

- **Filtering**: `GET /users?status=active&role=admin`
- **Sorting**: `GET /users?sort=created_at:desc,name:asc`
- **Field selection**: `GET /users?fields=id,name,email`
- **Search**: `GET /users?q=john` (full-text search across relevant fields)

## Request/Response Conventions

- Use **camelCase** for JSON fields (consistent with JavaScript consumers)
- Always include `id` in response objects
- Use **ISO 8601** for dates: `"2025-03-15T10:30:00Z"`
- Wrap collections: `{ "data": [...], "meta": {...} }`
- Include `created_at` and `updated_at` timestamps on all mutable resources

## Authentication & Security

- Use **Bearer tokens** in the `Authorization` header
- Return `401 Unauthorized` for missing/invalid credentials
- Return `403 Forbidden` for valid credentials but insufficient permissions
- Rate limit endpoints and return `429 Too Many Requests` with `Retry-After` header
- Never expose internal IDs (database auto-increments) — use UUIDs or slugs

## Checklist for New Endpoints

- [ ] URL follows naming conventions (plural, kebab-case, no verbs)
- [ ] Correct HTTP method and status codes
- [ ] Input validation with clear error messages
- [ ] Authentication and authorisation checks
- [ ] Pagination for list endpoints
- [ ] Rate limiting configured
- [ ] OpenAPI/Swagger spec updated
- [ ] Integration tests cover success + error paths

## Response Format

When designing or reviewing an API, structure your response as:
1. **Endpoint spec** — Method, URL, request/response schemas
2. **Issues** — Convention violations or design concerns
3. **Recommendations** — Improvements with examples
