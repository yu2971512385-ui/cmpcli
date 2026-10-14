#!/usr/bin/env python3
"""Run two commands over the same inputs and report where they disagree.

Written for reimplementations: a Rust coreutils against GNU, a new awk against
the old one, a rewritten CLI against the one it replaces. The project's own
standard is "behave like the reference", so a difference is a bug by its own
definition and there is nothing to argue about.

The hard part is not running both -- it is that the two disagree constantly on
things nobody cares about: their own name in a usage line, a path, a version, a
timestamp. Those drown the real findings, so normalisation is built in rather
than left to the caller.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import shlex
import subprocess

__version__ = "0.1.0"


@dataclasses.dataclass(frozen=True)
class Case:
    """One invocation to run against both commands."""

    args: list[str]
    stdin: str = ""
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or " ".join(shlex.quote(a) for a in self.args) or "(no arguments)"


@dataclasses.dataclass(frozen=True)
class Run:
    stdout: str
    stderr: str
    status: int


@dataclasses.dataclass(frozen=True)
class Difference:
    case: Case
    reference: Run
    candidate: Run

    def fields(self) -> list[str]:
        differing = []
        if self.reference.stdout != self.candidate.stdout:
            differing.append("stdout")
        if self.reference.stderr != self.candidate.stderr:
            differing.append("stderr")
        if self.reference.status != self.candidate.status:
            differing.append("status")
        return differing


class Normaliser:
    """Rewrites output so that uninteresting differences stop being differences."""

    #: Replacements applied to both sides, in order.
    BUILT_IN: tuple[tuple[str, str], ...] = (
        # A filesystem path, which differs because the two binaries live
        # elsewhere. The lookbehind keeps the "//" of a URL out of it, so a
        # licence line in --version output survives intact.
        (r"(?<![:\w/])/[\w.\-]+(/[\w.\-]+)+", "PATH"),
        # Version strings: "foo 1.2.3", "foo (GNU coreutils) 9.4".
        (r"\b\d+\.\d+(\.\d+)*(-\w+)?\b", "VERSION"),
        # Times and dates that move between the two runs.
        (r"\b\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?\b", "DATE"),
        (r"\b\d{2}:\d{2}:\d{2}\b", "TIME"),
    )

    def __init__(self, program_names: tuple[str, str], rules: list[tuple[str, str]], enabled: bool) -> None:
        self.enabled = enabled
        self.rules = [(re.compile(pattern), replacement) for pattern, replacement in rules]
        self.built_in = [(re.compile(p), r) for p, r in self.BUILT_IN]
        # Only where a program names itself: at the start of a diagnostic, in
        # a usage line, or in the "Try 'x --help'" footer. Replacing the name
        # everywhere would also rewrite it where it is an ordinary word, and
        # `date: invalid date` is exactly the kind of message these tools emit.
        self.name_patterns = [
            re.compile(pattern.replace("NAME", re.escape(name)))
            for name in program_names
            if name
            for pattern in (r"(?m)^NAME(?=:)", r"(?<=')NAME(?= )", r"(?<=Usage: )NAME\b")
        ]

    def apply(self, text: str) -> str:
        if not self.enabled:
            return text
        for pattern in self.name_patterns:
            text = pattern.sub("PROGRAM", text)
        for pattern, replacement in self.built_in:
            text = pattern.sub(replacement, text)
        for pattern, replacement in self.rules:
            text = pattern.sub(replacement, text)
        return text


def run_one(command: str, case: Case, timeout: float) -> Run:
    try:
        done = subprocess.run(
            [*shlex.split(command), *case.args],
            input=case.stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Run("", f"cmpcli: timed out after {timeout}s", 124)
    except (OSError, ValueError) as error:
        return Run("", f"cmpcli: {error}", 127)
    return Run(done.stdout, done.stderr, done.returncode)


def compare(
    reference: str,
    candidate: str,
    cases: list[Case],
    normaliser: Normaliser,
    timeout: float,
    ignore_stderr: bool,
) -> list[Difference]:
    differences: list[Difference] = []
    for case in cases:
        left = run_one(reference, case, timeout)
        right = run_one(candidate, case, timeout)
        left_n = Run(
            normaliser.apply(left.stdout),
            "" if ignore_stderr else normaliser.apply(left.stderr),
            left.status,
        )
        right_n = Run(
            normaliser.apply(right.stdout),
            "" if ignore_stderr else normaliser.apply(right.stderr),
            right.status,
        )
        if dataclasses.astuple(left_n) != dataclasses.astuple(right_n):
            differences.append(Difference(case, left_n, right_n))
    return differences


def load_cases(path: pathlib.Path) -> list[Case]:
    """Read cases from JSON.

    Either a list of argument lists, or a list of objects with `args` and
    optionally `stdin` and `name`.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases: list[Case] = []
    for entry in raw:
        if isinstance(entry, list):
            cases.append(Case([str(a) for a in entry]))
        elif isinstance(entry, dict):
            cases.append(
                Case(
                    [str(a) for a in entry.get("args", [])],
                    entry.get("stdin", ""),
                    entry.get("name", ""),
                )
            )
        else:
            raise ValueError(f"a case must be a list or an object, not {type(entry).__name__}")
    return cases


