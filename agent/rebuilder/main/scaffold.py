"""Write the rebuild folder as soon as a rebuild is asked for, keep it current, and remove it on Stop.

`<repo>/rebuild/<run_id>/<language>/` is split into sections, each in its own folder:

    README.md            what each section is
    report.json          the rebuild's verdict (written by the rebuild stage)
    program/             the code that runs: the engine plus the fixed contract server and build files
    notes/GAME_NOTES.md  what the agent knows about the game so far, in plain words
    rules/<part>/        one file per candidate rule for that part of the game, named by status
                         (proven-c-0091.py, likely-c-0093.py, wrong-c-0080.py)

The moment someone says "rebuild it in Go", the program section gets a starter engine whose
every behavior is an explicit "unknown" (it never guesses). Notes and rules are rewritten from
the ledger whenever it changes, so the folder grows while the agent learns. The rebuild stage
later replaces the starter engine with the real one. If the run is stopped, `remove` deletes
the whole language folder. No model is called here.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from hypothesizer.main.groups import GROUP_ORDER
from ledger.main.models import Claim, Status
from ledger.main.store import Ledger
from livefeed.main import safety
from llm.main import firewall

from .languages import LanguageConfig
from .location import rebuild_dir

PLAIN = {Status.CONFIRMED: "proven by testing", Status.INFERRED: "likely, not yet proven",
         Status.OBSERVED: "seen in play", Status.OPEN: "still unknown", Status.REFUTED: "shown to be wrong"}
FILE_WORD = {Status.CONFIRMED: "proven", Status.INFERRED: "likely", Status.OBSERVED: "seen",
             Status.OPEN: "open", Status.REFUTED: "wrong"}
_ORDER = {Status.CONFIRMED: 0, Status.INFERRED: 1, Status.OBSERVED: 2, Status.REFUTED: 3, Status.OPEN: 4}
STARTER_MARK = "STARTER ENGINE"
PROGRAM, NOTES, RULES = "program", "notes", "rules"

GO_STARTER = '''package main

// STARTER ENGINE, written by a template the moment the rebuild was asked for.
// Every behavior is an explicit unknown: the agent has not proven any rule yet.
// The rebuild stage replaces this file with an engine written from Confirmed rules only.

var DeckCounts map[string]int

var UnknownBehaviors = []string{{
{unknowns}
}}

func NewGame(players int, deck []string) any {{
	Unknown("how a game is set up")
	return nil
}}

func LegalActions(state any) []map[string]any {{
	Unknown("which actions are legal")
	return nil
}}

func Act(state any, action map[string]any) (any, []map[string]any) {{
	Unknown("what an action does")
	return nil, nil
}}

func View(state any) map[string]any {{
	Unknown("what the public view shows")
	return nil
}}
'''

JS_STARTER = """'use strict';
// STARTER ENGINE, written by a template the moment the rebuild was asked for.
// Every behavior is an explicit unknown: the agent has not proven any rule yet.
// The rebuild stage replaces this file with an engine written from Confirmed rules only.

function unknown(what) {{
  throw Object.assign(new Error('unknown behavior: ' + what), {{ kind: 'unknown_behavior' }});
}}

exports.UNKNOWN_BEHAVIORS = [
{unknowns}
];

exports.newGame = () => unknown('how a game is set up');
exports.legalActions = () => unknown('which actions are legal');
exports.act = () => unknown('what an action does');
exports.view = () => unknown('what the public view shows');
"""

README = """# Pressure, rebuilt in {name}

Run `{run_id}`. Written by the Strata agent while it studies the game.

| Section | What is in it |
|---|---|
| `program/` | The code that runs. `{engine}` is the game engine; the other files are the fixed contract server and build files, not written by a model. Until the rebuild stage runs, `{engine}` is a starter where every behavior is an explicit unknown. |
| `notes/GAME_NOTES.md` | What the agent knows about the game so far, in plain words, with trace IDs. |
| `rules/<part>/` | One file per candidate rule for each part of the game. The name says its status: `proven-...` (proven by testing), `likely-...` (fits every recorded game, not yet proven), `wrong-...` (the games contradict it). Only proven rules are used to build the engine. |
| `report.json` | The checker's verdict on the rebuild, once it has been checked. |

