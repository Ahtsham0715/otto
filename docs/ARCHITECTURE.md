# Otto — architecture

The stack was chosen in `RESEARCH.md`; the ambiguities were resolved in `DECISIONS.md`.
This document says how the pieces fit.

## Shape

One Python process. A rumps status item on the main thread, a supervisor on a worker
thread, and a handful of pure-stdlib modules underneath that know nothing about macOS or
about each other.

```
                        ┌──────────────────────────────┐
   hotkey (pynput) ───► │  ui/menubar.py  (rumps)      │  main thread, ~0% idle
   mic (sounddevice) ─► │  UI states, approvals, menu  │
                        └──────────────┬───────────────┘
                                       │ snapshot (read-only)
                        ┌──────────────▼───────────────┐
   text box ──────────► │  app.py  Otto.handle_utterance│  ONE entry point
                        └──────────────┬───────────────┘
                          ┌────────────┴────────────┐
                          │                         │
                 ┌────────▼────────┐      ┌─────────▼─────────┐
                 │ fastpath.py     │      │ supervisor.py     │  worker thread
                 │ regex intents,  │      │ plan → validate → │
                 │ NO model call   │      │ waves → verify    │
                 └────────┬────────┘      └─────────┬─────────┘
                          └────────────┬────────────┘
                                       ▼
                        ┌──────────────────────────────┐
                        │ tools/registry.py            │  the ONLY dispatch path
                        │ schema → permission → audit  │
                        │   → execute → VERIFY → record│
                        └──┬────────┬────────┬─────────┘
                           │        │        │
                  ┌────────▼──┐ ┌───▼────┐ ┌─▼────────────┐
                  │ platform/ │ │ fs/    │ │ proc/        │
                  │ mac.py    │ │ sandbox│ │ argv allowlist│
                  │ (ABC)     │ │        │ │ + arg screen │
                  └─┬───────┬─┘ └────────┘ └──────────────┘
              OsascriptMac  FakeMac        ← tests only ever see the fake
```

## Modules

| Module | Responsibility | stdlib only? |
| --- | --- | --- |
| `otto/core/state.py` | `Task`, `Subtask`, `AgentMessage`, `ToolCall`, `Artifact`, `Approval`, `TimelineEvent`, `Status` | yes |
| `otto/core/agents.py` | The agent roster as **data** — id, role, instructions, model, tools, permission ceiling, memory scope, max steps | yes |
| `otto/core/permissions.py` | SAFE / CONFIRM / ALWAYS_CONFIRM, agent ceilings, the approval broker | yes |
| `otto/core/audit.py` | Append-only JSONL audit with secret redaction; records refusals too | yes |
| `otto/tools/registry.py` | The single dispatch function and the tool/verifier contract | yes |
| `otto/tools/*.py` | Tool definitions: mac, files, proc, git, memory | yes |
| `otto/platform/mac.py` | `MacBridge` ABC + `OsascriptMac` + `FakeMac` | yes |
| `otto/platform/audio.py` | `AudioCapture` ABC + real (sounddevice) + fake | yes |
| `otto/security/paths.py` | Path resolution, allowlist roots, traversal/symlink escape, credential denylist | yes |
| `otto/security/argv.py` | Binary allowlist **and** per-binary dangerous-argument screening | yes |
| `otto/security/secrets.py` | Secret-shaped-value detector; Keychain/env/in-memory store | yes |
| `otto/memory/store.py` | SQLite, four scopes, inspect/edit/delete, secret refusal | yes |
| `otto/providers/*.py` | `Provider` ABC, `MockProvider`, Ollama, OpenAI-compatible, Anthropic, Gemini | yes (`urllib`) |
| `otto/agentloop/supervisor.py` | Plan → validate → wave-schedule → delegate → merge → retry-once → cancel | yes |
| `otto/agentloop/planner.py` | Prompt construction and **strict JSON plan validation** | yes |
| `otto/voice/pipeline.py` | Record → transcribe (lazy ASR) → `handle_utterance` → speak | yes at import |
| `otto/ui/menubar.py` | rumps status item, the seven UI states, approval prompts | no (rumps) |
| `otto/ui/console.py` | Read-only dev console on 127.0.0.1, started on demand | yes |

Anything importing a wheel does so **inside a function**, so `import otto` stays cheap
(D-26, and there is a test for it).

## The dispatch contract

Nothing executes except through `ToolRegistry.dispatch(call, agent, ctx)`, which always
does, in order:

1. **Resolve** the tool by name. Unknown tool → `FAILED`, audited.
2. **Validate** arguments against the tool's JSON-ish schema (types, required, enums).
   Extra keys are rejected, not ignored.
3. **Permission**: compute the effective level; clamp to the agent's ceiling; if the
   ceiling forbids it, refuse — *even if a human would have approved*. If CONFIRM or
   ALWAYS_CONFIRM, block on the approval broker (interruptible by cancellation).
4. **Audit** the attempt, redacted, before it runs.
5. **Execute** the handler.
6. **Verify** by calling the tool's declared verifier, which re-reads real state. No
   verifier means the tool cannot be registered. Verifier false → the call is `FAILED`
   regardless of what the handler returned.
7. **Record** the result, the verification and the timing on the task timeline.

An agent never receives a handler and cannot skip a step: agents hold tool *names*.

## Statuses

`PENDING → RUNNING → {COMPLETED | FAILED | CANCELLED}`, plus `WAITING` (blocked on a
dependency or an approval) and `REQUIRES_HUMAN` (an approval was denied or a decision is
needed). Terminal states are terminal; a cancelled task cannot be resurrected.

## Agents shipped

Supervisor, Planner, Mac, Files, Coder, Research, QA, Reviewer — each a record in
`agents.py` with its own tool list and permission ceiling. Adding a specialist is adding a
record. Notably: Research has a `SAFE` ceiling (it reads the web/files and is the most
likely to be prompt-injected, so it *cannot* be granted write or exec even by an approving
human); Reviewer and QA are read-only; only Mac, Files and Coder can reach CONFIRM.

## Security posture

Assume the model is hostile, because a README it just read may be. Every control is in
code, none in prompt wording: path resolution and allowlisting, credential denylist, argv
allowlist plus argument screening, `on run argv` for all AppleScript, `http(s)`-only URLs,
`127.0.0.1` binding, environment scrubbing, output caps and timeouts, Trash instead of
unlink, and an audit record for refusals as well as successes.

## Personalisation

Four memory scopes (global / workspace / agent / task) in one SQLite table with an index on
`(scope, scope_key, key)`. Retrieval for the planner is scoped, `LIKE`-matched, recency-
and hit-count-ordered, and capped, so context stays small — which matters when generation
is 8 tok/s. Corrections are learned explicitly ("no, my project means …") and implicitly
(the app you actually open for a given word). Everything is listable, editable and
deletable from the UI, and nothing secret-shaped is ever written.
