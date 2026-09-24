"""Generates `_juggler/protocol.py` from Juggler's `Protocol.js`.

WHY IT IS GENERATED AND NOT HAND-WRITTEN. `Protocol.js` is the
machine-readable schema of the protocol, and the browser enforces it in a
CLOSED WORLD: an undeclared field is not ignored, it is REJECTED at
runtime, and the session dies at context creation with a green build. The
measured cost of that class of failure in this project is 97 tests out of
133. A hand-written table next to a file that already holds the truth is a
second source that drifts on its own.

⛔ IT READS THE **SHIPPED** PROTOCOL.JS, NOT THE ONE IN THE TREE. They are
two different files that happen to coincide today: generating from the
tree produces a client for a browser no user runs, and the way it fails is
the closed-world way. The default is therefore a binary, not a source
path.

⛔ AND JUGGLER LIVES IN TWO LAYOUTS: `omni.ja` (Windows) and the loose tree
`./chrome/juggler/` (Linux). Both are tried, and it says which one
answered.

⛔ THIS DOES NOT REPLACE `protocol_drift_check.py`. That gate compares
what a REAL client sends against our declaration, and remains the only
thing that knows how to answer that question. Generating both sides from
the same source does not make drift impossible: it makes it UNOBSERVABLE.
See `docs/firefox-stealth-architecture/32-stacco-da-playwright.md` §3.2.

    python scripts/gen_juggler_protocol.py --binary <firefox folder>
    python scripts/gen_juggler_protocol.py --check     (regenerate and compare)
"""
from __future__ import annotations

import argparse
import pprint
import os
import pathlib
import re
import sys
import zipfile

JAR_MEMBER = "chrome/juggler/content/protocol/Protocol.js"
LOOSE_TREE = "chrome/juggler/content/protocol/Protocol.js"

# The combinators that `Protocol.js` actually uses. The vocabulary
# declared in PrimitiveTypes.js has 11; these 8 are the ones exercised. If
# another one shows up one day, the generator REJECTS instead of guessing.
COMBINATORS = {"String", "Number", "Boolean", "Any", "Enum",
               "Nullable", "Optional", "Array"}


# ── reading ──────────────────────────────────────────────────────────────────

def protocol_source(binary: str) -> tuple[str, str]:
    """(label, text) of the SHIPPED Protocol.js. Tries both layouts."""
    base = pathlib.Path(binary)
    loose = base / LOOSE_TREE
    if loose.is_file():
        return ("loose tree: %s" % loose,
                loose.read_bytes().decode("utf-8", "replace"))
    for name in ("omni.ja", os.path.join("browser", "omni.ja")):
        jar = base / name
        if not jar.is_file():
            continue
        try:
            with zipfile.ZipFile(jar) as z:
                if JAR_MEMBER in z.namelist():
                    return ("%s!%s" % (jar, JAR_MEMBER),
                            z.read(JAR_MEMBER).decode("utf-8", "replace"))
        except zipfile.BadZipFile:
            # Firefox may package an optimized jar that zipfile cannot
            # read. Not a case to swallow: say which file and why.
            raise SystemExit(
                "jar %s does not open with zipfile (optimized jar). "
                "Extract it with 7z and pass --proto." % jar)
    raise SystemExit(
        "Protocol.js not found in %s: neither loose tree nor omni.ja"
        % binary)


