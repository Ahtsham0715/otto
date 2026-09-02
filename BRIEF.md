# Build brief — Otto

You are building **Otto**, a personal, local-first AI assistant for macOS, from scratch,
overnight, unattended. The user is asleep and returns in the morning. Work the entire
session. Do not stop to ask questions. When something is ambiguous, pick the option most
likely to actually run on their Mac tomorrow, write the assumption into `docs/DECISIONS.md`,
and keep going.

**Commit and push to `main` repeatedly throughout the run.** If the session dies, only what
you pushed survives. Never leave work sitting in the sandbox.

---

## 1. What Otto is

A menu-bar macOS app you talk to. Press a hotkey, speak, it acts on your Mac, verifies the
action worked, and tells you what it did — out loud. Behind it sits a multi-agent harness
that plans a task, delegates to specialist agents, calls real tools, verifies results, asks
permission before anything destructive, and remembers the user's preferences so it becomes
more personal over time.

Commands it must handle end to end:

- "Open Safari." / "Open VS Code."
- "Open my project and run the tests."
- "Create a folder called Test on my Desktop."
- "Read this file and summarise it."
- "Remember that my projects live in ~/Projects." — and use that later, unprompted.

It must **execute** actions, not describe them.

---

## 2. The user's machine — this constrains everything

Read this twice. Getting it wrong means nothing runs tomorrow.

- **macOS on an Intel x86_64 Mac.** Not Apple Silicon. Anything Apple-Silicon-only (MLX,
  many recent PyTorch wheels, `mps`) is unavailable. PyPI has **no** `torch` wheels for
  macOS x86_64 at modern versions — if you need torch, it must come from **conda-forge**
  (`conda install --override-channels -c conda-forge pytorch`), and you should prefer a
  design that does not need torch at all.
- **Python 3.11+, Node 20+, npm and conda are installed. Rust/cargo is NOT. Xcode is NOT
  guaranteed.** Prefer a stack that needs neither.
- **No LLM is configured yet** — no Ollama, no LM Studio, no API keys anywhere.
- The user's shell is zsh. Their home is `/Users/apple`.

**You are running on Linux in the cloud. You cannot test any of the macOS-specific
behaviour** — no AppleScript, no Accessibility API, no microphone, no menu bar, no
`osascript`, no `say`. Plan for that (see §7).

---

## 3. Phase 0 — research before you write code

Use WebSearch and WebFetch. Write what you find, with links and dates, to
`docs/RESEARCH.md`. Cover at least:

1. **Menu-bar app stack.** Compare `rumps`, PyObjC, Electron, Tauri for a macOS menu-bar
   assistant. Which installs on Intel macOS with no Rust and no Xcode? Recommend one.
2. **Local speech-to-text on an Intel CPU.** Compare `faster-whisper` (CTranslate2),
   `whisper.cpp`/`pywhispercpp`, `openai-whisper`, and Apple's built-in dictation. Which
   gives usable latency on an Intel CPU with no GPU? What model size?
3. **Text-to-speech.** macOS `say` needs zero dependencies and always works — treat it as
   the floor. Research one better local option and how to make it optional.
4. **Global hotkey + push-to-talk on macOS** without Xcode. What permissions does macOS
   demand (Accessibility, Input Monitoring, Microphone) and how does an app request them?
5. **macOS automation.** AppleScript via `osascript`, the Accessibility API via
   `System Events`, JXA. How do you enumerate and click UI elements *by name* rather than
   by coordinates? What does the Accessibility permission prompt look like and how is it
   granted?
6. **Local LLM options for an Intel Mac.** Ollama on Intel — which small models are usable
   (llama3.2:3b, qwen2.5:3b, phi)? What throughput should be expected? How is Ollama
   installed and a model pulled non-interactively?
7. **Wake-word / always-listening** options and their cost. Decide whether v1 uses
   push-to-talk only (recommended) and say why.

Then write `docs/ARCHITECTURE.md` with your chosen stack and the reasoning.

---

## 4. Architecture — non-negotiable principles

These are hard-won; do not design around them.

