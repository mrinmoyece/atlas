"""Tools the agents use to inspect a repository.

Three properties every tool here holds to, because agents are adversarial
inputs by construction (the model chooses the arguments, and the model can
be steered by content it reads):

  1. **Confined.** Confinement lives in `_resolve` (direct reads) *and* in
     `_walk` (everything that scans). Both are needed: an earlier version
     confined only `read_file`, so a symlink inside the repo pointing
     anywhere on disk was readable through `grep` while being rejected
     through `read_file`. Symlink targets are resolved and checked, not just
     the link path.
  2. **Bounded** in four dimensions, because output size alone is not
     enough: output chars, match count, lines scanned, and wall-clock time.
     The clock is what defends against a model-supplied regex causing
     catastrophic backtracking. `read_file` streams to its window instead of
     materialising the file.
  3. **Deterministic.** Same repo, same arguments, same bytes out. Without
     this the eval numbers mean nothing.

Tools are plain callables plus a JSON schema; they are exposed to LangGraph
directly and re-exported over MCP by `atlas.mcp_layer.server` from the same
definitions - one source of truth, two transports.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import multiprocessing
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MAX_OUTPUT_CHARS = 8_000
# Upper bound on one read_file window. Comfortably more than MAX_OUTPUT_CHARS
# can hold, so it never truncates a result that would have been returned.
MAX_READ_LINES = 2_000
MAX_MATCHES = 40
MAX_PATTERN_CHARS = 200
# Lines scanned per grep. Bounds total work even if a single line is cheap
# but there are millions of them.
MAX_LINES_SCANNED = 200_000
# Wall-clock ceiling for one grep. Catastrophic backtracking is not
# detectable by inspection in general, so the defence is a clock.
GREP_DEADLINE_S = 2.0
# Extra time the parent waits before concluding the child is wedged rather
# than merely finishing up. Small: the child enforces GREP_DEADLINE_S itself
# on every path that can observe a clock.
GREP_KILL_GRACE_S = 1.0

# Static guard against catastrophic backtracking.
#
# It has to carry the weight, because the wall-clock deadline below can only
# be checked BETWEEN lines - nothing can interrupt a single `regex.search()`
# that is already spinning. (Finding that out took a hung test suite: the
# deadline looked like a complete defence and bounded nothing within one
# pathological line.)
#
# The guard must also not be blunt. Rejecting *every* quantified group would
# block `(x|y)*z`, which is perfectly safe and perfectly common. What makes a
# quantified group dangerous is **ambiguity** - more than one way for the
# engine to match the same input:
#
#   (a+)+          inner quantifier      -> exponential
#   (a|a)*         duplicate branches    -> exponential
#   (a|ab|abc)+    prefix-overlapping    -> exponential
#   (x|y)*         disjoint branches     -> linear, allowed
# Note `[^()]*?` and the nested-group alternative: an earlier version used
# `[^()]*`, so any group containing a nested group was skipped entirely.
_QUANTIFIED_GROUP = re.compile(r"\((\(?[^()]*\)?[^()]*)\)\s*([+*]|\{\d*,?\d*\})")
# A quantifier is `+`, `*`, `?` or `{n,m}`. Each omission has cost a round:
# checking only `+` and `*` let `(a{2,}){2,}$` through; adding `{n,m}` but
# not `?` let `(a?){20}a{20}$` through, which is the textbook exponential
# payload and hung a 40-second probe. Enumerate all four.
_INNER_QUANTIFIER = re.compile(r"[+*?]|\{\d*,?\d*\}")


def _is_ambiguous_quantified_group(body: str) -> bool:
    """True if a quantified group can match the same text more than one way.

    All four quantifier forms count here - `+`, `*`, `?` and `{n,m}` - and
        every omission has cost a round of review:

          * v1 tested `any(q in body for q in ("+", "*"))`. It caught `(a+)+`
            and waved through `(a{2,}){2,}`, the identical catastrophic pattern
            written in the other notation. Measured against a 61-byte file:
            `(a+)+$` rejected in microseconds, `(a{2,}){2,}$` still spinning
            after 40 seconds against a 2-second deadline that cannot interrupt
            it. `([a-z]{1,20})+$` and `(?:a{2,})+$` were through too.
          * v2 added `{n,m}` and still omitted `?`, so `(a?){20}a{20}$` - the
            textbook exponential payload from every ReDoS paper - sailed past
            and hung the probe again.

        The lesson is not "add another character". A denylist over *group*
        syntax cannot be complete, and the counterexample needs no group at all:
        `a*a*a*a*a*a*a*a*a*a*b` against a line of 60 `a`s is exponential and
        every version of this guard accepts it, correctly, because there is
        nothing syntactically wrong with it.

        So this function is a fast-path reject for the obvious payloads, not the
        security boundary. The boundary is `_scan_in_subprocess`, which runs the
        match somewhere it can be killed. Keeping both is deliberate: the guard
        turns a common mistake into an instant, explanatory error instead of a
        two-second timeout, and it costs nothing.
    """
    if _INNER_QUANTIFIER.search(body):
        return True  # nested quantifier, in any notation
    if "|" not in body:
        return False
    branches = [b.strip() for b in body.split("|")]
    if len(set(branches)) != len(branches):
        return True  # duplicate branches
    # Prefix overlap: one branch is a prefix of another, so the engine can
    # split the same input across repetitions in multiple ways.
    return any(
        a and b and a != b and (a.startswith(b) or b.startswith(a))
        for i, a in enumerate(branches)
        for b in branches[i + 1 :]
    )


def find_ambiguous_quantifier(pattern: str) -> str | None:
    """Return the offending fragment, or None if the pattern looks safe."""
    for match in _QUANTIFIED_GROUP.finditer(pattern):
        if _is_ambiguous_quantified_group(match.group(1)):
            return match.group(0)
    return None


# Matching is capped per line for the same reason: backtracking cost grows
# with input length, so bounding the input bounds the worst case even for a
# pattern the static guard did not anticipate.
MAX_LINE_CHARS_MATCHED = 1_000
logger = logging.getLogger(__name__)

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build", ".mypy_cache"}


def _scan_worker(conn, root: str, files: list[str], pattern: str, glob: str) -> None:
    """Child-process entry point. Never raises across the pipe."""
    try:
        conn.send(RepoToolkit(root)._scan(files, pattern, glob))
    except BaseException as exc:  # noqa: BLE001 - the parent gets a string
        conn.send(f"ERROR: {type(exc).__name__}: {exc}")
    finally:
        conn.close()


def _scan_in_subprocess(*, root: Path, files: list[str], pattern: str, glob: str) -> str:
    """Run one grep somewhere it can be killed, and kill it if it overruns.

    This is the ReDoS boundary, and it is a process boundary because nothing
    smaller works. A wall-clock check between lines cannot interrupt a single
    `regex.search()` that is already backtracking, and a syntactic guard over
    quantified groups cannot be complete - `a*a*a*a*a*a*a*a*a*a*b` is
    exponential against 60 characters and contains no group to inspect.

    `fork` rather than `spawn`: the child does nothing but compile a regex
    and read files, so it needs no re-import of the parent, and fork costs
    single-digit milliseconds against `spawn`'s couple of hundred. Grep is
    called a handful of times per run, so even the slow case would be fine;
    this just keeps the tool's latency honest.

    Falls back to in-process scanning where fork is unavailable, and says so
    rather than silently dropping the guarantee.
    """
    try:
        ctx = multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - non-fork platforms
        logger.warning("fork unavailable; grep runs unbounded in-process")
        return RepoToolkit(root)._scan(files, pattern, glob)

    parent_conn, child_conn = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_scan_worker, args=(child_conn, str(root), files, pattern, glob))
    worker.start()
    child_conn.close()
    try:
        if parent_conn.poll(GREP_DEADLINE_S + GREP_KILL_GRACE_S):
            return parent_conn.recv()
        return (
            f"ERROR: search exceeded {GREP_DEADLINE_S}s and was terminated. "
            "The pattern is almost certainly backtracking catastrophically - "
            "rewrite it so each repetition has exactly one way to match."
        )
    finally:
        parent_conn.close()
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=1.0)
            if worker.is_alive():  # pragma: no cover - terminate ignored
                worker.kill()
        worker.join(timeout=1.0)


class ToolSpec(BaseModel):
    """Transport-neutral tool description (LangGraph and MCP both consume it)."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolError(RuntimeError):
    """Tool failed in a way the agent should see and can recover from."""


