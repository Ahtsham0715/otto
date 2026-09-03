# Status

Built overnight, unattended, on Linux. **I could not run a single line of macOS code.**
That shapes everything below: this document separates what is *verified by tests that
actually ran*, what is *reasoned but unproven*, and what is *not done*.

**Test suite: 387 tests, all passing, in 5.8 seconds.** No Mac, no microphone, no model,
no network required.

```
$ python3 -m pytest tests -q
...........................................................................
387 passed in 5.79s
```

---

## 1. The performance budget

`python3 scripts/footprint.py`, measured in this sandbox on **x86_64 Linux, Python
3.11** — the same processor architecture as the target Mac.

| Budget (BRIEF §2b) | Target | Measured | Verdict |
| --- | --- | --- | --- |
| Idle RAM, whole app | **< 250 MB** | **24.2 MB** after a command (10.9 MB interpreter + 12.6 MB Otto + 0.7 MB running) | **PASS**, with ~10× headroom |
| Idle CPU | **~0%** | No polling loop exists anywhere. Every timer is a one-shot `threading.Timer`; the approval broker blocks on an `Event`; the console's port is not even opened until you ask for it | **PASS by construction** |
| Cold start to menu-bar icon | **< 3 s** | **88 ms** to import `otto.app` and construct `Otto()` — plus rumps/PyObjC, unmeasured | **PASS**, ~34× under |
| Push-to-talk → transcript | **< 2 s** | **not measured** — see §3 | **unverified** |

**What the macOS additions will cost.** The 24 MB above excludes `rumps`, PyObjC and
`sounddevice`, none of which install here. PyObjC's Cocoa bindings plus rumps are
commonly a few tens of MB; a realistic idle figure on the Mac is **60–90 MB**, still far
inside 250 MB. Verify it in one command on the Mac:

```zsh
./run.sh &            # then, with Otto idle in the menu bar:
ps -o rss= -p $(pgrep -f "python.*-m otto" | head -1)   # KB; divide by 1024 for MB
```

**Measured import costs on x86_64** (these are real, from this sandbox):

| Import | Time | RSS delta |
| --- | --- | --- |
| `otto.app` (the whole harness) | 0.08 s | **+15.9 MB** |
| `numpy` | 0.06 s | +17.7 MB |
| `ctranslate2` | 0.05 s | +17.6 MB |
| `faster_whisper` | 0.17 s | **+54.6 MB** |

That last row is exactly why the ASR model is lazy-loaded and unloaded after five idle
minutes: importing the speech stack costs more than three times the entire rest of Otto.
It is imported inside a function, and a test asserts it is absent from `sys.modules`
after importing the app.

**Model weights are not in that 54 MB.** `base` int8 weights are roughly 75 MB on disk;
expect **150–250 MB resident while transcribing**, returning to ~0 on unload. I could
not confirm this: the sandbox blocks huggingface.co (`403` on CONNECT), so no model ever
downloaded. It is the single number most worth checking on the Mac.

---

## 2. Verified by tests that actually ran

Every item here has at least one test that executed in this sandbox and passed.

| Area | Tests | What is actually proven |
| --- | --- | --- |
| **Command screening** | 48 | Each argument-level bypass is refused: `find -exec/-execdir/-ok/-delete`, `git -c` (sshCommand and credential.helper), `git --upload-pack=`/`--receive-pack=`/`--exec-path=`/`--config-env=`, `ext::` URLs, `rg --pre`, `make -f`/`--eval`, `python -c`, `node -e`, `npm --node-options=`, `--use-compress-program`. Shells are off the allowlist; paths are not binary names; every shell metacharacter in any argument is refused; the child environment is stripped of every credential-shaped variable |
| **macOS bridge & injection** | 38 | Against the **real** `OsascriptMac` with `subprocess` replaced: four AppleScript injection payloads — including `" & (do shell script "id") & "` — appear **only** as trailing `on run argv` arguments and never in the script source; `shell=False` throughout; `say` takes text as an argument; permission errors (-1743) are recognised; path-shaped and metacharacter app names are refused; non-http URL schemes are refused |
| **Filesystem scope** | 31 | `$HOME` itself is not a root; `/etc/passwd` refused; `../../..` refused; **a symlink inside Desktop pointing outside is refused** (both directory and file); 18 credential path shapes refused even inside an allowed folder; lookalike names (`environment.md`, `keynote.txt`) still allowed |
| **Secrets & audit** | 26 | 11 credential shapes detected, 6 ordinary preferences not; redaction covers keys, value shapes and nested structures; the audit file never contains the secret it audited; the Keychain call is an argv list with `shell=False` (verified by patching `subprocess`, not a stand-in) |
| **Fast path** | 29 | Seven spoken phrasings of "open Safari" reach one intent; every fast-path plan validates against the real roster; **fast-path commands make zero model calls**; a fast-path plan still hits the permission engine (a denied approval leaves no folder behind) |
| **Planner validation** | 27 | Unknown agent, supervisor-as-target, unknown tool, a tool the agent may not hold, missing description, duplicate id, dangling dependency, self-dependency, cycle, over-length plan and non-object args each reject the **whole** plan; prose is never parsed into a plan; waves group independent steps and serialise dependent ones |
| **Tools + verification** | 42 | Verifiers re-read real state; a handler that lies is marked FAILED; a raising verifier is a failure, not a crash; a non-zero exit code is a *result* but a timeout is a failure; output is capped; Trash is used instead of unlink |
| **Supervisor** | 22 | Independent steps genuinely **overlap in wall-clock time** (asserted with an Event, not by reading the code); dependent steps genuinely do not; a failure is retried **exactly once**; a retry whose verifier now passes **does not repeat the work**; cancelling while an approval is pending **releases the blocked worker**; a model replying in prose never has that prose turned into an action |
| **Permissions** | 9 | The broker **fails closed** with no UI attached; an agent ceiling refuses even when the human approves; a broken UI hook denies rather than hangs; a timeout becomes a denial; cancellation releases every outstanding approval |
| **Memory** | 24 | Four scopes isolated; workspace memories only surface in their own workspace; secrets refused; everything deletable; usage counting; persistence across reopen |
| **Providers** | 24 | Fenced/chattered JSON parsed, prose rejected; the null provider explains instead of pretending; request shapes for Ollama, OpenAI-compatible, Anthropic and Gemini; a non-http endpoint refused; keys come from the store, never the config file |
| **Voice & app** | 38 | Voice reaches the **same** `handle_utterance` as text (asserted with a spy) and produces identically-shaped tasks; silence names the Microphone permission; the ASR model is absent until used, preloaded during recording, and unloaded by a one-shot timer; **importing `otto.app` pulls in none of** torch/transformers/numpy/faster_whisper/sounddevice/rumps/pynput (checked in a subprocess) |
| **Console** | in the 38 above | Binds to 127.0.0.1; the snapshot needs a token; memory is editable and deletable; a secret is refused; **no route executes anything** (five exec-shaped paths all 404) |
| **State** | 10 | The transition table; terminal states are terminal; a cancelled task cannot resurrect; **three threads blocked on approvals are all released by one cancel** |

