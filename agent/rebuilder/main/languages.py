"""T14.3: target languages for the rebuild.

A language is data: where the engine and the fixed contract server go, how to check
and run them, and what to tell the model. Output goes to `<repo>/rebuild/<run_id>/<key>/` (see location.py).
JavaScript and Go ship first. A language whose toolchain is missing is reported, never
swapped for another one.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent


class LanguageError(RuntimeError):
    pass


class UnsupportedLanguage(LanguageError):
    pass


class ToolchainMissing(LanguageError):
    pass


JS_API = '''\
engine.js is a CommonJS module that exports exactly:
  newGame(players, deck)  -> state     // players 2-4; deck = array of card names, dealt round-robin
  legalActions(state)     -> array of actions, SORTED by action type then by card name (both
                             alphabetical, so draw < pass < play, and play cards a..z)
  act(state, action)      -> { state, events }   // action is one of legalActions(state); apply it
  view(state)             -> the public view object (keys exactly as in the Python rules)
  DECK_COUNTS             -> optional {cardName: count} of a full deck
  UNKNOWN_BEHAVIORS       -> array of strings: behaviors you could not implement from the rules
A helper `unknown(description)` should throw `Object.assign(new Error('unknown behavior: ' + description), {kind: 'unknown_behavior'})`.
Actions: {"type":"play","card":NAME} | {"type":"draw"} | {"type":"pass"}  (a play always has a card;
draw and pass have NO card key). Events are plain objects exactly as the rules emit them, in order.
Mutating the state in place and returning it is fine. JSON key order does not matter; values do.
'''

GO_API = '''\
engine.go is the model-written half of a Go program (package main, Go 1.21, standard library only).
A fixed server.go in the same package already reads the JSON-lines contract; do NOT write main().
engine.go must define exactly:
  func NewGame(players int, deck []string) any                       // players 2-4; deck dealt round-robin
  func LegalActions(state any) []map[string]any                      // SORTED by "type" then "card" (alphabetical,
                                                                     // so draw < pass < play, and play cards a..z)
  func Act(state any, action map[string]any) (any, []map[string]any) // new state, events in order
  func View(state any) map[string]any                                // public view, keys exactly as in the Python rules
  var DeckCounts map[string]int                                      // card -> count of a full deck (may be nil)
  var UnknownBehaviors []string                                      // behaviors you could not implement from the rules
server.go provides `Unknown(description string)`: call it for any behavior the rules do not establish; it panics
with an explicit unknown-behavior error. Never guess.
Actions: {"type":"play","card":NAME} | {"type":"draw"} | {"type":"pass"} (a play always has a "card" key; draw and
pass have NO card key). Events are map[string]any exactly as the rules emit them, in order. Use ints for numbers and
nil for null. Do not import anything unused.
'''


def _node_check(path: Path) -> str | None:
    done = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=30)
    return None if done.returncode == 0 else done.stderr.strip()[:600]


def _js_unknown(directory: Path) -> list[str]:
    script = ("const e=require(process.argv[1]);"
              "process.stdout.write(JSON.stringify(e.UNKNOWN_BEHAVIORS||[]))")
    done = subprocess.run(["node", "-e", script, str(directory / "engine.js")],
                          capture_output=True, text=True, timeout=30)
    try:
        return [str(x) for x in json.loads(done.stdout)]
    except json.JSONDecodeError:
        return []


def _go_env() -> dict[str, str]:
    import os
    env = dict(os.environ)
    env.setdefault("GOFLAGS", "-mod=mod")
    env.setdefault("GOCACHE", str(Path(env.get("TMPDIR", "/tmp")) / "strata-gocache"))
    for key in ("META_API_KEY", "SUBQ_API_KEY"):          # compiling model code: no keys in reach
        env.pop(key, None)
    return env


def _go_check(directory: Path) -> str | None:
    if shutil.which("gofmt"):          # tidy the model's code first; a formatter failure changes nothing
        subprocess.run(["gofmt", "-w", *(str(p) for p in sorted(Path(directory).glob("*.go")))],
                       capture_output=True, timeout=20)
    done = subprocess.run(["go", "build", "-o", "server", "."], cwd=directory, capture_output=True,
                          text=True, timeout=90, env=_go_env())
    return None if done.returncode == 0 else (done.stderr or done.stdout).strip()[:900]


def _go_unknown(directory: Path) -> list[str]:
    done = subprocess.run([str(directory / "server"), "--unknown-behaviors"], capture_output=True,
                          text=True, timeout=30)
    try:
        return [str(x) for x in json.loads(done.stdout)]
    except json.JSONDecodeError:
        return []


@dataclass(frozen=True)
class LanguageConfig:
    key: str                                   # folder name under rebuild/
    name: str                                  # as shown to people
    aliases: tuple[str, ...]
    toolchain: tuple[str, ...]                 # executables that must exist
    fence: str                                 # code-fence tag the model should use
    engine_file: str
    support_files: dict[str, str]              # file name -> fixed template (not model-written)
    build_command: tuple[str, ...]             # {dir} is the output folder
    run_command: tuple[str, ...]
    file_layout: tuple[str, ...]
    engine_api: str
    prompt_hints: str
    check: Callable[[Path], str | None] = field(repr=False, default=_node_check)
    unknown: Callable[[Path], list[str]] = field(repr=False, default=_js_unknown)
    install_hint: str = ""

    def command(self, directory: Path) -> list[str]:
        return [part.format(dir=str(directory)) for part in self.run_command]

    def build(self, directory: Path) -> list[str]:
        return [part.format(dir=str(directory)) for part in self.build_command]

    def missing_tools(self, which: Callable[[str], str | None] = shutil.which) -> list[str]:
        return [tool for tool in self.toolchain if which(tool) is None]

    def require_toolchain(self, which: Callable[[str], str | None] = shutil.which) -> None:
        missing = self.missing_tools(which)
        if missing:
            raise ToolchainMissing(
                f"I can't rebuild in {self.name}: {', '.join(missing)} is not installed on this machine. "
                f"{self.install_hint}I did not switch to another language.".replace("  ", " "))

    def write(self, directory: Path, engine_code: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / self.engine_file).write_text(engine_code)
        return self.write_support(directory)

    def write_support(self, directory: Path) -> Path:
        """The fixed files only (contract server, go.mod): never model-written."""
        directory.mkdir(parents=True, exist_ok=True)
        for target, template in self.support_files.items():
            if template.startswith("="):                 # literal text, not a file to copy
                (directory / target).write_text(template[1:])
            else:
                shutil.copyfile(HERE / template, directory / target)
        return directory

    def extract(self, reply: str) -> str:
        blocks = re.findall(r"```[A-Za-z0-9_+-]*[ \t]*\n(.*?)```", reply, re.DOTALL)
        code = (max(blocks, key=len) if blocks else reply).strip()
        if self.key == "js" and "module.exports" not in code and "exports." not in code:
            raise LanguageError("the reply did not contain a CommonJS module (no module.exports)")
        if self.key == "go" and not re.search(r"^package\s+main\b", code, re.M):
            raise LanguageError("the reply did not contain a Go file in package main")
        return code + "\n"


JAVASCRIPT = LanguageConfig(
    key="js", name="JavaScript", aliases=("js", "javascript", "node", "nodejs", "node.js", "ecmascript"),
    toolchain=("node",), fence="javascript", engine_file="engine.js",
    support_files={"server.js": "server.js"},
    build_command=("node", "--check", "{dir}/engine.js"), run_command=("node", "{dir}/server.js"),
    file_layout=("engine.js (model-written)", "server.js (fixed contract server)"),
    engine_api=JS_API, prompt_hints="Node 20, CommonJS, no dependencies.",
    check=lambda d: _node_check(d / "engine.js"), unknown=_js_unknown,
    install_hint="Install Node.js 20 or newer. ")

GO = LanguageConfig(
    key="go", name="Go", aliases=("go", "golang"),
    toolchain=("go",), fence="go", engine_file="engine.go",
    support_files={"server.go": "server.go", "go.mod": "=module pressure\n\ngo 1.21\n"},
    build_command=("go", "build", "-o", "{dir}/server", "."), run_command=("{dir}/server",),
    file_layout=("engine.go (model-written)", "server.go (fixed contract server)", "go.mod", "server (built)"),
    engine_api=GO_API, prompt_hints="Go 1.21, standard library only, package main, gofmt style.",
    check=_go_check, unknown=_go_unknown,
    install_hint="Install Go 1.21 or newer (https://go.dev/dl). ")

LANGUAGES: dict[str, LanguageConfig] = {JAVASCRIPT.key: JAVASCRIPT, GO.key: GO}
DEFAULT_LANGUAGE = JAVASCRIPT.key


def resolve(name: str | None) -> LanguageConfig:
    """Turn what a person typed ("Go", "golang", "js") into a config; JavaScript if none was named."""
    if name is None or not str(name).strip():
        return LANGUAGES[DEFAULT_LANGUAGE]
    wanted = re.sub(r"[^a-z0-9.+#]", "", str(name).lower())
    for config in LANGUAGES.values():
        if wanted == config.key or wanted in config.aliases:
            return config
    supported = ", ".join(c.name for c in LANGUAGES.values())
    raise UnsupportedLanguage(f"I can rebuild in {supported} so far, not {str(name).strip()[:30]}.")


# Languages people may name that we cannot build in yet: said plainly, never silently swapped.
_OTHER_LANGUAGES = {"rust", "python", "java", "c", "c++", "cpp", "c#", "csharp", "ruby", "php", "swift",
                    "kotlin", "typescript", "ts", "scala", "haskell", "lua", "perl", "dart", "elixir", "zig",
                    "ocaml", "clojure", "r", "julia", "fortran", "cobol", "bash"}
_LANG_PHRASE = re.compile(r"\b(?:in|into|to|using|with|as)\s+(?:pure\s+|plain\s+|vanilla\s+)?([A-Za-z][\w.+#]*)", re.I)


def language_in(text: str) -> str | None:
    """The programming language a one-line request names ("rebuild it in Go"), as typed, or None.

    Only a word after in/to/using/with counts, and only if it is a language we know or one we
    know we cannot build. That keeps "go ahead" or "in the browser" from being read as a language.
    """
    for match in _LANG_PHRASE.finditer(text):
        word = match.group(1).strip(".").lower()
        known = {a for c in LANGUAGES.values() for a in (c.key, *c.aliases)}
        if word in known or word in _OTHER_LANGUAGES:
            return match.group(1).strip(".")
    return None
