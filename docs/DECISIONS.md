# Decisions log

Every ambiguity resolved during the unattended build, with the reason. The rule I applied
throughout: **when in doubt, pick the lighter option that is most likely to actually run on
a 2019 Intel i9 tomorrow morning.**

Format: `D-nn — decision — why — what it costs.`

---

**D-01 — Menu bar is `rumps`, not Electron, not a native Swift app.**
Electron is 200–400 MB idle (RESEARCH §1), which fails the budget before Otto does
anything. A Swift app would need Xcode, which is not guaranteed on the machine. rumps is
pure Python over PyObjC, no compiler.
*Cost:* rumps 0.4.0 was last released in 2022. Mitigated by keeping all rumps contact
inside `otto/ui/menubar.py`, which is replaceable with direct PyObjC.

**D-02 — One process, not a daemon plus a UI client.**
Two processes means two interpreters means roughly double the floor RAM, plus IPC.
*Cost:* long tool calls must not block the UI, so the supervisor runs on a worker thread
and the UI only ever reads a snapshot.

**D-03 — ASR default is `base`, not `tiny`.**
`tiny` is the safest for the budget but its accuracy on command phrases with proper nouns
("open Xcode", "run pytest") is poor enough to make Otto feel broken. `base` int8 is still
comfortably inside the 2 s budget by the §2 extrapolation. Configurable to `tiny` in one
setting, and `setup.sh` mentions it.
*Cost:* ~2× the transcribe time and RAM of `tiny`, both still within budget.

**D-04 — The ASR model is lazy-loaded and unloaded after 5 minutes idle.**
A resident model would consume most of the 250 MB idle budget on its own. Unloading returns
idle RAM to the interpreter floor.
*Cost:* the first press after an idle period pays a model load. Hidden behind the
"listening" state (we load while the user is still speaking), and the unload timer is a
one-shot `threading.Timer`, not a polling loop.

**D-05 — No always-on wake word.** See RESEARCH §7. Push-to-talk only, documented as a
choice.

**D-06 — There is a deterministic, no-LLM fast path for common commands.**
This is the single most important concession to the hardware. "Open Safari", "create a
folder called X on my Desktop", "remember that …" are matched by a small ordered set of
regex intents and dispatched straight to the tool registry — no model call, no fans, and it
works on a machine with **no LLM configured at all**, which is the user's exact situation
tomorrow. The LLM planner handles everything the fast path does not recognise.
*Cost:* a hand-written matcher to maintain. Worth it: it makes Otto useful before the user
has installed anything.

**D-07 — TTS is `say`; Piper is optional and off.** RESEARCH §3. Piper's reported >2 GB for
medium voices is disqualifying as a default on this machine.

**D-08 — No vector DB, no embeddings.** The brief forbids it and the data volume (a few
hundred preference rows) does not justify it. SQLite + scoped `LIKE` + recency ordering.

**D-09 — Secrets live in the macOS Keychain via the `security` binary, with an
`OTTO_*_API_KEY` environment fallback.**
`keyring` as a dependency pulls its own backends; shelling out to `/usr/bin/security` with
an argv list (never a shell string) is zero-dependency and native. On Linux, and in tests,
the store degrades to an in-memory implementation so the suite runs here.
*Cost:* unverifiable from Linux — flagged in STATUS.md.

**D-10 — Every macOS call goes through `otto/platform/mac.py:MacBridge`, an ABC with
`OsascriptMac` (real) and `FakeMac` (tests).**
The brief requires it and it is the only way to have a meaningful test suite on Linux.
`OsascriptMac` refuses to construct on a non-Darwin platform, so a test can never
accidentally exercise it.

**D-11 — AppleScript values are passed only as `on run argv` runtime arguments.**
Never `.format()`, never f-strings into script source. There is a dedicated test asserting
a `" & (do shell script "id") & "` payload is treated as literal text.

**D-12 — No shell anywhere. Ever.** `subprocess` is always called with an argv list and
`shell=False`. A central screener rejects binaries outside an allowlist *and* rejects
dangerous argument shapes within allowed binaries (`find -exec`, `git -c
credential.helper=`, `git --upload-pack=`, `ext::` URLs, `rg --pre`, `make -f`, …), each
with its own test.

