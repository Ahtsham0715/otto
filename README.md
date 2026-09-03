# Otto

A local-first assistant for macOS that you talk to. Press a hotkey, say what you want,
and Otto does it on your Mac — then checks that it actually happened and tells you, out
loud.

It is deliberately small. On the machine it was built for — a 2019 Intel Core i9
MacBook Pro with no usable GPU — a heavy assistant is worse than no assistant, because
you turn it off. Otto idles at **24 MB and 0% CPU** and starts in **under a tenth of a
second**.

```zsh
./setup.sh     # once
./run.sh       # 🎙 appears in the menu bar
```

**It works before you install any AI model.** "Open Safari", "create a folder called
Test on my Desktop", "remember that my projects live in ~/Projects" all run with no
model, no network and no tokens.

---

## What it does

| You say | What happens |
| --- | --- |
| "Open Safari" | Safari opens — and Otto asks the Mac which app is frontmost before claiming it worked |
| "Create a folder called Test on my Desktop" | You get one approval prompt, then the folder — verified to exist |
| "Read ~/Documents/notes.md and summarise it" | A summary, spoken |
| "Remember that my projects live in ~/Projects" | Stored, and used later without being asked again |
| "Open my project and run the tests" | Editor opens, the right test command runs, you hear the result |

---

## How it is built

```
hotkey / text box  →  Otto.handle_utterance()  →  fast path or planner
                                                        ↓
                                              tool registry (the only door)
                                validate → permission → audit → run → VERIFY
                                                        ↓
                                     macOS bridge · files · commands · memory
```

Five ideas do most of the work.

**Structured state, never prose.** `Task`, `Subtask`, `ToolCall`, `AgentMessage`,
`Artifact`, `Approval` and a timeline are real typed objects with a real transition
table. A model *proposes* a plan as JSON; code validates every step against the actual
agent roster and tool registry and rejects the whole plan on one bad step. Otto never
parses orchestration state back out of free text.

**One dispatch path.** Everything goes through `ToolRegistry.dispatch`, which always
does the same seven things in the same order: resolve → validate arguments → check
permission and the agent's ceiling → audit → execute → **verify** → record. Agents hold
tool *names*, never handlers, so there is no way in halfway.

**Verification is mandatory.** Every tool declares a verifier that re-reads real state
afterwards. Did the folder appear? Is that app actually frontmost? Does the text field
hold what we typed? A failed verification marks the call FAILED no matter what the
handler or the model said. A tool cannot be registered without one.

**Agents are configuration.** Supervisor, Planner, Mac, Files, Coder, Research, Memory,
QA, Reviewer are records — id, role, instructions, model tier, tools, permission
ceiling, memory scope, step budget. Adding a specialist is adding a record.

**The macOS boundary is an interface.** One `MacBridge` with an `osascript`
implementation and a fake. That is what lets a 443-test suite run on Linux — and it is
why this README is careful, further down, about what has and has not been run on a real
Mac.

---

## Security

Assume the model is hostile, because a README it just read may be. Every control below
is in code; none of it is prompt wording.

- **Three permission levels.** SAFE runs (open an app, read a file). CONFIRM asks
  (write a file, run a command, click something). ALWAYS_CONFIRM always asks (delete,
  anything irreversible). Each agent also has a **ceiling** it cannot exceed *even if
  you approve*. The Research agent — the one that reads untrusted web pages — is pinned
  to SAFE, so a prompt injection cannot reach a writing tool through it.
- **No shell, ever.** Commands are argv lists run with `shell=False` against a binary
  allowlist. Arguments are screened too, because an allowlist that only checks
  `argv[0]` is bypassable: `find -exec`, `git -c credential.helper=`,
  `git --upload-pack=`, `ext::` URLs, `rg --pre`, `make -f`, `python -c` and
  `npm --node-options=` all execute code through an *allowed* program. Each has a test.
- **AppleScript injection.** Untrusted values reach `osascript` only as `on run argv`
  runtime arguments, never interpolated into script source. A payload like
  `" & (do shell script "id") & "` is typed as literal text; there is a test that
  asserts it never appears in the compiled script.
- **Filesystem scope.** Desktop, Documents, Downloads, ~/Projects — deliberately not
  `$HOME`. Paths are resolved (following symlinks) and then contained, so traversal,
  absolute escape and symlink escape are one check. Resolved paths are then screened
  against a credential denylist: `.ssh`, `.aws`, `.gnupg`, keychains, `.env*`, `*.pem`,
  `id_rsa*`, `.npmrc`, `credentials`, `.netrc`.
