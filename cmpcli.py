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
import os
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


def run_one(command: str, case: Case, timeout: float, env: dict[str, str] | None = None) -> Run:
    try:
        done = subprocess.run(
            [*shlex.split(command), *case.args],
            input=case.stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **env} if env else None,
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
    env: dict[str, str] | None = None,
) -> list[Difference]:
    differences: list[Difference] = []
    for case in cases:
        left = run_one(reference, case, timeout, env)
        right = run_one(candidate, case, timeout, env)
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


def builtin_cases(name: str) -> list[Case]:
    """The packaged cases for a utility."""
    return [Case(args, stdin, label) for args, stdin, label in CORPUS[name]]


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
    parser.add_argument("--reference", help="the command whose behaviour is correct")
    parser.add_argument("--candidate", help="the command being checked against it")
    parser.add_argument("--cases", type=pathlib.Path, help="JSON file of invocations")
    parser.add_argument(
        "--builtin",
        metavar="UTILITY",
        help="use the packaged cases for this utility; --list-builtin names them",
    )
    parser.add_argument(
        "--list-builtin", action="store_true", help="print the packaged utilities and exit"
    )
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
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="set a variable for both commands; repeatable. Locale decides how "
             "GNU tools quote and collate, so leaving it to the shell makes "
             "the comparison depend on who ran it",
    )
    parser.add_argument(
        "--c-locale",
        action="store_true",
        help="shorthand for --env LC_ALL=C --env LANG=C --env TZ=UTC",
    )
    parser.add_argument("--ignore-stderr", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    parser.add_argument("--version", action="version", version=f"cmpcli {__version__}")
    args = parser.parse_args(argv)

    if args.list_builtin:
        for name, cases in sorted(CORPUS.items()):
            print(f"  {name:<10} {len(cases)} cases")
        return 0

    cases: list[Case] = []
    if args.builtin:
        if args.builtin not in CORPUS:
            parser.error(
                f"no packaged cases for {args.builtin!r}; "
                f"try one of {', '.join(sorted(CORPUS))}"
            )
        cases.extend(builtin_cases(args.builtin))
    if args.cases:
        try:
            cases.extend(load_cases(args.cases))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            parser.error(f"could not read {args.cases}: {error}")
    cases.extend(Case(shlex.split(line), args.stdin) for line in args.inline_cases)
    if not cases:
        parser.error("give at least one case with --builtin, --cases or --arg")

    if not args.reference or not args.candidate:
        parser.error("--reference and --candidate are both needed")

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

    env: dict[str, str] = {}
    if args.c_locale:
        env.update(LC_ALL="C", LANG="C", TZ="UTC")
    for setting in args.env:
        key, separator, value = setting.partition("=")
        if not separator:
            parser.error(f"--env wants KEY=VALUE, got {setting!r}")
        env[key] = value

    differences = compare(
        args.reference,
        args.candidate,
        cases,
        normaliser,
        args.timeout,
        args.ignore_stderr,
        env or None,
    )

    if not args.quiet:
        for difference in differences:
            show(difference)

    print(f"\n{len(cases)} case(s), {len(differences)} differing")
    return 1 if differences else 0


# --------------------------------------------------------------------------
# The built-in corpus.
#
# Arguments chosen for the places two implementations actually drift:
# formatting, flag parsing, boundary values and the exact text of a
# diagnostic. Not coverage of the happy path, which everyone gets right.
# Each case is (args, stdin, name).

CORPUS: dict[str, list[tuple[list[str], str, str]]] = {
    "date": [
        (["-u", "-d", "@0", "+%Y-%m-%dT%H:%M:%S"], "", "the epoch"),
        (["-u", "-d", "@-1", "+%Y-%m-%dT%H:%M:%S"], "", "one second before the epoch"),
        (["-u", "-d", "@1234567890", "+%s"], "", "back to an epoch second"),
        (["-u", "-d", "@1234567890.5", "+%s"], "", "a fractional epoch second"),
        (["-u", "-d", "@-1.5", "+%s"], "", "a negative fractional second, which rounds differently"),
        (["-u", "-d", "2024-02-29", "+%Y-%m-%d"], "", "a leap day"),
        (["-u", "-d", "2023-02-29", "+%Y-%m-%d"], "", "a leap day that is not one"),
        (["-u", "-d", "2024-01-31 +1 month", "+%Y-%m-%d"], "", "a month that overflows"),
        (["-u", "-d", "next monday", "+%u"], "", "a relative weekday"),
        (["-u", "-d", "@1234567890", "+%Ey|%EY|%EC|%Ec|%Ex|%EX"], "", "the E modifier"),
        (["-u", "-d", "@1234567890", "+%Oy|%Om|%Od|%OH"], "", "the O modifier"),
        (["-u", "-d", "@1234567890", "+%-Ey|%10EY|%-Oy|%10OY"], "", "a modifier behind a flag or width"),
        (["-u", "-d", "@1234567890", "+%Ej|%E|%E9|%%Ey"], "", "a modifier that modifies nothing"),
        (["-u", "-d", "@1234567890", "+%:z|%::z|%:::z"], "", "the colon forms of the offset"),
        (["-u", "-d", "@1234567890", "+%N|%3N|%9N"], "", "nanoseconds, with and without a width"),
        (["-u", "-d", "@1234567890", "+%G-%V-%u"], "", "the ISO week date"),
        (["-u", "-d", "@1234567890", "+%-d|%_d|%0d|%^a|%#A"], "", "the GNU flags"),
        (["-u", "-d", "nonsense", "+%Y"], "", "an input to reject"),
        (["-u", "-d", "@1234567890", "+%q|%C|%e|%k|%l|%P|%r"], "", "the less common conversions"),
    ],
    "seq": [
        (["1", "5"], "", "counting up"),
        (["5", "1"], "", "a range that counts down without a step"),
        (["3", "-1", "1"], "", "counting down"),
        (["1", "0", "3"], "", "a zero increment, which is an error"),
        (["0.1", "0.1", "0.5"], "", "a fractional step, where rounding shows"),
        (["1e2"], "", "an exponent"),
        (["-w", "1", "10"], "", "equal width"),
        (["-s", ",", "1", "3"], "", "a separator"),
        (["-f", "%.3f", "1", "3"], "", "a format"),
        (["-f", "%05.1f", "1", "3"], "", "a format with padding"),
        (["1000000000000000000000", "1000000000000000000002"], "", "past what a double holds"),
        (["9007199254740993", "9007199254740995"], "", "past the exact integer range"),
    ],
    "printf": [
        (["%d\\n", "5"], "", "an integer"),
        (["%5d|%-5d|\\n", "5", "5"], "", "width and left alignment"),
        (["%05.2f\\n", "1.5"], "", "zero padding a float"),
        ([".0f: %.0f %.0f %.0f\\n", "0.5", "1.5", "2.5"], "", "rounding at a half"),
        (["%x|%#x|%o|%#o\\n", "255", "255", "8", "8"], "", "the alternate forms"),
        (["%a\\n", "1.5"], "", "a hex float"),
        (["%c\\n", "abc"], "", "more than one character for %c"),
        (["%b\\n", "a\\\\tb"], "", "an escape in %b"),
        (["%d\\n", "0x10"], "", "a hexadecimal argument"),
        (["%d\\n", "'a"], "", "a character as a number"),
        (["%d\\n", "9223372036854775808"], "", "past a signed 64-bit integer"),
        (["%s%s\\n", "a"], "", "fewer arguments than conversions"),
        (["%d\\n", "5", "6", "7"], "", "more arguments than conversions"),
        (["\\\\u00e9\\n"], "", "a unicode escape"),
    ],
    "numfmt": [
        (["--to=iec", "1023"], "", "just under a power of two"),
        (["--to=iec", "1024"], "", "a power of two"),
        (["--to=si", "999"], "", "just under a power of ten"),
        (["--to=si", "1000"], "", "a power of ten"),
        (["--to=iec", "--round=down", "1025"], "", "rounding down"),
        (["--to=iec", "--round=up", "1025"], "", "rounding up"),
        (["--to=iec", "--round=nearest", "1536"], "", "rounding at a half"),
        (["--from=auto", "1Ki"], "", "reading an iec suffix"),
        (["--from=si", "1M"], "", "reading an si suffix"),
        (["--grouping", "1234567"], "", "digit grouping"),
        (["--to=iec", "--padding=10", "1024"], "", "padding"),
        (["--to=si", "--", "-1000"], "", "a negative number"),
        (["--to=iec", "1208925819614629174706176"], "", "a number past 64 bits"),
        (["--invalid=ignore", "abc"], "", "input that is not a number"),
    ],
    "sort": [
        ([], "b\na\nB\nA\n", "the default order"),
        (["-f"], "b\na\nB\nA\n", "folding case"),
        (["-n"], "10\n9\n-1\n1.5\n", "numeric"),
        (["-g"], "1e3\n2\n-1e-3\n", "general numeric"),
        (["-h"], "1K\n1M\n1G\n500\n", "human readable sizes"),
        (["-V"], "1.0.9\n1.0.10\n1.0.2\n", "version sort"),
        (["-M"], "JAN\nFEB\nMAR\nDEC\n", "month names"),
        (["-u"], "a\na\nb\n", "unique"),
        (["-b", "-u"], "  a\n a\na\n", "ignoring leading blanks while uniquing"),
        (["-k2,2", "-k1,1"], "a\tb\nA\tb\n", "two keys"),
        (["-t", ",", "-k2"], "x,2\ny,1\n", "a field separator"),
        (["-z"], "a\x00b\x00", "NUL separated input"),
        (["-r", "-n"], "1\n10\n2\n", "reversed numeric"),
    ],
    "uniq": [
        ([], "a\na\nb\nA\n", "the default"),
        (["-c"], "a\na\nb\n", "counting"),
        (["-d"], "a\na\nb\n", "only duplicates"),
        (["-u"], "a\na\nb\n", "only singletons"),
        (["-i"], "a\nA\n", "ignoring case"),
        (["-D"], "a\na\nb\n", "every duplicate line"),
        (["-w", "1"], "ax\nay\nb\n", "comparing a prefix"),
        (["-s", "1"], "xa\nya\nb\n", "skipping characters"),
        (["-f", "1"], "x a\ny a\nz b\n", "skipping fields"),
    ],
    "cut": [
        (["-d", ",", "-f", "2"], "a,b,c\n", "a field"),
        (["-d", ",", "-f", "2-"], "a,b,c\n", "an open range"),
        (["-d", ",", "-f", "1,3", "--output-delimiter=:"], "a,b,c\n", "an output delimiter"),
        (["-d", ",", "-f", "2-3", "--complement"], "a,b,c,d\n", "the complement"),
        (["-c", "2-4"], "abcdef\n", "characters"),
        (["-b", "2-4"], "abcdef\n", "bytes"),
        (["-c", "2"], "日本語\n", "a character in a multibyte line"),
        (["-b", "2-4"], "日本語abc\n", "bytes in a multibyte line"),
        (["-d", ",", "-f", "1", "-z"], "a,b\x00c,d\x00", "NUL separated"),
        (["-d", ",", "-f", "1"], "no delimiter here\n", "a line without the delimiter"),
    ],
    "tr": [
        (["a-z", "A-Z"], "abcABC\n", "a range"),
        (["[:lower:]", "[:upper:]"], "abcABC\n", "a character class"),
        (["-s", "a"], "aaa\n", "squeezing"),
        (["-d", "b"], "abc\n", "deleting"),
        (["-c", "a", "z"], "abc\n", "the complement"),
        (["-cd", "a"], "abc\n", "deleting the complement"),
        (["abc", "xy"], "abc\n", "a shorter replacement set"),
        (["-t", "abc", "xy"], "abc\n", "truncating the first set"),
        (["[=a=]", "x"], "abc\n", "an equivalence class"),
        (["[:alpha:]", "[:digit:]"], "hello\n", "a class that cannot be a target"),
        (["abc", "[x*]"], "abc\n", "a repeated character"),
    ],
    "wc": [
        (["-c"], "日本語\n", "bytes of a multibyte line"),
        (["-m"], "日本語\n", "characters of a multibyte line"),
        (["-w"], "a b  c\n", "words separated by runs of blanks"),
        (["-l"], "a\nb", "a last line without a newline"),
        (["-L"], "a\nbb\nccc\n", "the longest line"),
        (["-L"], "a\tb\n", "the longest line, counting a tab"),
        (["-c"], "", "empty input"),
    ],
    "fold": [
        (["-w", "3"], "aaaaaaaaaa\n", "wrapping"),
        (["-w", "4"], "日本語テキスト\n", "wrapping a multibyte line"),
        (["-s", "-w", "4"], "ab cd ef\n", "breaking at a space"),
        (["-b", "-w", "2"], "abcdef\n", "counting bytes"),
        (["-w", "4"], "ab\tcd\n", "a tab, which advances to a stop"),
    ],
    "od": [
        (["-t", "x1"], "abcdefgh", "hex bytes"),
        (["-t", "c"], "ab\n", "characters"),
        (["-t", "d2"], "abcd", "signed two-byte"),
        (["-t", "u4"], "abcd", "unsigned four-byte"),
        (["-t", "f4"], "abcd", "a float"),
        (["-A", "n", "-t", "x1"], "ab", "no address column"),
        (["-A", "x", "-t", "x1"], "ab", "a hexadecimal address"),
        (["-j", "1", "-N", "2", "-t", "x1"], "abcd", "skipping and limiting"),
        (["-t", "x1z"], "abcdefgh", "the trailing character column"),
        (["-v", "-t", "x1"], "aaaaaaaaaaaaaaaa", "repeated lines, not elided"),
    ],
    "head": [
        (["-n", "2"], "1\n2\n3\n4\n5\n", "the first lines"),
        (["-n", "-2"], "1\n2\n3\n4\n5\n", "all but the last lines"),
        (["-n", "0"], "1\n2\n", "no lines"),
        (["-c", "3"], "12345", "bytes"),
        (["-c", "-3"], "12345", "all but the last bytes"),
        (["-q"], "1\n2\n", "no header"),
    ],
    "tail": [
        (["-n", "2"], "1\n2\n3\n4\n5\n", "the last lines"),
        (["-n", "+2"], "1\n2\n3\n4\n5\n", "from a line onwards"),
        (["-n", "0"], "1\n2\n", "no lines"),
        (["-c", "3"], "12345", "bytes"),
        (["-c", "+3"], "12345", "from a byte onwards"),
    ],
    "factor": [
        (["1"], "", "one"),
        (["0"], "", "zero"),
        (["2305843009213693951"], "", "a Mersenne prime"),
        (["18446744073709551615"], "", "the largest 64-bit value"),
        (["1000000007"], "", "a large prime"),
    ],
    "basename": [
        (["/a/b/c.txt", ".txt"], "", "stripping a suffix"),
        (["//"], "", "a doubled root, which POSIX leaves to the system"),
        (["/a/b/"], "", "a trailing slash"),
        (["a"], "", "a bare name"),
    ],
    "expand": [
        (["-t", "4"], "a\tb\n", "one tab stop"),
        (["-t", "2,5"], "a\tb\tc\n", "an explicit list of stops"),
        (["-i"], "  a\tb\n", "only leading tabs"),
        (["-t", "4"], "日\tb\n", "a tab after a wide character"),
    ],
}


if __name__ == "__main__":
    raise SystemExit(main())
