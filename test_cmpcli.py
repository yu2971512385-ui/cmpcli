from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

import cmpcli

HERE = pathlib.Path(__file__).resolve().parent


def script(tmp_path: pathlib.Path, name: str, body: str) -> str:
    """A tiny Python program, invoked the way a real command would be."""
    path = tmp_path / f"{name}.py"
    path.write_text(body)
    return f"{sys.executable} {path}"


ECHO_ARGS = "import sys; print(' '.join(sys.argv[1:]))"
ECHO_ARGS_UPPER = "import sys; print(' '.join(sys.argv[1:]).upper())"
FAILS = "import sys; sys.exit(3)"
NAMES_ITSELF = r"""
import sys, pathlib
print(f"{pathlib.Path(sys.argv[0]).stem}: invalid date 'x'", file=sys.stderr)
"""


def normaliser(names=("a", "b"), rules=None, enabled=True) -> cmpcli.Normaliser:
    return cmpcli.Normaliser(names, rules or [], enabled)


def test_identical_commands_agree(tmp_path):
    command = script(tmp_path, "echo", ECHO_ARGS)
    differences = cmpcli.compare(
        command, command, [cmpcli.Case(["x", "y"])], normaliser(), 10.0, False
    )
    assert differences == []


def test_different_stdout_is_reported(tmp_path):
    left = script(tmp_path, "lower", ECHO_ARGS)
    right = script(tmp_path, "upper", ECHO_ARGS_UPPER)
    differences = cmpcli.compare(
        left, right, [cmpcli.Case(["hello"])], normaliser(), 10.0, False
    )
    assert len(differences) == 1
    assert differences[0].fields() == ["stdout"]


def test_different_exit_status_is_reported(tmp_path):
    left = script(tmp_path, "ok", ECHO_ARGS)
    right = script(tmp_path, "bad", FAILS)
    differences = cmpcli.compare(left, right, [cmpcli.Case([])], normaliser(), 10.0, False)
    assert "status" in differences[0].fields()


def test_stdin_reaches_both(tmp_path):
    command = script(tmp_path, "cat", "import sys; sys.stdout.write(sys.stdin.read())")
    other = script(tmp_path, "rev", "import sys; sys.stdout.write(sys.stdin.read()[::-1])")
    differences = cmpcli.compare(
        command, other, [cmpcli.Case([], stdin="abc")], normaliser(), 10.0, False
    )
    assert differences[0].reference.stdout == "abc"
    assert differences[0].candidate.stdout == "cba"


def test_timeout_is_a_difference_not_a_hang(tmp_path):
    quick = script(tmp_path, "quick", ECHO_ARGS)
    slow = script(tmp_path, "slow", "import time; time.sleep(5)")
    differences = cmpcli.compare(quick, slow, [cmpcli.Case([])], normaliser(), 0.3, False)
    assert differences[0].candidate.status == 124
    assert "timed out" in differences[0].candidate.stderr


def test_missing_command_is_reported_not_raised(tmp_path):
    present = script(tmp_path, "present", ECHO_ARGS)
    differences = cmpcli.compare(
        present, "/nonexistent/command", [cmpcli.Case([])], normaliser(), 10.0, False
    )
    assert differences[0].candidate.status == 127


class TestNormalising:
    def test_leaves_a_url_alone(self):
        text = "see <https://gnu.org/licenses/gpl.html>\n"
        assert normaliser().apply(text) == text

    def test_replaces_a_filesystem_path(self):
        assert "PATH" in normaliser().apply("Try '/usr/local/bin/thing --help'")

    def test_replaces_a_program_naming_itself_but_not_the_same_word(self):
        # `date: invalid date 'x'` must keep the second "date": it is a noun,
        # and the quoting difference next to it is the thing worth seeing.
        result = normaliser(names=("date", "date")).apply("date: invalid date 'x'\n")
        assert result == "PROGRAM: invalid date 'x'\n"

    def test_replaces_a_version(self):
        assert normaliser().apply("tool 1.2.3\n") == "tool VERSION\n"

    def test_raw_mode_changes_nothing(self):
        text = "date: /a/b 1.2.3\n"
        assert normaliser(enabled=False).apply(text) == text

    def test_extra_rules_are_applied(self):
        n = normaliser(rules=[(r"pid \d+", "pid N")])
        assert n.apply("pid 1234\n") == "pid N\n"


def test_normalising_hides_a_difference_that_is_only_the_name(tmp_path):
    left = script(tmp_path, "alpha", NAMES_ITSELF)
    right = script(tmp_path, "beta", NAMES_ITSELF)
    case = [cmpcli.Case([])]

    names = ("alpha", "beta")
    assert cmpcli.compare(left, right, case, cmpcli.Normaliser(names, [], True), 10.0, False) == []
    # Without normalising, the two differ only because of their own names.
    assert cmpcli.compare(left, right, case, cmpcli.Normaliser(names, [], False), 10.0, False)


def test_ignore_stderr(tmp_path):
    left = script(tmp_path, "quiet", "pass")
    right = script(tmp_path, "noisy", "import sys; print('warning', file=sys.stderr)")
    case = [cmpcli.Case([])]
    assert cmpcli.compare(left, right, case, normaliser(), 10.0, True) == []
    assert cmpcli.compare(left, right, case, normaliser(), 10.0, False)


class TestLoadingCases:
    def test_list_of_argument_lists(self, tmp_path):
        path = tmp_path / "cases.json"
        path.write_text(json.dumps([["-n", "1"], ["--help"]]))
        cases = cmpcli.load_cases(path)
        assert [c.args for c in cases] == [["-n", "1"], ["--help"]]

    def test_objects_with_stdin_and_name(self, tmp_path):
        path = tmp_path / "cases.json"
        path.write_text(json.dumps([{"args": ["-r"], "stdin": "ab", "name": "reverse"}]))
        case = cmpcli.load_cases(path)[0]
        assert (case.args, case.stdin, case.label) == (["-r"], "ab", "reverse")

    def test_label_falls_back_to_the_arguments(self):
        assert cmpcli.Case(["-n", "1"]).label == "-n 1"
        assert cmpcli.Case([]).label == "(no arguments)"

    def test_rejects_a_malformed_case(self, tmp_path):
        path = tmp_path / "cases.json"
        path.write_text(json.dumps(["just a string"]))
        with pytest.raises(ValueError):
            cmpcli.load_cases(path)


def test_cli_exit_codes(tmp_path, capsys):
    left = script(tmp_path, "lower", ECHO_ARGS)
    right = script(tmp_path, "upper", ECHO_ARGS_UPPER)

    assert cmpcli.main(["--reference", left, "--candidate", left, "--arg", "x", "--quiet"]) == 0
    assert cmpcli.main(["--reference", left, "--candidate", right, "--arg", "x", "--quiet"]) == 1
    assert "1 differing" in capsys.readouterr().out


def test_cli_runs_as_a_script(tmp_path):
    command = script(tmp_path, "echo", ECHO_ARGS)
    done = subprocess.run(
        [sys.executable, str(HERE / "cmpcli.py"),
         "--reference", command, "--candidate", command, "--arg", "x"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert "1 case(s), 0 differing" in done.stdout