Two real bugs were found by these tests and fixed, not papered over:

1. `open_app` read `frontmost_app()` back as its own result, making its verifier
   tautological — an app that silently failed to launch still "verified". The bridge now
   exposes `resolve_app()` and the verifier checks the app that was *asked for*.
2. Memory search treated a query of pure stopwords ("the a is") as an empty query and
   returned the user's entire profile.

Also verified by running them here: **`setup.sh` end to end** (creates the venv,
installs wheels, handles an unreachable model download without a traceback, runs the
tests, prints the report) and **`run.sh --text`/`--check`**.

---

## 3. Unverified — needs a Mac

Nothing in this section has ever run. It is written against documented behaviour
(sources in `docs/RESEARCH.md`) and is exercised only through `FakeMac`. Treat every row
as a hypothesis with a stated experiment.

| # | What is unverified | Why I could not test it | How to verify, in one step |
| --- | --- | --- | --- |
| 1 | **Every real `osascript` call**: `open_app`, `open_url`, `frontmost`, `active_window`, `running_apps`, `click_element`, `type_into`, `select_menu`, the accessibility `tree` dump, clipboard, `notify`, Trash | No macOS | `./run.sh --text "open Safari"` — it should report *"Safari is frontmost"* |
| 2 | **That the accessibility scripts are valid AppleScript at all.** They are syntactically plausible and follow the System Events idiom, but an unparsed script is an error at runtime, not import time. `select_menu` and `tree` are the most likely to need adjusting | No `osascript` | `osascript -e 'tell application "System Events" to tell process "Safari" to click menu item "New Window" of menu 1 of menu bar item "File" of menu bar 1'` — then Otto's own `select_menu_item` |
| 3 | **The Trash move.** `FakeMac` records the call; it does not move anything, so the verifier's real branch (`path no longer exists`) has never run | No Finder | Create `~/Desktop/junk.txt`, `./run.sh --text "delete ~/Desktop/junk.txt"`, approve, confirm it is in the Trash and *not* deleted |
| 4 | **The AppleScript injection defence in production.** The *mechanism* is tested (values go in as argv, never as source). What is untested is that a real `osascript` treats those argv values as inert text | No `osascript` | Run the one-liner below — it must **echo** the payload, not run `id` |
| 5 | **The menu bar itself.** No `rumps` here, so `MenuBarApp` has never been constructed: the status item, the seven state titles, the approval modal, `rumps.Window` for typing | No PyObjC | `./run.sh` — a 🎙 should appear; every menu item should open something |
| 6 | **The global hotkey.** `pynput` is absent, and per the research it *fails silently* without Input Monitoring — the failure mode most likely to bite you | No macOS input stack | `./run.sh`, press ⌃⌥Space, watch for 🔴. If nothing: menu → *Check permissions…* |
| 7 | **The microphone.** `sounddevice`/PortAudio never opened a device; the real capture path and the "denied permission returns silence" assumption are untested against a real device | No audio hardware | Press the hotkey, speak, press again |
| 8 | **All ASR numbers.** No model ever downloaded (huggingface.co blocked). Model load time, transcription latency, and the < 2 s budget are **extrapolations** from published benchmarks, not measurements | Network policy | `python3 -c "import time,faster_whisper as f; t=time.time(); m=f.WhisperModel('base',device='cpu',compute_type='int8'); print('load',time.time()-t)"`, then time a real command |
| 9 | **`say`.** Argument construction is tested; nothing has ever been spoken | No macOS | `./run.sh --text "open Safari"` and listen |
| 10 | **The Keychain.** The argv construction is tested; `/usr/bin/security` was never invoked | No Keychain | `security add-generic-password -U -s otto-assistant -a groq -w test`, then `./run.sh --check` |
| 11 | **`list_apps` on a real Mac.** It reads four `/Applications` directories; the fuzzy-match rules (exact → unique substring → refuse if ambiguous) have only met the fake's six apps | No `/Applications` | `./run.sh --text "open vs code"` |
| 12 | **Ollama and every cloud provider over a real network.** Request shapes are tested against a fake `urlopen`; no real endpoint was ever called | Network policy | `ollama serve`, then `./run.sh --repl` and ask something that needs planning |
| 13 | **The macOS idle RAM figure.** 24 MB is Linux-without-PyObjC | No PyObjC | The `ps` command in §1 |
| 14 | **Whether `rumps` 0.4.0 (2022) still behaves on current macOS.** Chosen for footprint; its age is a real risk | No macOS | `./run.sh`; if it fails, `otto/ui/menubar.py` is the only file that touches rumps |