**D-13 — Deletes go to the Trash by AppleScript `Finder … delete`, never `os.unlink`.**
On Linux/test the fake records the move. A tool that cannot reach the Trash refuses rather
than falling back to unlink.

**D-14 — Filesystem allowlist is Desktop, Documents, Downloads and `~/Projects` — not
`$HOME`.** Resolved with `Path.resolve()` (which resolves symlinks) and then checked with
`is_relative_to` against resolved roots, so traversal, absolute escape and symlink escape
are all caught by the same check. A credential denylist screens the resolved path
afterwards.

**D-15 — Plans are validated JSON, never parsed prose.**
The model returns JSON; a validator checks every step against the real agent roster, the
real tool registry and the dependency graph, and rejects the whole plan on any malformed
step. Orchestration state is never regex'd out of free text.

**D-16 — Parallelism is a dependency-ordered wave scheduler over a small thread pool
(4 workers).** Steps with satisfied dependencies and no ordering conflict run together;
everything else serialises. Threads not processes: the work is I/O-bound (subprocess,
network) and processes would cost RAM.

**D-17 — Approvals are a blocking queue with cancellation, not a poll loop.**
An approval waits on a `threading.Event`. Cancelling a task sets every outstanding event
with a CANCELLED verdict, so no waiter can leak. Budget rule: no polling loops anywhere.

**D-18 — The developer console is a local `http.server` on `127.0.0.1`, started only when
the user opens it, serving a static snapshot as JSON + one small HTML page.**
No framework, no websocket, no always-on port. It is read-only: there is no route that
executes anything.

**D-19 — When no model is configured, Otto still starts and still works** through the fast
path, and says so in plain language in the menu and out loud. This is a first-class state,
not an error.

**D-20 — Python 3.11 is the floor** (the machine has 3.11+). Uses `tomllib`,
`Path.is_relative_to`, `X | None` syntax freely. No 3.12-only syntax, so 3.11 works.

**D-21 — Dependencies are kept to five, all pure-Python or pre-built wheel, all optional
except the first.** `rumps`+`pyobjc-framework-Cocoa` (menu bar), `pynput` (hotkey),
`sounddevice` (mic), `faster-whisper` (ASR), `requests` (providers). The core harness —
tasks, agents, tools, permissions, memory, providers — imports **nothing outside the
standard library**, which is why the test suite runs on Linux with zero installs.

**D-22 — `requests` is not required either.** Providers use `urllib.request` from the
stdlib. One less wheel, one less thing to break on Intel macOS.
*Cost:* slightly more verbose HTTP code, confined to one file.

**D-23 — Audio capture is behind an interface too** (`AudioCapture` / `FakeAudioCapture`),
so the voice pipeline is testable on Linux and a wake-word engine could be added later
without touching the pipeline.

**D-24 — Text and voice share exactly one pipeline.** `VoicePipeline` turns audio into a
string and then calls the *same* `Otto.handle_utterance()` the text box calls. There is no
second code path to keep in sync.

**D-25 — Memory never stores anything that looks like a secret.** A shaped-value detector
(entropy + known prefixes like `sk-`, `ghp_`, `AKIA`, `-----BEGIN`) refuses the write and
records the refusal in the audit log. Tested.

**D-26 — Cold start: nothing heavy is imported at module level.** `faster-whisper`,
`sounddevice`, `pynput` and `rumps` are imported inside functions or guarded by
`TYPE_CHECKING`. `python -c "import otto"` must stay in the tens of milliseconds; there is
a test asserting the heavy names are absent from `sys.modules` after importing the app
core.

**D-27 — Retries: at most one, and only after re-inspecting state.**
The retry path re-runs the tool's verifier first; if the verifier now passes, the call is
marked COMPLETED without re-executing. Otherwise it retries once with the failure reason
fed back in as context. No blind loops.

**D-28 — Where the brief and the budget conflict, the budget wins**, and the cut is
recorded in STATUS.md. Nothing in section 10 was cut for the budget in the end; what was
cut was cut for time, and is listed there.