class RepoToolkit:
    """Tools bound to one repository root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.exists():
            raise ToolError(f"repository root does not exist: {self.root}")

    # -- safety -------------------------------------------------------

    def _resolve(self, relative: str) -> Path:
        """Resolve a repo-relative path, rejecting anything outside the root.

        Uses `Path.is_relative_to`, NOT `str.startswith`. String prefix
        comparison is the classic broken confinement check: with a root of
        `/srv/repo`, the path `../repo-evil/secrets` resolves to
        `/srv/repo-evil/secrets`, whose string form starts with `/srv/repo`
        and therefore passes. Path-level ancestry has no such hole.
        """
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise ToolError(f"path {relative!r} escapes the repository root - rejected")
        return target

    def _walk(self) -> list[Path]:
        """Every file inside the root, with symlinks that escape excluded.

        `read_file` was confined and `grep`/`list_files`/`dependency_manifest`
        were not, because they walked and read directly. A symlink inside the
        repo pointing anywhere on disk was therefore readable through grep
        while being rejected through read_file - proven with a one-line
        symlink. Confinement belongs in the walk, which is the only place all
        four tools share.
        """
        root = self.root.resolve()
        out: list[Path] = []
        for path in sorted(self.root.rglob("*")):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_file():
                    continue
                # A symlink is only followed if its *target* is also inside
                # the root. Checking the link path alone is not enough.
                if not resolved.is_relative_to(root):
                    continue
            except OSError:  # broken link, permission error
                continue
            out.append(path)
        return out

    # -- tools --------------------------------------------------------

    def list_files(self, pattern: str = "*") -> str:
        """List repo-relative file paths matching a glob."""
        names = [
            str(p.relative_to(self.root))
            for p in self._walk()
            if fnmatch.fnmatch(p.name, pattern)
            or fnmatch.fnmatch(str(p.relative_to(self.root)), pattern)
        ]
        if not names:
            return f"no files matched {pattern!r}"
        return "\n".join(names[:200])[:MAX_OUTPUT_CHARS]

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 200) -> str:
        """Read a slice of a file with line numbers (so findings can cite lines)."""
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        start = max(1, start_line)
        # Capped, because `max_lines` comes from the model. Uncapped, a
        # single call with max_lines=100_000_000 against an 88MB file
        # allocated 665MB to return 8_000 characters - every line held in a
        # list before the output truncation ever ran. Streaming the file was
        # only half the fix; the window has to be bounded too.
        want = min(max(1, max_lines), MAX_READ_LINES)
        # Stream to the requested window rather than materialising the file.
        # The old version called read_text() on the whole file before slicing,
        # so the 40MB-lockfile scenario this module's docstring warns about
        # still allocated 40MB.
        chunk: list[str] = []
        try:
            with target.open(errors="replace") as handle:
                for lineno, line in enumerate(handle, start=1):
                    if lineno < start:
                        continue
                    chunk.append(line.rstrip("\n"))
                    if len(chunk) >= want:
                        break
        except OSError as e:
            raise ToolError(f"cannot read {path}: {e}") from e
        numbered = "\n".join(f"{start + i}: {line}" for i, line in enumerate(chunk))
        return numbered[:MAX_OUTPUT_CHARS] or f"{path} is empty"

    def grep(self, pattern: str, glob: str = "*") -> str:
        """Regex search across the repo. Returns path:line:text matches.

        The pattern comes from the model, which means it is attacker-
        influenceable, which means it is a denial-of-service vector: Python's
        backtracking engine will spin for hours on `(a+)+$` against a 60-char
        line. Three bounds, because no single one is sufficient:

          1. reject quantified groups containing alternation or quantifiers
             up front - this is the load-bearing one, because it is the only
             check that runs *before* the engine starts,
          2. cap the input: pattern length, line length matched, lines
             scanned - backtracking cost grows with input size,
          3. a wall-clock deadline between lines, which bounds total work
             across a large tree.

        Note what (3) cannot do: interrupt a single `regex.search()` already
        spinning. Python has no way to do that without a subprocess. So the
        static guard and the input caps are the real defence, and the
        deadline is a backstop - the reverse of how it first appeared.
        """
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ToolError(f"pattern too long ({len(pattern)} > {MAX_PATTERN_CHARS} chars)")
        offender = find_ambiguous_quantifier(pattern)
        if offender:
            raise ToolError(
                f"pattern fragment {offender!r} is an ambiguous quantified group "
                "(nested quantifier, duplicate or prefix-overlapping alternatives). "
                "These cause catastrophic backtracking - rewrite it so each "
                "repetition has exactly one way to match"
            )
        try:
            re.compile(pattern)
        except re.error as e:
            raise ToolError(f"invalid regex {pattern!r}: {e}") from e

        # Everything from here runs in a child process that can be killed.
        # A wall-clock deadline checked between lines is unenforceable
        # against a single pathological line - nothing in CPython can
        # interrupt a `regex.search()` mid-backtrack - and no syntactic
        # guard is complete (see `find_ambiguous_quantifier`). A process
        # boundary is the only bound that actually holds.
        return _scan_in_subprocess(
            root=self.root,
            files=[str(p) for p in self._walk()],
            pattern=pattern,
            glob=glob,
        )

    def _scan(self, files: list[str], pattern: str, glob: str) -> str:
        """The actual scan. Runs in a child process; see `grep`."""
        regex = re.compile(pattern)
        deadline = time.monotonic() + GREP_DEADLINE_S
        matches: list[str] = []
        scanned = 0
        for name in files:
            path = Path(name)
            rel = str(path.relative_to(self.root))
            if not (fnmatch.fnmatch(path.name, glob) or fnmatch.fnmatch(rel, glob)):
                continue
            try:
                handle = path.open(errors="replace")
            except OSError:
                continue
            # Iterate the handle rather than `read_text().splitlines()`.
            # `MAX_LINES_SCANNED` bounded the *scan* while the read before it
            # was unbounded: an 88MB file cost 368MB of RSS to look at its
            # first 200k lines. Bounding work without bounding memory is half
            # a bound.
            with handle:
                for i, line in enumerate(handle, start=1):
                    scanned += 1
                    # Checked every line, not every 256: the cheaper the check,
                    # the tighter the bound between pathological lines.
                    if time.monotonic() > deadline:
                        return (
                            "\n".join(matches) + f"\n...[search stopped after {GREP_DEADLINE_S}s; "
                            "narrow the pattern or the glob]"
                        ).lstrip("\n")
                    if scanned > MAX_LINES_SCANNED:
                        return (
                            "\n".join(matches) + f"\n...[stopped after {MAX_LINES_SCANNED} lines]"
                        ).lstrip("\n")
                    if regex.search(line[:MAX_LINE_CHARS_MATCHED]):
                        matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(matches) >= MAX_MATCHES:
                            return "\n".join(matches) + f"\n...[capped at {MAX_MATCHES} matches]"
        return "\n".join(matches) if matches else f"no matches for {pattern!r}"

    def dependency_manifest(self) -> str:
        """Find and return dependency manifests (package.json, requirements, pom...)."""
        wanted = {
            "requirements.txt",
            "pyproject.toml",
            "package.json",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "go.mod",
            "Cargo.toml",
        }
        chunks: list[str] = []
        for path in self._walk():
            if path.name in wanted:
                rel = path.relative_to(self.root)
                body = path.read_text(errors="replace")[:2000]
                chunks.append(f"--- {rel} ---\n{body}")
        return "\n\n".join(chunks)[:MAX_OUTPUT_CHARS] if chunks else "no dependency manifests found"

    def repo_stats(self) -> str:
        """Language mix, file count, and presence of CI/tests/docs signals."""
        files = self._walk()
        langs: dict[str, int] = {}
        for p in files:
            langs[p.suffix or "(none)"] = langs.get(p.suffix or "(none)", 0) + 1
        rels = {str(p.relative_to(self.root)) for p in files}
        signals = {
            "has_ci": any(r.startswith(".github/workflows/") for r in rels),
            "has_tests": any("test" in r.lower() for r in rels),
            "has_readme": any(r.lower().startswith("readme") for r in rels),
            "has_dockerfile": any(r.lower().endswith("dockerfile") for r in rels),
            "file_count": len(files),
        }
        return json.dumps(
            {"languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])[:10]), **signals},
            indent=2,
        )

    # -- binding ------------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        """Schemas to advertise to the model.

        Same method name as `FederatedToolkit.specs`, so the binding site
        does not care which kind of toolkit it was handed.
        """
        return tool_specs()

    def as_callables(self) -> dict[str, Callable[..., str]]:
        return {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "grep": self.grep,
            "dependency_manifest": self.dependency_manifest,
            "repo_stats": self.repo_stats,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Dispatch by name with error containment: a tool bug must never
        crash the agent loop, it must become an observation the model can
        react to."""
        fn = self.as_callables().get(name)
        if fn is None:
            return f"ERROR: unknown tool {name!r}"
        try:
            return fn(**arguments)
        except ToolError as e:
            return f"ERROR: {e}"
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"
        except Exception as e:  # noqa: BLE001 - untrusted call surface
            return f"ERROR: {type(e).__name__}: {e}"


def tool_specs() -> list[ToolSpec]:
    """Schemas advertised to models and to MCP clients."""
    return [
        ToolSpec(
            name="list_files",
            description="List repository files matching a glob pattern.",
            parameters={
                "type": "object",
                "properties": {"pattern": {"type": "string", "default": "*"}},
            },
        ),
        ToolSpec(
            name="read_file",
            description="Read a slice of a file, with line numbers for citation.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "default": 1},
                    "max_lines": {"type": "integer", "default": 200},
                },
                "required": ["path"],
            },
        ),
        ToolSpec(
            name="grep",
            description="Regex search across the repository; returns path:line:text.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string", "default": "*"},
                },
                "required": ["pattern"],
            },
        ),
        ToolSpec(
            name="dependency_manifest",
            description="Return dependency manifests found in the repository.",
            parameters={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name="repo_stats",
            description="Language mix, file count, and CI/test/docs signals.",
            parameters={"type": "object", "properties": {}},
        ),
    ]


ATLAS_TOOLS = tool_specs()
