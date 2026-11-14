"""Pinned argv actions fill a template and run it without a shell."""

import subprocess

from agentforge.tools import action as action_mod
from agentforge.tools.action import (
    _DEFAULT_ALLOW,
    execute_action,
    parse_allow,
    template_for,
)


def _catalog():
    return parse_allow(_DEFAULT_ALLOW)


def _runner(stdout: str, code: int = 0, stderr: str = ""):
    seen: dict = {}

    def _run(argv, cwd, timeout):
        seen["argv"] = argv
        seen["cwd"] = cwd
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)

    return _run, seen


def test_file_size_uses_the_platform_argv():
    catalog = _catalog()
    darwin, err = template_for(catalog["file-size"], "Darwin")
    linux, linux_err = template_for(catalog["file-size"], "Linux")
    assert err is None and linux_err is None
    assert darwin == ("stat", "-f", "%z", "{path}")
    assert linux == ("stat", "-c", "%s", "{path}")


def test_execute_substitutes_path_as_one_argument():
    run, seen = _runner("4218\n")
    output = execute_action(
        "file-size",
        {"path": "$(rm -rf /)"},
        catalog=_catalog(),
        system="Darwin",
        run=run,
    )
    assert seen["argv"] == ["stat", "-f", "%z", "$(rm -rf /)"]
    assert output.startswith("$ stat -f %z '$(rm -rf /)'")
    assert "4218" in output


def test_git_log_default_and_bounds():
    catalog = _catalog()
    run, seen = _runner("abc\n")
    execute_action("git-log", {"path": "."}, catalog=catalog, system="Linux", run=run)
    assert seen["argv"][-3:] == ["-n", "20", "--oneline"]

    run, seen = _runner("abc\n")
    output = execute_action(
        "git-log",
        {"path": ".", "n": "5"},
        catalog=catalog,
        system="Linux",
        run=run,
    )
    assert seen["argv"][-3:] == ["-n", "5", "--oneline"]
    assert output.startswith("$ git -C . log -n 5 --oneline")

    too_big = execute_action(
        "git-log",
        {"path": ".", "n": 51},
        catalog=catalog,
        system="Linux",
        run=run,
    )
    assert "must be <= 50" in too_big


def test_rejects_unknown_action_and_slot():
    catalog = _catalog()
    run, _seen = _runner("")
    unknown = execute_action("reboot", catalog=catalog, system="Darwin", run=run)
    assert unknown.startswith("Error: unknown action")
    assert "file-size" in unknown
    extra = execute_action(
        "file-bytes",
        {"path": "/etc/hosts", "cmd": "id"},
        catalog=catalog,
        system="Darwin",
        run=run,
    )
    assert "no slot cmd" in extra


def test_missing_slot_and_bad_cwd_do_not_run():
    catalog = _catalog()
    run, seen = _runner("")
    missing = execute_action("file-bytes", catalog=catalog, system="Darwin", run=run)
    assert "needs slot 'path'" in missing
    assert "argv" not in seen
    bad_cwd = execute_action(
        "file-bytes",
        {"path": "/etc/hosts"},
        cwd="/no/such/agentforge-action-dir",
        catalog=catalog,
        system="Darwin",
        run=run,
    )
    assert "cwd is not a directory" in bad_cwd
    assert "argv" not in seen


def test_nonzero_exit_and_timeout():
    catalog = _catalog()
    run, _seen = _runner("", code=1, stderr="stat: bad")
    failed = execute_action(
        "file-size",
        {"path": "/etc/hosts"},
        catalog=catalog,
        system="Linux",
        run=run,
    )
    assert failed.startswith("[exit 1] $ stat -c %s /etc/hosts")
    assert "stat: bad" in failed

    def _timeout(argv, cwd, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    timed = execute_action(
        "file-bytes",
        {"path": "/etc/hosts"},
        catalog=catalog,
        system="Darwin",
        run=_timeout,
        timeout=30,
    )
    assert "timed out after 30s" in timed


def test_action_tool_runs_shell_false(monkeypatch):
    seen: dict = {}

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["shell"] = kwargs["shell"]
        return subprocess.CompletedProcess(argv, 0, stdout="22\n", stderr="")

    monkeypatch.setattr(action_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(action_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        action_mod,
        "load_action_catalog",
        lambda: (_catalog(), None),
    )
    output = action_mod.action("file-size", {"path": "/etc/resolv.conf"})
    assert seen["shell"] is False
    assert seen["argv"] == ["stat", "-c", "%s", "/etc/resolv.conf"]
    assert "22" in output


def test_explicit_allow_replaces_builtins():
    catalog = parse_allow([{"name": "echo-ok", "argv": ["echo", "ok"]}])
    assert list(catalog) == ["echo-ok"]
    run, seen = _runner("ok\n")
    output = execute_action("echo-ok", catalog=catalog, system="Darwin", run=run)
    assert seen["argv"] == ["echo", "ok"]
    assert "ok" in output


def test_bad_allow_list_is_rejected():
    try:
        parse_allow([{"name": "FileSize", "argv": ["stat"]}])
    except ValueError as exc:
        assert "invalid action name" in str(exc)
    else:
        raise AssertionError("expected ValueError")
