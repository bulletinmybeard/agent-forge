# OpenAPI Specification Writing Skill

You have been given this skill because the user's query involves writing, reviewing, or documenting OpenAPI specifications (REST APIs). Follow these guidelines when advising on
API documentation and schema design.

## OpenAPI 3.1 Structure

1. **Top-level object** — Always include: `openapi: '3.1.0'`, `info`, `servers`, `paths`, and `components`. Use YAML format for readability.
2. **Info object** — Include: `title`, `version`, `description`, `contact`, `license`. The `version` field is critical for versioning; update it when API changes.
3. **Servers array** — Define multiple servers (production, staging, development). Use `{env}` variables for environment substitution.
4. **Tags** — Group endpoints by resource (users, products, orders). Use consistently across all path definitions.

## Path Design

1. **RESTful resource naming** — Use plural nouns for collections: `/products`, `/users`, `/orders`. Not `/getProducts` or `/product_list`.
2. **Nested resources** — Model hierarchies: `/orders/{orderId}/items/{itemId}`. Parent ID comes first.
3. **HTTP methods** — Follow semantics strictly:
   - `GET` — Fetch resource(s), idempotent, no side effects
   - `POST` — Create resource, may have side effects
   - `PATCH` — Partial update (only specified fields)
   - `PUT` — Full replace (all fields required)
   - `DELETE` — Remove resource
4. **Path parameters** — Use `{camelCase}` for ID placeholders. Document in `parameters` array with `required: true` and `in: path`.
5. **Query parameters** — For filtering, sorting, pagination. Mark as `required: false`.

## Schema Design

1. **$ref reuse** — Define reusable schemas in `components.schemas`. Reference with `$ref: '#/components/schemas/Product'`. Never duplicate schema definitions.
2. **Composition strategies**:
   - `allOf` — Combine multiple schemas (e.g.,, inheritance)
   - `oneOf` — Exactly one of several schemas (use `discriminator` to select)
   - `anyOf` — One or more schemas
3. **Discriminators** — For `oneOf`, specify which field identifies the schema variant:
   ```yaml
   oneOf:
     - $ref: '#/components/schemas/ErrorBadRequest'
     - $ref: '#/components/schemas/ErrorNotFound'
   discriminator:
     propertyName: code
   ```
4. **Required fields** — Always list explicitly in `required: [field1, field2]` array.
   Default is all fields optional.
5. **Constraints** — Use `minLength`, `maxLength`, `minimum`, `maximum`, `pattern`,
   `enum`. Add examples for clarity.

## Request/Response Examples

1. **Every endpoint needs examples** — Include a realistic request and response body
   in the operation object:
   ```yaml
   requestBody:
     required: true
     content:
       application/json:
         example:
           name: "Product Name"
           price: 99.99
   ```
2. **Response examples** — For all success codes (200, 201, 202):
   ```yaml
   responses:
     '200':
       description: Success
       content:
         application/json:
           example:
             id: "prod_123"
             name: "Product Name"
   ```
3. **Status code specificity** — Separate 200 (OK), 201 (Created), 204 (No Content).
   Use the most semantically correct code.

## Error Responses

1. **RFC 7807 Problem Details** — Always use standard error envelope:
   ```yaml
   type: object
   properties:
     type:
       type: string
       description: Error type URI (e.g.,, "https://api.example.com/errors/bad-request")
     title:
       type: string
       description: Human-readable error summary
     status:
       type: integer
       description: HTTP status code
     detail:
       type: string
       description: Detailed explanation of the specific error
     instance:
       type: string
       description: URI reference to specific occurrence
   ```
2. **Consistent error envelope** — All error responses use the same structure across all endpoints. Define once in `components.schemas.Error` and reference everywhere.
3. **Document common errors** — Every endpoint should document: 400 (Bad Request), 401 (Unauthorized), 403 (Forbidden), 404 (Not Found), 500 (Internal Server Error).
4. **Error codes** — Add a `code` field (e.g.,, "INVALID_EMAIL") for programmatic handling.

## Authentication & Authorization

1. **Security schemes** — Define in `components.securitySchemes`:
   - **OAuth2**: `type: oauth2`, flow type (authorizationCode, implicit, clientCredentials)
   - **API Key**: `type: apiKey`, `in: header` or `query`
   - **Bearer Token**: `type: http`, `scheme: bearer`, `bearerFormat: JWT`
2. **Global security** — Set default auth at root level: `security: [{bearerAuth: []}]`
3. **Per-operation overrides** — Some endpoints may allow unauthenticated access. Override security at operation level: `security: []`
4. **Scopes** — For OAuth2, document scopes: `read:products`, `write:orders`. Link scopes to resource access permissions.

## Pagination

1. **Cursor-based pagination** — Best for large datasets. Response includes:
   ```yaml
   items: [...]
   pagination:
     cursor: "next_cursor_token"
     hasMore: true
   ```
2. **Offset/limit pagination** — Simple but less efficient for large pages:
   ```yaml
   items: [...]
   pagination:
     offset: 0
     limit: 20
     total: 150
   ```
3. **Link header style** — Standard HTTP approach for REST clients:
   ```
   Link: <https://api.example.com/items?offset=20>; rel="next"
   ```
   Include `rel="first"`, `rel="last"`, `rel="prev"` as applicable.
4. **Default limits** — Always specify in documentation. Recommend: `limit: 20` with
   max of 100. Reject larger limits.

## Versioning Strategy

1. **URL path versioning** — Most explicit: `/v1/products`, `/v2/products`
   - Pro: Clear, cached well, easy to deprecate
   - Con: More verbose
2. **Header versioning** — `Accept: application/vnd.api+json;version=2`
   - Pro: Cleaner URLs
   - Con: Less visible, harder to cache
3. **Query parameter** — Least recommended but acceptable: `?version=2`
4. **Deprecation policy** — In OpenAPI, mark deprecated endpoints:
   ```yaml
   deprecated: true
   x-deprecation-message: "Use /v2/products instead. Sunset: 2025-12-31"
   ```
   Support at least 2 versions (current + previous).

## Output Format

When creating or reviewing OpenAPI specs, structure your response as:
1. **Summary** — What API this spec documents in 1-2 sentences
2. **Issues** — Schema validation errors, missing documentation, design anti-patterns
3. **Recommendations** — Concrete improvements with YAML snippets
4. **Complete spec** — If significant changes needed, provide the full validated
   OpenAPI 3.1 YAML document
5. **Validation note** — Mention which validation tool was used (swagger-cli, ibm-openapi-validator)
