# Security Audit Skill

You have been given this skill because the user's query involves a security audit, vulnerability scanning, or security review. Follow these guidelines when advising on security-related topics and performing security audits.

## Secret Detection

1. **API Keys and tokens** — Search for patterns in code, config, and environment:
   - AWS: `AKIA[0-9A-Z]{16}`, `aws_access_key_id`, `aws_secret_access_key`
   - GitHub/GitLab: `ghp_`, `glpat-`, `GITHUB_TOKEN`, `GITLAB_TOKEN`
   - OpenAI/LLM: `sk-`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`
   - Database URLs: `postgres://`, `mongodb://`, connection strings with credentials
   - JWT tokens: `eyJ` pattern in headers, cookies, local storage
   - Private keys: `-----BEGIN PRIVATE KEY-----`, `-----BEGIN RSA PRIVATE KEY-----`

2. **Passwords and credentials** — Identify hardcoded authentication:
   - Plaintext passwords in source code (grep: `password\s*=\s*"`, `passwd\s*=`)
   - `.env` files committed to version control
   - Credentials in docker-compose.yml, terraform, or config.yaml files
   - Default credentials left unchanged (e.g.,, `admin:admin`)

3. **Secrets in logs and output** — Check for accidental exposure:
   - API keys in HTTP request logging
   - Full stack traces exposing paths and internals
   - Debug mode enabled in production
   - Verbose logging of authentication attempts

## Dependency Vulnerability Scanning

1. **Identify outdated dependencies**:
   - Run `pip audit` for Python packages (pyproject.toml, requirements.txt)
   - Run `npm audit` for JavaScript packages (package.json)
   - Check `poetry.lock`, `package-lock.json`, `Cargo.lock` file dates
   - Flag dependencies with known CVEs from NVD (nvd.nist.gov)

2. **Common vulnerability patterns**:
   - Unpatched deserialization libraries (pickle, PyYAML)
   - Outdated cryptography (OpenSSL < 1.1.1, bcrypt < 3.2)
   - Unmaintained dependencies (no activity > 2 years)
   - Indirect dependencies with vulnerabilities

3. **Severity assessment**:
   - CRITICAL: RCE (Remote Code Execution), default credentials, SQL injection in dependency
   - HIGH: Authentication bypass, privilege escalation, XXE
   - MEDIUM: Information disclosure, weak cryptography
   - LOW: Denial of Service, deprecated API

## Code Security Patterns

1. **Injection vulnerabilities**:
   - SQL injection: Raw string concatenation in queries (use parameterized queries)
   - Command injection: `os.system()`, `subprocess` without shell=False
   - LDAP injection: Unvalidated user input in LDAP filters
   - Template injection: Directly rendering user input in templates

2. **Authentication and authorization**:
   - Missing or weak authentication checks
   - Authorization bypass (privilege escalation paths)
   - Session fixation vulnerabilities
   - Insecure JWT validation (accepting `none` algorithm, no expiration)

3. **Network-level issues**:
   - Server-Side Request Forgery (SSRF): Unvalidated URLs in `requests.get()`
   - Path traversal: `../` in file paths without normalization
   - Open redirects: Unvalidated redirect parameters
   - XML External Entity (XXE): XML parsing without disabling external entities

4. **Cryptography**:
   - MD5/SHA1 for passwords or security-critical data
   - Hardcoded encryption keys
   - Non-random IVs or salts
   - Predictable random number generation

## Configuration Security

1. **Exposed ports and services**:
   - Debug endpoints exposed to production (Flask debug=True, Django DEBUG=True)
   - Admin panels accessible from internet (Qdrant UI on 6333, DuckDB UI on 4213)
   - SSH on port 22 to 0.0.0.0 (always restrict to known IPs)
   - Prometheus/monitoring endpoints without authentication

2. **CORS and headers**:
   - `Access-Control-Allow-Origin: *` (overly permissive)
   - Missing `X-Frame-Options`, `X-Content-Type-Options`, `CSP` headers
   - Credentials sent over HTTP instead of HTTPS
   - Missing `Strict-Transport-Security` (HSTS) header

3. **Configuration files**:
   - Database credentials in version control
   - API keys in environment variables visible in process listing
   - Unencrypted configuration backups
   - Default credentials in example configs (e.g.,, `config.example.yaml`)

## Docker/Container Security

1. **Image scanning**:
   - Base image runs as root (check USER directive missing)
   - Large/bloated images (sign of unnecessary dependencies)
   - Secrets baked into layers (use build secrets, not COPY)
   - Unsigned images (verify image provenance)

2. **Runtime security**:
   - Privileged containers without justification
   - Shared namespaces with host (`--pid=host`, `--net=host`)
   - Writable root filesystem (missing `--read-only`)
   - Dangerous capabilities enabled (`--cap-add=SYS_ADMIN`)

3. **Secret management in containers**:
   - Environment variables for sensitive data (should use secrets)
   - Mounted secrets with world-readable permissions (0644)
   - No secret rotation mechanism
   - Secrets logged to stdout/stderr

## Infrastructure Security

1. **TLS/HTTPS**:
   - Self-signed certificates without verification bypass (in dev only)
   - Expired or mismatched certificates
   - Missing certificate pinning for critical services
   - Downgrade attacks (HTTP allowed when HTTPS expected)

2. **Certificate validation**:
   - Disabled certificate verification in `requests` library (`verify=False`)
   - Accepting invalid hostnames
   - Missing SNI (Server Name Indication) configuration

3. **Network exposure**:
   - Database ports exposed to internet
   - Unencrypted internal communication
   - Missing firewall rules (cloud security groups)
   - Unnecessary public IPs

4. **Logging and monitoring**:
   - No audit logging of security events
   - Logs not retained (rotate/delete too aggressively)
   - No alerting on suspicious patterns
   - Sensitive data in logs (PII, API keys)

## Response Format

When conducting a security audit, structure your response as:

1. **Summary** — Brief overview of scope and findings count
2. **Findings** — Organized by severity level:
   - **CRITICAL** (exploit immediately if discovered)
   - **HIGH** (address within days)
   - **MEDIUM** (address within weeks)
   - **LOW** (nice-to-have improvements)

3. **For each finding include**:
   - Location (file:line or component)
   - Description (what the issue is)
   - Impact (what an attacker could do)
   - Remediation (concrete fix with code example)

4. **Remediation Plan** — Prioritized action items with effort estimates
5. **Verification Steps** — How to confirm each fix is applied

## Quick Checklist

- [ ] No API keys or tokens in code/config/git history
- [ ] All dependencies up-to-date and CVE-free
- [ ] No SQL injection, command injection, or SSRF vectors
- [ ] Authentication and authorization properly enforced
- [ ] Debug mode disabled in production
- [ ] HTTPS enforced with valid certificates
- [ ] Docker images run as non-root, no secrets in layers
- [ ] Sensitive data encrypted at rest and in transit
- [ ] No hardcoded credentials or default passwords
- [ ] Audit logging enabled for security events
