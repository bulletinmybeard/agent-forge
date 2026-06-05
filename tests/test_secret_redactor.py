"""Redaction must mask secret VALUES in place, never eat the key name.

Regression: the GenericKeyAssignment / AWSSecretAccessKey regex patterns matched
the whole `key: "value"` assignment, so `text.replace(secret_value, placeholder)`
collapsed key+value into one placeholder — whole config keys disappeared instead
of showing `key: "[REDACTED:...]"`.
"""

from agentforge.secret_redactor import SecretRedactor


def _redact(text: str) -> str:
    # Construct directly (not the config-backed singleton) so the test is hermetic.
    return SecretRedactor().redact(text).text


def test_high_entropy_catch_all_redacts_unknown_tokens():
    # A token with NO known format under a NON-credential key name — the residual
    # the keyword/signature layers can't reach. The high-entropy layer catches it.
    for line in [
        'mystery_field: "F7HHYZMXZCKIGIXUYHFZ1234"',
        'x: "aB3kQ9zR7yL4mP2wN8tH6jF1cD5eG0v"',
    ]:
        assert "[REDACTED:" in _redact(line), line


def test_high_entropy_keeps_structured_identifiers():
    # Separators break the contiguous run, so word/version identifiers survive.
    for line in [
        'model: "devstral-small-2:24b-cloud"',
        'name: "qwen3-embedding:8b"',
        'collection_name: "agentforge_knowledge_base"',
        'stream: "audit:tool_executions"',
    ]:
        assert _redact(line) == line, line


def test_high_entropy_can_be_disabled():
    line = 'x: "aB3kQ9zR7yL4mP2wN8tH6jF1cD5eG0v"'
    assert SecretRedactor(detect_high_entropy=False).redact(line).text == line


def test_high_entropy_does_not_corrupt_paths():
    # Regression: '/' must break the run AND flank-guard protects path segments,
    # so file paths in queries / tool args stay intact (else read_file breaks).
    for line in [
        "/Users/john/projects/app/config.yaml",
        "Show me the content of: /Users/john/projects/app/config.yaml",
        "/data/uploads/019e970703a3d973bab9601a72fcdd884a/file.md",  # 32-hex dir
        "https://example.com/a3d9f1c2b4e5a6d7c8b9e0f1a2b3c4d5/asset",  # hash in URL
    ]:
        assert _redact(line) == line, line


def test_assignment_keeps_key_name_redacts_only_value():
    out = _redact('  encryption_key: "kQ8vN2xZ7yL4mP9wR3tH6jF1aB5cD0eG="')
    assert "encryption_key:" in out  # key preserved
    assert "kQ8vN2xZ7yL4mP9wR3tH6jF1aB5cD0eG=" not in out  # value gone
    assert "[REDACTED:" in out


def test_key_prefix_not_eaten():
    # Pattern starts matching at 'api_key' inside 'brave_api_key' — the prefix
    # must survive (previously produced 'brave_[REDACTED:...]').
    out = _redact('  brave_api_key: "BSA1a2b3c4d5e6f7g8h9i0jKLMNOP"')
    assert "brave_api_key:" in out
    assert "BSA1a2b3c4d5e6f7g8h9i0jKLMNOP" not in out


def test_aws_secret_key_keeps_key_name():
    out = _redact('aws_secret_access_key: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"')
    assert "aws_secret_access_key:" in out
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in out


def test_detect_secrets_password_still_value_only():
    out = _redact('    password: "Sup3rS3cretP@ss!"')
    assert "password:" in out
    assert "Sup3rS3cretP@ss!" not in out


def test_no_secret_left_unchanged():
    text = "host: localhost\nport: 6333\n"
    assert _redact(text) == text


def test_short_password_does_not_clobber_other_lines():
    # A DB password that happens to be a common word ("pass") must be redacted in
    # the URL but NOT clobber every other "pass" substring elsewhere in the file.
    text = (
        "sql_databases:\n"
        "  luna:\n"
        '    url: "postgres://luna:pass@db.example.com:5432/lunadb"\n'
        "routing:\n"
        "  pass_hint: true\n"
        '  note: "pass the heuristic verdict to the LLM"\n'
    )
    out = _redact(text)
    assert "luna:pass@db.example.com" not in out  # the URL/password is redacted
    assert "pass_hint: true" in out  # unrelated key survives intact
    assert "pass the heuristic verdict" in out  # unrelated prose survives intact


def test_client_id_and_token_shapes_redacted():
    # Caught by VALUE shape (and key name), regardless of the surrounding key.
    cases = [
        'cf_access_client_id: "00000000000000000000000000000000.access"',
        'client_id: "000000000000-testdummyclientid0000000000xy.apps.googleusercontent.com"',
        'oauth_token: "EXAMPLETESTOAUTHTOKEN1234567890AB"',
    ]
    for line in cases:
        out = _redact(line)
        assert "[REDACTED:" in out, line
        # key name preserved
        assert line.split(":")[0] in out


def test_non_secret_identifiers_not_redacted():
    # Structured config identifiers must NOT be flagged (entropy would; we don't).
    for line in [
        'collection_name: "agentforge_knowledge_base"',
        'tool_executions: "audit:tool_executions"',
        'model: "devstral-small-2:24b-cloud"',
    ]:
        assert _redact(line) == line, line


def test_multiline_pem_still_redacted():
    text = (
        "key: |\n"
        "  -----BEGIN PRIVATE KEY-----\n"
        "  MIIBVAIBADANBgkqhkiG9w0BAQEFAASCAT4wggE6AgEAAkEA\n"
        "  -----END PRIVATE KEY-----\n"
    )
    out = _redact(text)
    assert "MIIBVAIBADANBgkqhkiG9w0BAQEFAASCAT4" not in out
    assert "[REDACTED:" in out