- **Deletes go to the Trash.** There is no `unlink` in the codebase.
- **No click-at-coordinates tool exists.** UI elements are addressed by name through the
  accessibility tree. A screenshot is not part of any loop.
- **Secrets.** API keys live in the macOS Keychain. Nothing secret-shaped is ever stored
  in memory, logged, or written to this repo — the memory layer refuses and says why.
- **Audit.** Every call is recorded, including the refused ones, with secrets redacted.
- **The local console binds to 127.0.0.1** and has no route that executes anything.
- **Cloud is always visible.** The menu shows ☁️ whenever a cloud model is in use, and
  audio and file contents are never sent to one unless you explicitly allow it.

---

## Memory — the part that grows with you

Four scopes (global, workspace, agent, task) in one SQLite table. **No vector database
and no embedding model** — scoped `LIKE` matching, ordered by use and recency. On this
hardware that is not a compromise: it is instant, and it keeps the planner's context
small, which is time you would otherwise spend waiting.

Otto learns explicitly ("remember that…") and implicitly (which apps you actually open,
which commands you actually run). Everything is visible as plain rows in the developer
console, editable and deletable. Nothing that looks like an API key is ever stored.

---

## Deliberate omissions

These are choices, with reasons, not gaps.

- **No wake word, no always-on listening.** Push-to-talk only. A wake-word model is
  cheap; an open microphone and an inference tick running forever on a throttling
  laptop is not, and "~0% idle CPU" and "always listening" cannot both be true.
- **No Electron.** Comparable Electron menu-bar apps idle at 200–400 MB. Otto's entire
  budget is 250 MB.
- **No torch, no transformers, no WhisperX.** PyPI has no modern torch wheel for macOS
  x86_64, and Otto is designed so that none is needed. Speech is `faster-whisper`
  (CTranslate2, int8) at `base`, loaded on first use and **unloaded after five idle
  minutes**.
- **`say` for the voice.** Zero dependencies, zero RAM, instant. Piper is available and
  off by default; its medium voices have been reported above 2 GB.
- **The common commands do not use a model at all.** See `agentloop/fastpath.py`.

The reasoning and the numbers behind each are in
[`docs/RESEARCH.md`](docs/RESEARCH.md) and [`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Measured footprint

| Budget | Target | Measured |
| --- | --- | --- |
| Idle RAM | < 250 MB | **24 MB** core (Linux x86_64); ~60–90 MB expected on macOS with PyObjC |
| Idle CPU | ~0% | no polling loops anywhere; every timer is one-shot |
| Cold start | < 3 s | **88 ms** to import and construct |
| Push-to-talk → transcript | < 2 s | not measurable without a Mac — see STATUS.md |

`python3 scripts/footprint.py` re-measures this on your machine.

---

## Extending it

**A new tool** — one `ToolSpec` with a handler *and a verifier*, added to a list in
`otto/tools/`:

```python
CLOSE_APP = ToolSpec(
    name="close_app",
    description="Quit an application.",
    schema={"name": {"type": "string", "max_length": 64}},
    required=("name",),
    handler=lambda ctx, name: {"app": ctx.mac.resolve_app(name)},
    verifier=lambda ctx, args, result: (
        result["app"] not in ctx.mac.running_apps(),
        f"{result['app']} is closed",
    ),
    permission=Permission.CONFIRM,
)
```

The registry refuses a tool with no verifier. Add the name to an agent's `tools` tuple
in `otto/core/agents.py` and it is available to the planner immediately.

**A new agent** — one `AgentSpec` record. Give it the smallest ceiling that lets it do
its job.

**A new model provider** — one class with `complete()` and `available()` in
`otto/providers/clients.py`, plus a line in the factory.

**Tests** — `python3 -m pytest tests -q`. They need no Mac, no microphone, no model and
no network.

---

## Layout

```
otto/
  app.py              the single entry point shared by text and voice
  services.py         everything wired together
  core/               state, agents, permissions, audit
  tools/              registry (the one dispatch path) + the tools
  platform/mac.py     MacBridge: osascript + fake
  security/           paths, argv screening, secrets
  memory/             SQLite personalisation
  providers/          Ollama, OpenAI-compatible, Anthropic, Gemini, mock, null
  agentloop/          planner, plan validation, supervisor, fast path
  voice/              capture, ASR, the shared pipeline
  ui/                 menu bar, hotkey, developer console
docs/                 RESEARCH, DECISIONS, ARCHITECTURE
tests/                443 tests, all runnable without a Mac
```

Start with [`SETUP.md`](SETUP.md). Then [`STATUS.md`](STATUS.md), which is candid about
what has been verified and what still needs a Mac.