Notes and rules are rewritten from the evidence every time it changes. If the run is stopped, this whole folder is removed.
"""


# ---------- small helpers ----------
def _write(path: Path, text: str) -> bool:
    """Atomic write that skips unchanged files, so the UI only sees real changes."""
    if path.exists() and path.read_text(errors="ignore") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return True


def _safe(text: str) -> bool:
    """Nothing restricted and no secret. (Rule code is model output on purpose here, so the
    feed's "no raw model output" check does not apply to the rule files.)"""
    try:
        firewall.check(text)
    except firewall.PromptFirewallError:
        return False
    return not safety.has_secret(text)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", text.lower())


def _cite(claim: Claim, limit: int = 2) -> str:
    return f" (trace {', '.join(claim.evidence[:limit])})" if claim.evidence else ""


def _sentence(text: str) -> str:
    return text.strip().rstrip(". ")


# ---------- locations ----------
def folder_for(run_dir: Path, language: LanguageConfig) -> Path:
    """`<repo>/rebuild/<run_id>/<language>/`: the whole rebuild for one language."""
    return rebuild_dir(run_dir) / language.key


def program_dir(run_dir: Path, language: LanguageConfig) -> Path:
    """Where the code that runs lives. The rebuild stage writes its engine here."""
    return folder_for(run_dir, language) / PROGRAM


# ---------- formatting ----------
def format_code(directory: Path, language: LanguageConfig) -> None:
    """Tidy the program's source with the language's own formatter, if it is installed.
    A formatter that fails changes nothing: the build reports real errors."""
    if language.key == "go" and shutil.which("gofmt"):
        files = [str(p) for p in sorted(Path(directory).glob("*.go"))]
        if files:
            subprocess.run(["gofmt", "-w", *files], capture_output=True, timeout=20)


def _starter(language: LanguageConfig) -> str:
    if language.key == "go":
        return GO_STARTER.format(unknowns="\n".join(f'\t"{g}",' for g in GROUP_ORDER))
    return JS_STARTER.format(unknowns="\n".join(f"  '{g}'," for g in GROUP_ORDER))


# ---------- the folder ----------
def scaffold(run_dir: Path, language: LanguageConfig) -> Path:
    """Create the sections, the fixed files and a starter engine. Never overwrites a real engine."""
    folder = folder_for(run_dir, language)
    program = folder / PROGRAM
    if not all((program / name).exists() for name in language.support_files):
        language.write_support(program)          # once; rewriting would look like a change in the UI
    engine = program / language.engine_file
    if not engine.exists():
        _write(engine, _starter(language))
        format_code(program, language)
    _write(folder / "README.md", README.format(name=language.name, run_id=Path(run_dir).name,
                                               engine=language.engine_file))
    notes = folder / NOTES / "GAME_NOTES.md"
    if not notes.exists():
        _write(notes, "# Pressure: what the agent knows so far\n\nNothing yet. The agent is reading the manual "
                      "and the partial code. This file fills in as it learns.\n")
    (folder / RULES).mkdir(parents=True, exist_ok=True)
    return folder


def remove(run_dir: Path, language: LanguageConfig) -> bool:
    """The run was stopped: delete this language's rebuild. Returns True if there was one."""
    folder = folder_for(run_dir, language)
    if not folder.exists():
        return False
    shutil.rmtree(folder)
    parent = folder.parent
    try:
        parent.rmdir()                           # the run's folder too, once no language is left
    except OSError:
        pass
    return True


def is_starter(folder: Path, language: LanguageConfig) -> bool:
    engine = Path(folder) / PROGRAM / language.engine_file
    return engine.exists() and STARTER_MARK in engine.read_text(errors="ignore")[:400]


# ---------- notes ----------
def notes_text(ledger: Ledger) -> str:
    claims = [c for c in ledger.all_claims() if _safe(c.text)]
    rules = [c for c in claims if (c.rule or {}).get("type") == "executable"]
    facts = [c for c in claims if c not in rules]
    counts = {s: sum(c.status is s for c in claims) for s in Status}
    out = ["# Pressure: what the agent knows so far", "",
           f"{len(claims)} things noted: {counts[Status.CONFIRMED]} proven, {counts[Status.INFERRED]} likely, "
           f"{counts[Status.REFUTED]} shown wrong, {counts[Status.OPEN]} still unknown.",
           "This file is rewritten from the evidence every time it changes.", ""]

    findings = [f for f in ledger.findings() if f.trace_ids and _safe(f.artifact_said + f.traces_said)]
    if findings:
        out += ["## Where the manual or the code was wrong", ""]
        out += [f"- The {f.artifact} said \"{_sentence(f.artifact_said)}\". The games show: "
                f"{_sentence(f.traces_said)} (trace {f.trace_ids[0]})." for f in findings[:12]]
        out.append("")

    known = sorted((c for c in facts if c.status in (Status.CONFIRMED, Status.INFERRED, Status.OBSERVED)),
                   key=lambda c: (_ORDER[c.status], c.id))
    if known:
        out += ["## What the games show", "", "| What | How sure | Evidence |", "|---|---|---|"]
        out += [f"| {_sentence(c.text).replace('|', '/')} | {PLAIN[c.status]} | {', '.join(c.evidence[:2])} |"
                for c in known[:30]]
        out.append("")

    if rules:
        out += ["## Rules being tested", "", "One folder per part of the game in `rules/`.", "",
                "| Part of the game | Proven | Likely | Shown wrong |", "|---|---|---|---|"]
        for group in GROUP_ORDER:
            mine = [r for r in rules if r.rule.get("group") == group]
            if mine:
                out.append(f"| {group} | {sum(r.status is Status.CONFIRMED for r in mine)} "
                           f"| {sum(r.status is Status.INFERRED for r in mine)} "
                           f"| {sum(r.status is Status.REFUTED for r in mine)} |")
        out.append("")

    unknown = [c for c in facts if c.status is Status.OPEN]
    if unknown:
        out += ["## Still unknown", ""] + [f"- {_sentence(c.text)}" for c in unknown[:20]]
        if len(unknown) > 20:
            out.append(f"- ... and {len(unknown) - 20} more")
        out.append("")
    return "\n".join(out)


# ---------- rules ----------
def rule_file_name(claim: Claim) -> str:
    return f"{FILE_WORD[claim.status]}-{claim.id}.py"


def rule_file_text(claim: Claim) -> str:
    group = claim.rule.get("group", "?")
    header = [f"# Rule {claim.id} about '{group}' in Pressure.", "#",
              *[f"# {line}" for line in _wrap(claim.text, 92)], "#",
              f"# Status: {PLAIN[claim.status]}.",
              f"# Evidence: {', '.join(claim.evidence[:5]) or 'none yet'}.",
              "# Written by the model and judged only against recorded games.",
              "# Only a rule that is proven by testing is used to build the engine."]
    if claim.status is Status.REFUTED:
        header.append("# Kept for the record: the games contradict it.")
    return "\n".join(header) + "\n\n" + claim.rule.get("code", "").strip() + "\n"


def _wrap(text: str, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + ([line] if line else [])


def _sync_rules(rules_dir: Path, ledger: Ledger) -> list[str]:
    """One file per candidate rule. A rule whose status changes is renamed; stale files go."""
    changed, wanted = [], set()
    for claim in ledger.all_claims():
        rule = claim.rule or {}
        if rule.get("type") != "executable" or not rule.get("group") or not _safe(rule.get("code", "")):
            continue
        rel = Path(_slug(rule["group"])) / rule_file_name(claim)
        wanted.add(rel)
        if _write(rules_dir / rel, rule_file_text(claim)):
            changed.append(f"rules/{rel.as_posix()}")
    for path in sorted(rules_dir.glob("*/*.py")):
        rel = path.relative_to(rules_dir)
        if rel not in wanted:
            path.unlink()
            changed.append(f"rules/{rel.as_posix()} (removed)")
    for sub in rules_dir.iterdir():
        if sub.is_dir() and not any(sub.iterdir()):
            sub.rmdir()
    return changed


def refresh(run_dir: Path, language: LanguageConfig) -> list[str]:
    """Rewrite notes and rules from the ledger. Returns the files that changed."""
    run_dir = Path(run_dir)
    folder = scaffold(run_dir, language)
    try:
        ledger = Ledger(run_dir / "ledger.jsonl")
    except Exception:
        return []                                   # mid-write or unreadable: try again next time
    changed = []
    if _write(folder / NOTES / "GAME_NOTES.md", notes_text(ledger)):
        changed.append("notes/GAME_NOTES.md")
    return changed + _sync_rules(folder / RULES, ledger)