- **Structured state, never LLM prose.** Task, Subtask, AgentMessage, ToolCall, Artifact,
  Approval and an execution timeline are real typed objects. The model proposes a plan;
  your code validates it against the real agent roster and rejects anything malformed.
  Orchestration state is never parsed back out of free text.
- **Statuses**: PENDING, RUNNING, WAITING, COMPLETED, FAILED, CANCELLED, REQUIRES_HUMAN.
- **Agents are configuration, not subclasses.** id, name, role, instructions, model,
  tools, permission ceiling, memory scope, max steps. Adding a specialist means adding a
  record. Ship: Supervisor, Planner, Mac, Files, Coder, Research, QA, Reviewer.
- **One dispatch path.** Every action goes through a tool registry that does:
  validate arguments against a schema → enforce permission → audit → execute → **verify** →
  record. An agent never holds a handler and cannot bypass a step.
- **Verification is mandatory.** Each tool declares a verifier that re-reads real state
  after the fact (does the folder exist? is that app actually frontmost?). A failed
  verification marks the call FAILED. Never let the model narrate success.
- **Never blind-retry.** Inspect state before trying again, and retry at most once with the
  reason fed back in.
- **Accessibility APIs over screenshots.** Semantic operations: `find_element`,
  `inspect_accessibility_tree`, `click_element`, `type_into_element`, `select_menu_item`,
  `get_active_window`. There must be **no** "click at x,y" tool. A screenshot is a
  last-resort fallback, never the main loop.
- **The user can always cancel.** Cancellation releases every waiting approval; a cancelled
  task never resurrects.
- **Parallelism where it is real.** Independent plan steps run concurrently; dependent ones
  do not. Do not spawn agents that aren't needed.

## 5. Security — enforce in code, never by prompt wording

Assume the agent is hostile: prompt injection from a web page or a README it just read is
the expected case.

- **Permission engine, three levels.** SAFE (open an app, read a file, git status) runs.
  CONFIRM (write a file, run a command, click something) asks the human. ALWAYS_CONFIRM
  (delete, commit, anything irreversible) always asks. Each agent also has a **ceiling** it
  cannot exceed regardless of what the human would approve. Enforced inside the dispatch
  function.
- **Deletes go to the Trash**, never `unlink`.
- **Filesystem scope.** A configurable allowlist of folders (Desktop, Documents, Downloads,
  Projects — *not* `$HOME`). Resolve every agent-supplied path and reject traversal,
  absolute escapes and symlink escapes. Then screen the resolved path against a credential
  denylist: `.ssh`, `.aws`, `.gnupg`, Keychains, `.env*`, `*.pem`, `id_rsa*`, `.npmrc`,
  `credentials`, `.netrc`.
- **No shell, ever.** Commands are argv lists against a binary allowlist. Reject any string
  containing shell metacharacters. Scrub the child environment of API keys. Timeout and cap
  output.
- **Screen arguments too, not just the binary.** An allowlist that only checks `argv[0]` is
  bypassable: `find -exec`/`-delete`, `git --upload-pack=`, `git -c core.sshCommand=`,
  `git -c credential.helper=`, `ext::` URLs, `rg --pre`, `make -f` all execute arbitrary
  commands through an *allowed* program. Block those specifically. Write tests for each.
- **AppleScript injection.** Every untrusted value must reach `osascript` as a runtime
  argument via `on run argv` — **never** interpolated into script source. Test that a
  payload like `" & (do shell script "id") & "` is typed literally, not executed.
- **App names** are matched against apps actually installed on the Mac; reject path-shaped
  or metacharacter-bearing names.
- **URLs**: `http`/`https` only. No `file://`, no custom schemes.
- **Bind every local server to 127.0.0.1.** Never expose shell execution over a socket.
- **Audit every tool call**, including refused ones, with secrets redacted.
- **Secrets**: macOS Keychain or an encrypted local store. Never hardcode a key, never
  commit one, never log one.
- **Never send audio, files or screenshots to a cloud model** unless the user explicitly
  allowed it, and show clearly when a cloud model is in use.

## 6. Personalisation — "an agent that grows with me"

This is the feature the user cares most about. Make it real, not a stub.

- **Memory scopes**: global (standing preferences), workspace (per project), agent, task.
  Local SQLite. Inspectable, editable, deletable from the UI.