def show(difference: Difference) -> None:
    print(f"\n### {difference.case.label}")
    if difference.case.stdin:
        print(f"    stdin: {difference.case.stdin!r}")
    for field in difference.fields():
        left = getattr(difference.reference, field)
        right = getattr(difference.candidate, field)
        print(f"    {field}:")
        print(f"      reference: {left!r}")
        print(f"      candidate: {right!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cmpcli",
        description="Run two commands over the same inputs and report where they disagree.",
        epilog='example: cmpcli --reference /usr/bin/sort --candidate ./target/debug/sort --cases sort.json',
    )
    parser.add_argument("--reference", required=True, help="the command whose behaviour is correct")
    parser.add_argument("--candidate", required=True, help="the command being checked against it")
    parser.add_argument("--cases", type=pathlib.Path, help="JSON file of invocations")
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        dest="inline_cases",
        metavar="ARGS",
        help="one invocation, as a shell-quoted string; repeatable",
    )
    parser.add_argument("--stdin", default="", help="stdin for the inline invocations")
    parser.add_argument(
        "--rule",
        action="append",
        default=[],
        metavar="PATTERN=REPLACEMENT",
        help="extra normalisation, applied to both sides; repeatable",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="compare output exactly, without normalising paths, versions or dates",
    )
    parser.add_argument("--ignore-stderr", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    parser.add_argument("--version", action="version", version=f"cmpcli {__version__}")
    args = parser.parse_args(argv)

    cases: list[Case] = []
    if args.cases:
        try:
            cases.extend(load_cases(args.cases))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            parser.error(f"could not read {args.cases}: {error}")
    cases.extend(Case(shlex.split(line), args.stdin) for line in args.inline_cases)
    if not cases:
        parser.error("give at least one case with --cases or --arg")

    rules: list[tuple[str, str]] = []
    for rule in args.rule:
        pattern, separator, replacement = rule.partition("=")
        if not separator:
            parser.error(f"--rule wants PATTERN=REPLACEMENT, got {rule!r}")
        rules.append((pattern, replacement))

    normaliser = Normaliser(
        (pathlib.Path(shlex.split(args.reference)[0]).name,
         pathlib.Path(shlex.split(args.candidate)[0]).name),
        rules,
        enabled=not args.raw,
    )

    differences = compare(
        args.reference, args.candidate, cases, normaliser, args.timeout, args.ignore_stderr
    )

    if not args.quiet:
        for difference in differences:
            show(difference)

    print(f"\n{len(cases)} case(s), {len(differences)} differing")
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
