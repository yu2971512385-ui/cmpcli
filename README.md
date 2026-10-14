# cmpcli

Run two commands over the same inputs and report where they disagree.

For reimplementations: a Rust coreutils against GNU, a new awk against the old
one, a rewritten CLI against the one it replaces. Those projects already say
"behave like the reference", so a difference is a bug by their own definition
and there is nothing to argue about in review.

```console
$ cmpcli --reference /usr/bin/date --candidate ./target/debug/date --cases date.json

### E modifier
    stdout:
      reference: '09\n'
      candidate: '%Ey\n'

### O modifier with a flag
    stdout:
      reference: '9\n'
      candidate: '%Oy\n'

7 case(s), 2 differing
```

That is a real run, and it is where
[uutils/coreutils#15237](https://github.com/uutils/coreutils/pull/15237) came
from.

## Install

```console
$ pipx install git+https://github.com/yu2971512385-ui/cmpcli
```

One file, standard library only, Python 3.10 or newer.

## Use

There is a corpus built in, so there is something to run before you have
written any cases:

```console
$ cmpcli --list-builtin
  basename   4 cases
  cut        10 cases
  date       19 cases
  ...
$ cmpcli --reference /usr/bin/date --candidate ./date --builtin date --c-locale
```

148 cases over 16 utilities, chosen for the places two implementations
actually drift -- formatting, flag parsing, boundary values, the exact text of
a diagnostic -- rather than for coverage of the happy path, which everyone
gets right.

Your own cases work the same way:

```console
$ cmpcli --reference gnu-sort --candidate ./sort --cases sort.json
$ cmpcli --reference /usr/bin/wc --candidate ./wc --arg "-l" --arg "-c" --stdin "a b c"
```

Cases are JSON, either bare argument lists or objects:

```json
[
  ["-n", "5"],
  {"args": ["-d", "@0", "+%Ey"], "name": "E modifier"},
  {"args": ["-r"], "stdin": "unsorted\ninput\n", "name": "reverse"}
]
```

Exit status is 1 when anything differs.

## Normalising

Two programs disagree constantly about things nobody cares about: their own
name in a diagnostic, where they live on disk, their version, the current time.
Left alone those drown the findings, so they are normalised on both sides
before comparing.

What that means in practice, and where the care went:

- **A program naming itself** is replaced only where it is naming itself — at
  the start of a diagnostic, after `Usage: `, inside `Try '…'`. Replacing it
  everywhere would also rewrite it where it is an ordinary word, and
  `date: invalid date 'x'` is exactly the sort of message these tools emit.
  Normalising that second `date` would hide the quoting difference beside it.
- **Filesystem paths** become `PATH`, but a URL is left intact, so a licence
  line in `--version` output still reads as one.
- **Versions, dates and clock times** become `VERSION`, `DATE`, `TIME`.

`--rule 'pid \d+=pid N'` adds your own, repeatably. `--raw` turns all of it
off when you want the bytes compared exactly.

### Everything else

| flag | |
| --- | --- |
| `--builtin UTILITY` | use the packaged cases |
| `--list-builtin` | name the packaged utilities |
| `--arg "..."` | one invocation inline, repeatable |
| `--c-locale` | run both under `LC_ALL=C LANG=C TZ=UTC` |
| `--env KEY=VALUE` | set a variable for both, repeatable |
| `--stdin TEXT` | stdin for the inline invocations |
| `--ignore-stderr` | compare stdout and exit status only |
| `--timeout S` | per run, default 10s; a timeout is reported, never hung on |
| `--quiet` | summary only |

## Set the locale

Locale decides how GNU tools quote (`'0'` or `‘0’`) and how they
collate, so leaving it to whoever runs the comparison makes the result depend
on their shell. Running the built-in `sort` and `seq` cases without
`--c-locale` reports two differences that disappear with it -- neither of them
anything to do with the programs.

`--c-locale` is the usual answer; `--env` covers the rest.

## Reading the output

A difference is printed per case, naming only the fields that actually differ,
with both sides quoted so whitespace and invisible characters are visible.
That matters more than it sounds: the difference is often a trailing space or
a curly quote.

## Licence

MIT.
