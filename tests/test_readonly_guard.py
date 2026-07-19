from agentforge.tools.readonly_guard import is_read_only_safe


def _ssh(cmd):
    return is_read_only_safe("ssh", {"host": "myserver", "command": cmd})


def _shell(cmd):
    return is_read_only_safe("shell", {"command": cmd})


def test_structured_writers_blocked():
    assert is_read_only_safe("code_edit", {"file_path": "/x", "instruction": "y"}) is False
    assert is_read_only_safe("write_file", {"path": "/x", "content": "y"}) is False
    assert is_read_only_safe("delete_file", {"path": "/x"}) is False


def test_structured_readers_allowed():
    assert is_read_only_safe("read_file", {"path": "/x"}) is True
    assert is_read_only_safe("find_files", {"path": "/x", "pattern": "*.py"}) is True


def test_docker_reads_allowed():
    assert _ssh("docker ps -a") is True
    assert _ssh("docker logs converta-mcp-1 --tail 100") is True
    assert _ssh("docker inspect converta-mcp-1") is True
    assert _ssh("docker compose -f docker-compose.ally.yml ps") is True


def test_docker_writes_blocked():
    # The exact activation commands from the failed/read-only runs.
    assert _ssh("docker build --no-cache -t converta-mcp:latest .") is False
    assert _ssh("docker compose -f docker-compose.ally.yml up -d mcp") is False
    assert _ssh("docker compose -f docker-compose.ally.yml restart mcp") is False
    assert _ssh("docker system prune -f") is False


def test_plain_reads_allowed():
    assert _ssh("cat /opt/converta/mcp-server/server.py") is True
    assert _ssh("ls -la /opt/converta/") is True
    assert _shell("grep -r foo /tmp") is True
    assert _ssh("systemctl status nginx") is True
    assert _ssh("git status") is True


def test_writes_and_redirections_blocked():
    assert _ssh("mkdir -p /opt/converta/mcp-server") is False
    assert _shell("cat /tmp/a > /tmp/b") is False  # redirection to a real file
    assert _ssh("systemctl restart nginx") is False
    assert _ssh("git push origin master") is False


def test_chained_segment_with_a_writer_blocked():
    # A read piped/chained into a writer must fail — every segment must read.
    assert _ssh("docker ps && docker build -t x .") is False
    assert _shell("cat f | tee /tmp/out") is False


def test_redirect_to_devnull_is_fine():
    assert _ssh("docker inspect x 2>/dev/null") is True


def test_empty_command_is_noop_safe():
    assert is_read_only_safe("ssh", {"host": "myserver"}) is True


def test_version_probes_allowed():
    # Felix permission-test chain: all three segments must be read-safe.
    assert _shell("docker ps -a && df -h && npm --version") is True
    assert _shell("npm --version") is True
    assert _shell("npm -v") is True
    assert _shell("node --version") is True
    assert _shell("python3 -V") is True
    assert _shell("docker --version") is True
    assert _shell("npm version") is True  # bare subcommand, no bump args


def test_version_mutations_still_blocked():
    assert _shell("npm version patch") is False
    assert _shell("npm install --version") is False  # install is not info-only
    assert _shell("npm install express") is False