The injection check for item 4, in full:

```zsh
osascript -e 'on run argv
return item 1 of argv
end run' -- '" & (do shell script "id") & "'
```

It should print the payload back verbatim. If it prints your user id instead, stop and
tell me — that would mean the argv contract does not hold and the defence needs rework.

**Expect item 2 to need a fix.** If I had to bet on one thing breaking on first contact
with a real Mac, it is the exact AppleScript in `_SCRIPTS` for `select_menu` and `tree` —
UI-scripting syntax is fussy and every one of those strings is unexecuted. They are all
in one dictionary at the top of `otto/platform/mac.py`, and the argv-passing contract
around them is what the injection tests protect, so fixing a script cannot weaken the
security property.

---

## 4. What is not built

| Not done | Why |
| --- | --- |
| **A packaged `.app` bundle** (py2app) | Otto runs from the terminal. Bundling changes which process macOS attaches permissions to, and I could not test that consequence. Time, not budget |
| **A settings UI** | Config is `~/.otto/config.json`, documented in SETUP.md §6. The console edits memory, not settings |
| **Streaming model output** | Replies arrive whole. With a local model that means a longer silence before Otto speaks |
| **A QA/Reviewer pass after every plan** | Both agents exist and are reachable by the planner, but the supervisor does not automatically append a review step. Per-tool verification already covers the "did it work" question |
| **`git commit` as a tool** | The screener allows the subcommand; no tool wraps it. Deliberate: a commit is the kind of irreversible act I did not want to ship untested |
| **Piper TTS end to end** | The code path exists and is off by default. Never run |
| **Wake word** | Cut on purpose, not for time — see README → Deliberate omissions |

**Nothing was cut to meet the performance budget.** The budget shaped choices
(no Electron, no torch, no wake word, no vector DB), but every numbered item in BRIEF §10
is present. The four items above are time and untestability.

---

## 5. Honest assessment

**What I am confident in.** The harness — state machine, dispatch path, permission
engine with ceilings, plan validation, wave scheduling, retry-once-after-inspection,
cancellation — is pure Python with no platform dependencies, and it is tested hard,
including the concurrency and deadlock cases that are usually left to hope. The security
screens are the part I would most defend: the argv bypasses and the path-escape cases
are each individually tested, and the injection defence is structural (there is no
string-formatting path into a script) rather than a matter of careful escaping. The
footprint is not a claim, it is a measurement, and it has an order of magnitude of room.

**What I am not confident in.** Everything that touches macOS, in proportion to how
fiddly it is: I would expect `open_app`, `open_url` and `say` to work first try; I would
not be surprised if `select_menu_item` or the accessibility tree dump needs a syntax fix.
The ASR latency budget is an extrapolation from someone else's benchmark on a newer chip,
carried across two model sizes — the direction is safe, the exact number is not.

**The first five minutes tomorrow.** Run `./setup.sh`, then `./run.sh --check`, then
`./run.sh --text "open Safari"`. That last command exercises the real bridge end to end
and either prints *"Safari is frontmost"* or tells you exactly which macOS permission is
missing. Then grant the permissions in SETUP.md §3 and press ⌃⌥Space.

**What I would do next, in order.**

1. Work down §3 on the real machine, fixing the AppleScript that turns out to be wrong.
2. Measure the ASR numbers and put the real figures in §1 — then decide `tiny` vs `base`
   on evidence instead of my estimate.
3. Add a Groq key and use Otto for a day; the fast path will cover more than expected,
   and the requests that fall through to the planner are the honest backlog of intents
   worth adding to `fastpath.py`.
4. Only then consider the `.app` bundle, so that permissions attach to Otto rather than
   to your terminal.