- **Never auto-store secrets.** Refuse values shaped like API keys, tokens or private keys.
- **Learn from use**: remember corrections ("no, my project means ~/Projects/app"),
  preferred apps, frequent tasks, working hours, naming habits. Feed relevant memory into
  the planner as context.
- **A profile the user can read and edit** — surfaced in the UI as plain rows, not a
  mystery blob. The user must be able to delete anything.
- No vector database. SQL `LIKE` and good scoping first; add embeddings only if you can
  show they're needed.

## 7. What you can and cannot verify — be rigorous and honest

You are on Linux. You cannot run any macOS API.

- Put **all** macOS-specific code behind a small interface with two implementations: the
  real `osascript`-backed one, and a fake used in tests.
- Write a **large deterministic test suite that runs in the cloud** with mock agents, a
  mock LLM provider and the fake macOS layer. Cover: agent lifecycle, delegation, parallel
  execution, agent messaging, task state, tool registry, permission enforcement,
  dangerous-command blocking, argument-screening bypasses, filesystem restrictions,
  AppleScript-injection safety, approval flow, memory isolation and secret refusal,
  provider abstraction, cancellation, retries, failure recovery.
- **Run the tests. Make them pass. Show the output in the final report.**
- Anything you could not execute goes in `STATUS.md` under "unverified — needs a Mac", with
  exactly how to verify it. Do not describe untested macOS behaviour as working.

## 8. LLM providers

Provider abstraction from day one; never hardcode one vendor. Support Ollama and any
OpenAI-compatible local server, plus OpenAI / Anthropic / Gemini / generic
OpenAI-compatible cloud endpoints. Each agent may use a different model. The app must
start, run and give a clear, friendly message when **no** model is configured — that is
the user's exact situation tomorrow morning.

## 9. Setup must be one command

The user has no LLM, no models, and limited patience. Ship:

- `setup.sh` — idempotent, safe to re-run, works on Intel macOS. Creates the environment,
  installs dependencies, checks for Ollama and offers to install it plus pull a small
  model, and prints exactly which macOS permissions to grant and where.
- `run.sh` — starts Otto.
- `SETUP.md` — numbered steps, five minutes end to end, including screenshots-in-words of
  the macOS permission prompts (Accessibility, Microphone, Input Monitoring) and what to do
  when a prompt does not appear.
- `STATUS.md` — what is built, what is verified by tests, what is unverified, what is not
  done, and the honest next steps.
- `README.md` — what Otto is, the architecture, the security model, how to extend it with
  your own agents and tools.

Assume the user's first action is `./setup.sh` and their second is `./run.sh`. If those two
do not work, nothing else matters.

## 10. Build order — each phase working and tested before the next

1. Harness core: task/agent/tool/permission model + tests.
2. Tool registry with schema validation, permission gate, audit, verification + tests.
3. macOS layer behind its interface (apps, windows, accessibility, clipboard,
   notifications) + fake implementation + tests.
4. Filesystem, terminal and git tools with the full sandbox + security tests.
5. Provider abstraction + mock provider.
6. Supervisor: planning, delegation, parallelism, retries, cancellation, merge + tests.
7. Memory and personalisation + tests.
8. Voice: local ASR in, local TTS out. Text and voice must share exactly one pipeline.
9. Menu-bar app: hotkey, the UI states (idle, listening, thinking, executing, waiting for
   confirmation, speaking, error), transcript, current task, active agents, approval
   prompts, recent tasks.
10. A developer console showing agents, messages, tool calls, decisions, artifacts, errors
    and the execution timeline.
11. `setup.sh`, `run.sh`, `SETUP.md`, `STATUS.md`, `README.md`.

If you run low on time, a smaller system that genuinely works beats a larger one that does
not. Cut from the end of the list, and say plainly in `STATUS.md` what you cut.

## 11. Ground rules

- Do not build a web app for voice cloning or dubbing. This is a desktop assistant.
- Do not copy or depend on any other project of the user's.
- Reliability over feature count. Deterministic tools over free-form control.
- Small modular files. No giant god-class.
- Report honestly at the end: what works, what is untested, what failed, what you would do
  next. If tests fail, say so and show the output.