def strip_comments(s: str) -> str:
    """Removes // and /* */ without touching what is inside strings."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c in ("'", '"', "`"):
            j = i + 1
            while j < n:
                if s[j] == chr(92):
                    j += 2
                    continue
                if s[j] == c:
                    break
                j += 1
            out.append(s[i:j + 1])
            i = j + 1
        elif s.startswith("//", i):
            j = s.find(chr(10), i)
            i = n if j < 0 else j
        elif s.startswith("/*", i):
            j = s.find("*/", i)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def block(s: str, start: int) -> str:
    """The balanced block from the first '{' at `start`, skipping strings."""
    i = s.index("{", start)
    d, j, n = 0, i, len(s)
    while j < n:
        c = s[j]
        if c in ("'", '"', "`"):
            k = j + 1
            while k < n:
                if s[k] == chr(92):
                    k += 2
                    continue
                if s[k] == c:
                    break
                k += 1
            j = k + 1
            continue
        if c == "{":
            d += 1
        elif c == "}":
            d -= 1
            if d == 0:
                return s[i:j + 1]
        j += 1
    raise SystemExit("unbalanced block starting at %d" % start)


def entries(body: str) -> list[tuple[str, str]]:
    """The TOP-level key/value pairs of a { ... } block."""
    inner = body[1:-1]
    out, i, n = [], 0, len(inner)
    while i < n:
        m = re.compile(r"\s*'?([A-Za-z_$][\w$]*)'?\s*:\s*").match(inner, i)
        if not m:
            i += 1
            continue
        key, i = m.group(1), m.end()
        d, j, start = 0, i, i
        while j < n:
            c = inner[j]
            if c in ("'", '"', "`"):
                k = j + 1
                while k < n:
                    if inner[k] == chr(92):
                        k += 2
                        continue
                    if inner[k] == c:
                        break
                    k += 1
                j = k + 1
                continue
            if c in "{[(":
                d += 1
            elif c in "}])":
                d -= 1
            elif c == "," and d == 0:
                break
            j += 1
        out.append((key, inner[start:j].strip()))
        i = j + 1
    return out


# ── types ────────────────────────────────────────────────────────────────────

def parse_type_expr(expr: str, tables: dict) -> dict:
    """A type expression -> a JSON-serializable structure."""
    e = expr.strip()
    if e.startswith("{"):
        return {"k": "Object",
                "fields": {c: parse_type_expr(v, tables)
                           for c, v in entries(block(e, 0))}}
    m = re.match(r"t\.(\w+)\s*\((.*)\)\s*$", e, re.S)
    if m:
        name, inner = m.group(1), m.group(2)
        if name not in COMBINATORS:
            raise SystemExit("unexpected combinator: t.%s" % name)
        if name == "Enum":
            return {"k": "Enum", "values": re.findall(r"'([^']*)'", inner)}
        return {"k": name, "of": parse_type_expr(inner, tables)}
    m = re.match(r"t\.(\w+)\s*$", e)
    if m:
        if m.group(1) not in COMBINATORS:
            raise SystemExit("unexpected combinator: t.%s" % m.group(1))
        return {"k": m.group(1)}
    m = re.match(r"(\w+)\.(\w+)\s*$", e)
    if m and m.group(1) in tables and m.group(2) in tables[m.group(1)]:
        return dict(tables[m.group(1)][m.group(2)], ref="%s.%s" % m.groups())
    raise SystemExit("unparseable type expression: %r" % e[:90])


# ── parsing ──────────────────────────────────────────────────────────────────

def parse_type(text: str) -> dict:
    s = strip_comments(text)
    tables: dict = {}
    for m in re.finditer(r"^const (\w+Types) = \{\};", s, re.M):
        tables[m.group(1)] = {}
    for name in list(tables):
        for m in re.finditer(r"^%s\.(\w+) = " % re.escape(name), s, re.M):
            tables[name][m.group(1)] = parse_type_expr(
                block(s, m.end() - 1), tables)

    domains: dict = {}
    for m in re.finditer(r"^const ([A-Z]\w*) = \{", s, re.M):
        domain = m.group(1)
        body = block(s, m.end() - 1)
        if "methods:" not in body and "events:" not in body:
            continue
        d = {"commands": {}, "events": {}}
        for key, value in entries(body):
            if key not in ("methods", "events"):
                continue
            where = "commands" if key == "methods" else "events"
            for name, body2 in entries(value):
                if where == "events":
                    d["events"][name] = parse_type_expr(body2, tables)
                else:
                    parts = dict(entries(body2))
                    d["commands"][name] = {
                        "params": (
                            parse_type_expr(parts["params"], tables)
                            if "params" in parts else None),
                        "returns": (
                            parse_type_expr(parts["returns"], tables)
                            if "returns" in parts else None),
                    }
        domains[domain] = d
    return domains


# ── emission ─────────────────────────────────────────────────────────────────

HEADER = '''"""GENERATED by scripts/gen_juggler_protocol.py.
Do not edit by hand.

