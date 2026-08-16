"""Modes: the prompt template and the response shape, together — spec §10.

`--mode` swaps two things that must never drift apart: what we ask for, and
what we say we will accept. Where a provider enforces the schema, *the schema
wins* — a flatter schema than the template silently drops fields, and we lost
`why` and `confidence` off every cited file exactly that way (findings, trap
3). So both live in one object here, and a test asserts every required schema
field is named in the instructions.

`triage` is the default for `ask` and the reason the tool is worth building:
when the answer is "which files", prose is the wrong interface. Three things
its field set encodes, each from an observed failure:

- **`missing_context` is surfaced, not buried.** Every wrong answer in the
  dogfooding runs named the relevant file as absent. It is the built-in
  confidence check: a confident claim about a file the model never read is a
  guess wearing a score.
- **File-level attribution is reliable; line numbers are not.** Three runs
  cited plausible, wrong line numbers while naming exactly the right files at
  0.95 confidence. So the template asks for files and forbids line numbers as
  citations.
- **Permission to answer "none"** belongs in the template, not in every
  question typed by hand. Open-ended "review this" otherwise returns a
  restatement of the design.

Templates stay user-editable: a file at `<config>/modes/<name>.md` replaces
the built-in instructions for that mode, and the schema goes with it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from kopipasta.config import get_global_profile_path
from kopipasta.core.errors import UsageError

_NO_LINE_NUMBERS = (
    "Cite files, never line numbers: line numbers from a model reading a "
    "payload are unreliable and a wrong one reads as a citation."
)
_SAY_NONE = (
    "If a category is genuinely clean, say so explicitly rather than "
    "inventing something to fill it."
)
_ADMIT_MISSING = (
    "List in missing_context every file you needed and did not get. A "
    "confident claim about a file you could not read is a guess."
)


def _json_block(example: str) -> str:
    return "\n".join(
        [
            "## Required Output Format",
            "",
            "Return ONLY a single JSON object, in a ```json code block, with no prose",
            "before or after it:",
            "",
            "```json",
            example,
            "```",
        ]
    )


TRIAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant_files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "why": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["path", "why", "confidence"],
            },
        },
        "hypothesis": {"type": "string"},
        "missing_context": {"type": "array", "items": {"type": "string"}},
        "suggested_selection": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "relevant_files",
        "hypothesis",
        "missing_context",
        "suggested_selection",
    ],
}

REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "severity": {"type": "string"},
                    "what": {"type": "string"},
                    "why": {"type": "string"},
                    "fix": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["path", "severity", "what", "why", "fix", "confidence"],
            },
        },
        "verdict": {"type": "string"},
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "verdict", "missing_context"],
}

PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "what": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "risk": {"type": "string"},
                },
                "required": ["what", "files", "risk"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["steps", "open_questions", "missing_context"],
}


@dataclass
class Mode:
    name: str
    instructions: str
    schema: Optional[Dict[str, Any]] = None
    #: Renders the parsed result for a human. Only used when --json is off.
    summary: Optional[Callable[[Dict[str, Any]], str]] = None
    #: Answers arrive as code blocks to be applied, not as an answer to read.
    expects_code: bool = False
    aliases: List[str] = field(default_factory=list)

    @property
    def structured(self) -> bool:
        return self.schema is not None


def _triage_summary(d: Dict[str, Any]) -> str:
    out: List[str] = []
    hypothesis = str(d.get("hypothesis") or "").strip()
    if hypothesis:
        out += [hypothesis, ""]
    # Built first, then headed: the schema guarantees the key exists, not that
    # every item inside it is the shape it promised, and a heading over
    # nothing reads as "it found none" rather than "it returned junk".
    cited = []
    for f in d.get("relevant_files") or []:
        if not isinstance(f, dict):
            continue
        conf = f.get("confidence")
        conf_s = f"{float(conf):.2f}" if isinstance(conf, (int, float)) else "  ? "
        cited.append(f"  {conf_s}  {f.get('path', '?')}  — {f.get('why', '')}")
    if cited:
        out += ["relevant files:", *cited, ""]
    missing = d.get("missing_context") or []
    if missing:
        out += ["missing context (the answer did not see these):"]
        out += [f"  {m}" for m in missing]
        out.append("")
    suggested = d.get("suggested_selection") or []
    if suggested:
        out += ["suggested selection (feed back with --from-file):"]
        out += [f"  {s}" for s in suggested]
    return "\n".join(out).rstrip()


def _review_summary(d: Dict[str, Any]) -> str:
    out: List[str] = []
    verdict = str(d.get("verdict") or "").strip()
    if verdict:
        out += [verdict, ""]
    for f in d.get("findings") or []:
        if not isinstance(f, dict):
            continue
        out.append(
            f"[{f.get('severity', '?')}] {f.get('path', '?')}: {f.get('what', '')}"
        )
        if f.get("why"):
            out.append(f"    why: {f['why']}")
        if f.get("fix"):
            out.append(f"    fix: {f['fix']}")
    missing = d.get("missing_context") or []
    if missing:
        out += ["", "missing context:"] + [f"  {m}" for m in missing]
    return "\n".join(out).rstrip()


def _plan_summary(d: Dict[str, Any]) -> str:
    out: List[str] = []
    for i, step in enumerate(d.get("steps") or [], start=1):
        if not isinstance(step, dict):
            continue
        out.append(f"{i}. {step.get('what', '')}")
        if step.get("files"):
            out.append(f"    files: {', '.join(step['files'])}")
        if step.get("risk"):
            out.append(f"    risk: {step['risk']}")
    for label, key in (
        ("open questions", "open_questions"),
        ("missing context", "missing_context"),
    ):
        items = d.get(key) or []
        if items:
            out += ["", f"{label}:"] + [f"  {x}" for x in items]
    return "\n".join(out).rstrip()


TRIAGE = Mode(
    name="triage",
    schema=TRIAGE_SCHEMA,
    summary=_triage_summary,
    instructions="\n".join(
        [
            "You are a triage oracle. You have been given a large slice of a codebase and",
            "one question. Answer with *which files matter and why*, not with a lecture.",
            "",
            f"- {_ADMIT_MISSING}",
            f"- {_NO_LINE_NUMBERS}",
            f"- {_SAY_NONE} An empty relevant_files list is a valid answer.",
            "- confidence is 0.0-1.0 and must reflect what you actually read: files shown",
            "  only as a path or a skeleton support a low score, not a high one.",
            "- suggested_selection is the minimal file set a follow-up call should load in",
            "  full to act on your hypothesis.",
            "",
            _json_block(
                '{"relevant_files": [{"path": "...", "why": "...", "confidence": 0.0}],\n'
                ' "hypothesis": "...",\n'
                ' "missing_context": ["..."],\n'
                ' "suggested_selection": ["..."]}'
            ),
        ]
    ),
)

REVIEW = Mode(
    name="review",
    schema=REVIEW_SCHEMA,
    summary=_review_summary,
    instructions="\n".join(
        [
            "Review the code above against the question. Report concrete defects only:",
            "something that is wrong, with the file it is wrong in and the change that",
            "fixes it. Do not restate the design.",
            "",
            "- severity is one of: critical, major, minor, nit.",
            f"- {_NO_LINE_NUMBERS}",
            f"- {_SAY_NONE} An empty findings list is the right answer for clean code.",
            f"- {_ADMIT_MISSING}",
            "- verdict is one sentence: is this safe to ship, and if not, why not.",
            "",
            _json_block(
                '{"findings": [{"path": "...", "severity": "major", "what": "...",\n'
                '               "why": "...", "fix": "...", "confidence": 0.0}],\n'
                ' "verdict": "...",\n'
                ' "missing_context": ["..."]}'
            ),
        ]
    ),
)

PLAN = Mode(
    name="plan",
    schema=PLAN_SCHEMA,
    summary=_plan_summary,
    instructions="\n".join(
        [
            "Produce an ordered plan for the task above. Each step must name the files it",
            "touches and the risk it carries. Plan the change; do not write it.",
            "",
            f"- {_ADMIT_MISSING}",
            f"- {_NO_LINE_NUMBERS}",
            "- open_questions is for decisions you cannot make from the code alone. Put",
            "  them there instead of guessing inside a step.",
            "",
            _json_block(
                '{"steps": [{"what": "...", "files": ["..."], "risk": "..."}],\n'
                ' "open_questions": ["..."],\n'
                ' "missing_context": ["..."]}'
            ),
        ]
    ),
)

EXPLAIN = Mode(
    name="explain",
    instructions="\n".join(
        [
            "Explain the code above as it relates to the question. Prose is the artifact",
            "here, so write it for someone who has to change this code tomorrow.",
            "",
            f"- {_NO_LINE_NUMBERS}",
            "- Name the files you are describing, so the reader can follow along.",
            "- If something in the payload contradicts your explanation, say so rather",
            "  than smoothing it over.",
            "- If a file you would need is missing, say which, at the top.",
        ]
    ),
)

ANSWER = Mode(
    name="answer",
    aliases=["default", "prose"],
    instructions="\n".join(
        [
            "Answer the question above directly, from the code you were given.",
            "",
            f"- {_NO_LINE_NUMBERS}",
            "- If the payload does not contain what you need, say which file is missing",
            "  instead of guessing. That is a useful answer; a confident invention is not.",
        ]
    ),
)

PATCH = Mode(
    name="patch",
    expects_code=True,
    instructions="\n".join(
        [
            "## Code Output Rules (CRITICAL)",
            "",
            "A local tool applies your code blocks automatically. There is no human in",
            "this loop to fix a malformed one.",
            "",
            "- Put every change in a fenced code block: ``` on its own line before and",
            "  after. The fence is how the tool finds where a change starts and ends;",
            "  an unfenced block is skipped in silence, however correct it is.",
            "- Every code block starts with a path comment: `# FILE: path/to/file.py`",
            "- To EDIT an existing file, use a Search/Replace block:",
            "  `<<<<<<< SEARCH` / exact existing lines / `=======` / new lines /",
            "  `>>>>>>> REPLACE`",
            "- To CREATE a file, give its FULL content.",
            "- Only files under '## Active Workspace (Editable)' may be modified. A patch",
            "  against any other file is rejected before it is applied.",
            "- Output the code blocks and a one-paragraph summary. Do not ask questions:",
            "  nobody can answer them.",
        ]
    ),
)

_BUILTIN = (TRIAGE, REVIEW, PLAN, EXPLAIN, ANSWER, PATCH)

MODES: Dict[str, Mode] = {}
for _m in _BUILTIN:
    MODES[_m.name] = _m
    for _alias in _m.aliases:
        MODES[_alias] = _m

#: What `--mode` accepts, in a stable order for --help.
MODE_NAMES = [m.name for m in _BUILTIN]

DEFAULT_MODE = TRIAGE.name


def modes_dir() -> str:
    """Beside prompt_template.j2 and config.toml, edited the same way."""
    return os.path.join(str(get_global_profile_path().parent), "modes")


def get(name: str) -> Mode:
    """Resolve a mode name, applying a user override file if one exists."""
    key = (name or DEFAULT_MODE).strip().lower()
    mode = MODES.get(key)
    if mode is None:
        raise UsageError(
            f"unknown mode {name!r}.",
            detail=f"Known modes: {', '.join(MODE_NAMES)}.",
            hint="--mode triage      # which files matter, as JSON\n"
            "--mode answer      # plain prose",
        )
    override = os.path.join(modes_dir(), f"{mode.name}.md")
    if os.path.isfile(override):
        try:
            with open(override, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            return mode
        if text:
            # The schema travels with the built-in mode: an edited template
            # that no longer matches it would be silently overruled by the
            # provider, so overriding the words is supported and overriding
            # the shape is not.
            return Mode(
                name=mode.name,
                instructions=text,
                schema=mode.schema,
                summary=mode.summary,
                expects_code=mode.expects_code,
            )
    return mode