Source: %s
Commands: %d   Events: %d

The browser enforces this schema in a CLOSED WORLD: an undeclared field is
REJECTED at runtime, not ignored. It therefore serves to verify what WE
SEND before it goes out, not to document.

⛔ Does not replace `protocol_drift_check.py` in the source repo: that
gate asks what a REAL client sends, and that is a question this file
cannot ask, because it is generated from the same source it should be
checking.
"""
from __future__ import annotations

'''


def stable_source(source: str) -> str:
    """The source line in the generated file must not contain an absolute
    path: it would change from one machine to another and every
    regeneration would look like a change. And Windows backslashes inside
    a docstring are ESCAPES: `C:\\Users` raises `truncated \\UXXXXXXXX
    escape` and the generated file does not import. It happened on
    2026-08-27, and the way it was noticed was that the file did not
    import, not that the generator failed."""
    f = source.replace(chr(92), "/")
    return (f.split("/")[-1] if "!" not in f
            else "omni.ja!" + f.split("!", 1)[1])


def render(domains: dict, source: str) -> str:
    source = stable_source(source)
    n_cmd = sum(len(d["commands"]) for d in domains.values())
    n_ev = sum(len(d["events"]) for d in domains.values())
    commands, events = {}, {}
    for domain, d in sorted(domains.items()):
        for name, spec in sorted(d["commands"].items()):
            commands["%s.%s" % (domain, name)] = spec
        for name, spec in sorted(d["events"].items()):
            events["%s.%s" % (domain, name)] = spec
    # ⛔ pprint and not json.dumps: json emits `null`/`true`/`false`,
    # which do not exist in Python, and the generated file does not
    # IMPORT. The generator, though, still exits 0, so the failure only
    # shows up when importing - which is why this generator's test
    # imports the file instead of looking at its bytes.
    body = HEADER % (source, n_cmd, n_ev)
    body += "DOMAINS = %s\n\n" % pprint.pformat(sorted(domains), width=78)
    body += "COMMANDS = %s\n\n" % pprint.pformat(commands, width=88,
                                                  sort_dicts=True)
    body += "EVENTS = %s\n" % pprint.pformat(events, width=88,
                                              sort_dicts=True)
    return body


def _selftest(text: str) -> int:
    """Known-bad mutations on Protocol.js, without a browser and without a
    network.

    The question: if the SHIPPED Protocol.js changes, does `--check`
    notice? The text is mutated in memory, regenerated, and compared
    against the body produced from the intact text. A mutation that does
    not move the body is a hole.
    """
    base = render(parse_type(text), "base")

    def body(t):
        i = t.find("DOMAINS = ")
        return t[i:] if i >= 0 else t

    MUTATIONS = [
        ("a command REMOVED",
         lambda s: s.replace(
             "    'collectGarbage': {\n      params: {},\n    },\n",
             "", 1)),
        ("a command ADDED",
         lambda s: s.replace(
             "    'collectGarbage': {",
             "    'invented': {\n      params: {},\n    },\n"
             "    'collectGarbage': {", 1)),
        ("a field removed from a command",
         lambda s: s.replace(
             "        attachToDefaultContext: t.Boolean,\n", "", 1)),
        ("a type changed: String -> Number",
         lambda s: s.replace(
             "        url: t.String,", "        url: t.Number,", 1)),
        ("an Optional becomes required",
         lambda s: s.replace(
             "        browserContextId: t.Optional(t.String),",
             "        browserContextId: t.String,", 1)),
        ("a value removed from an Enum",
         lambda s: s.replace(
             "t.Enum(['reduce', 'no-preference'])",
             "t.Enum(['no-preference'])", 1)),
        ("an event RENAMED",
         lambda s: s.replace(
             "    'ready': {", "    'renamedReady': {", 1)),
        ("an event REMOVED",
         lambda s: s.replace(
             "    'crashed': {" + chr(10) + "    }," + chr(10), "", 1)),
    ]
    print("known-bad mutations on Protocol.js:")
    survived = 0
    for description, mutate in MUTATIONS:
        mutated = mutate(text)
        if mutated == text:
            print("  INERT MUTATION    %-42s (did not touch the text)"
                  % description)
            survived += 1
            continue
        try:
            different = body(render(parse_type(mutated), "base")) != body(base)
        except SystemExit as e:
            different, description = (
                True, description + " -> the generator REJECTS: %s" % e)
        if different:
            print("  killed            %s" % description[:70])
        else:
            print("  SURVIVED          %s" % description)
            survived += 1

    print()
    print("and the cases that must NOT trigger:")
    false_positives = 0
    if body(render(parse_type(text), "another source")) == body(base):
        print("  PASSES            same text, different source in the header")
    else:
        print("  WRONGLY REJECTED  same text with another source")
        false_positives += 1
    # An extra space inside an empty block is a reformat, not a drift: if
    # the gate tripped here it would be red on every touch-up of the
    # source, and a gate that is always red teaches people to route
    # around it.
    reformatted = text.replace("      params: {},", "      params: { },", 1)
    if reformatted == text:
        print("  WRONGLY REJECTED  no empty block to reformat: "
              "case not exercised")
        false_positives += 1
    elif body(render(parse_type(reformatted), "base")) == body(base):
        print("  PASSES            one extra space is not a drift")
    else:
        print("  WRONGLY REJECTED  one extra space")
        false_positives += 1
    # And a new COMMENT is not a drift: the parser strips comments.
    commented = text.replace(
        "const Heap = {", "// a note\nconst Heap = {", 1)
    if (commented != text
            and body(render(parse_type(commented), "base")) == body(base)):
        print("  PASSES            a new comment is not a drift")
    else:
        print("  WRONGLY REJECTED  a new comment")
        false_positives += 1

    print()
    print("survived: %d of %d, wrongly rejected: %d"
          % (survived, len(MUTATIONS), false_positives))
    return 1 if (survived or false_positives) else 0


def main() -> int:
    here = pathlib.Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", help="folder of the SHIPPED firefox")
    ap.add_argument("--proto",
                    help="an already-extracted Protocol.js "
                         "(for a jar that will not open)")
    ap.add_argument("--out", default=str(
        here / "src" / "invisible_playwright" / "_juggler" / "protocol.py"))
    ap.add_argument("--check", action="store_true",
                    help="regenerate and compare, without writing")
    ap.add_argument("--selftest", action="store_true",
                    help="known-bad mutations on Protocol.js, "
                         "without a browser")
    a = ap.parse_args()

    if a.proto:
        source, text = (
            a.proto,
            pathlib.Path(a.proto).read_bytes().decode("utf-8", "replace"))
    elif a.binary:
        source, text = protocol_source(a.binary)
    else:
        ap.error("need --binary (the shipped firefox) or --proto")

    if a.selftest:
        return _selftest(text)

    domains = parse_type(text)
    n_cmd = sum(len(d["commands"]) for d in domains.values())
    n_ev = sum(len(d["events"]) for d in domains.values())
    new = render(domains, source)

    print("source: %s" % source)
    for domain in sorted(domains):
        print("  %-10s %3d commands %3d events"
              % (domain, len(domains[domain]["commands"]),
                 len(domains[domain]["events"])))
    print("  %-10s %3d commands %3d events" % ("TOTAL", n_cmd, n_ev))

    out = pathlib.Path(a.out)
    if a.check:
        if not out.is_file():
            print("MISSING: %s" % out)
            return 1
        old = out.read_bytes().decode("utf-8")
        # compares the BODY, not the header: the source line carries a
        # path that changes from one machine to another.
        def body(t):
            i = t.find("DOMAINS = ")
            return t[i:] if i >= 0 else t
        if body(old) == body(new):
            print("PROTOCOL ALIGNED")
            return 0
        print("DRIFT: the generated file does not match the shipped "
              "Protocol.js")
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(new.encode("utf-8"))
    print("wrote %s (%d bytes)" % (out, len(new)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
