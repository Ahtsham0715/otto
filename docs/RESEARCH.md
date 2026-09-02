# Otto — Phase 0 research

**Researched:** 2026-09-02, from a Linux cloud sandbox.
**Target machine:** 2019 MacBook Pro, Intel Core i9, no usable GPU (no MPS, no CUDA),
throttles under sustained load. Python 3.11+, Node 20+, conda present. No Rust, no
guaranteed Xcode. Home is `/Users/apple`, shell zsh. No LLM configured.

**A note on the numbers below.** I cannot run macOS or Intel-Mac hardware from this
sandbox, so *every* number here is either (a) quoted from a published benchmark, with the
link, or (b) an extrapolation I have labelled as such. Nothing here was measured on the
user's machine. Where a number decides an architectural choice, I say which way the
decision goes even if the number is off by 2×, so a wrong quote does not silently produce
a wrong build.

---

## 1. Menu-bar app stack, ranked by footprint

| Option | Idle RAM | Installs on Intel macOS without Rust/Xcode? | Verdict |
| --- | --- | --- | --- |
| **`rumps` (on PyObjC)** | ~30–60 MB for the Python process incl. interpreter (extrapolated: native Swift menu-bar apps are cited at 30–80 MB, and rumps adds a CPython interpreter to an otherwise-native Cocoa status item) | **Yes.** Pure Python; only dependency is PyObjC, which ships universal2 wheels. No compiler needed. | **Chosen** |
| Raw PyObjC (`NSStatusItem` directly) | Same as rumps, minus a few hundred KB | Yes | Fallback if rumps' event loop fights us; rumps *is* a thin wrapper over exactly this |
| Electron | **200–400 MB** for equivalent monitor apps | Yes, but ships a Chromium | **Rejected — blows the 250 MB budget on its own** |

Sources:
- rumps — <https://github.com/jaredks/rumps>, PyPI <https://pypi.org/project/rumps/> (0.4.0, 2022-10-15). Dependency list is PyObjC only; py2app is optional and only for bundling. No Xcode, no Rust.
- Electron vs native menu-bar RAM, 200–400 MB vs 30–80 MB native: <https://monithor.dev/guides/best-mac-menu-bar-apps> (2026)

**Decision: `rumps`.** It is the lightest thing that can own a macOS status item from
Python, it needs no toolchain, and it keeps the whole assistant in one process so there is
one RAM number to defend rather than two. rumps 0.4.0 is old but stable and its surface
(status item, menu, `@rumps.clicked`, `rumps.notification`) is tiny; the risk of it
breaking on a modern macOS is confined to a file we can replace with ~120 lines of direct
PyObjC.

Richer UI (the developer console) is served as a small local page to the user's *existing*
browser on `127.0.0.1`, on demand — never a bundled runtime. It is a plain
`http.server` thread that only starts when the user clicks "Developer console".

---

## 2. Speech-to-text on a 2019 Intel i9, no GPU

| Option | Needs torch? | Install on Intel macOS | Notes |
| --- | --- | --- | --- |
| **`faster-whisper` (CTranslate2), `tiny`/`base`, int8** | **No** — CTranslate2 is a standalone C++ runtime with its own wheels | Yes, x86_64 wheels with AVX support | **Chosen.** `tiny` weights ≈ 78 MB on disk at int8 |
| `pywhispercpp` / `whisper.cpp` | No | Pre-built CPU wheels on PyPI; CoreML path needs Xcode **and** is Apple-Silicon-oriented, so no benefit here | Strong second choice; kept as a documented alternative |
| Apple Speech (`SFSpeechRecognizer`) via PyObjC | No | Yes, bindings exist (`import Speech`) | Kept as an *optional* backend, not the default — see below |
| `openai-whisper`, WhisperX, `transformers` | **Yes** | **PyPI has no modern torch wheel for macOS x86_64** | **Rejected outright** |

Sources:
- faster-whisper benchmarks and requirements: <https://github.com/SYSTRAN/faster-whisper>. Their published CPU run is the **small** model on an Intel i7-12700K, 8 threads: int8 **1m42s** and **1477 MB** peak for a 13-minute audio file; fp32 is 2m37s / 2257 MB. Batched int8 is faster (51s) but costs 3608 MB — so **we do not batch**.
- "CPU int8 ≈ 20× real-time for `tiny`", and `tiny` weights ≈ 78 MB: <https://codersera.com/blog/faster-whisper-vs-whisper-cpp-speech-to-text-2026/>, <https://www.promptquorum.com/power-local-llm/local-whisper-stt-comparison-2026>
- Quantisation analysis (int8 halves weights, runtime buffers do not halve): <https://arxiv.org/html/2503.09905v1>
- pywhispercpp pre-built CPU wheels, and CoreML needing Xcode: <https://github.com/absadiki/pywhispercpp>, <https://github.com/ggml-org/whisper.cpp/issues/2701>
- PyObjC Speech bindings: <https://pyobjc.readthedocs.io/en/latest/apinotes/Speech.html>; `requiresOnDeviceRecognition` for offline: <https://developer.apple.com/documentation/speech/sfspeechrecognizer>

**Does `tiny`/int8 hit the < 2 s budget?** Extrapolating from the two published figures:
the *small* model int8 on a 2022 i7 transcribed 13 min of audio in 102 s ≈ 7.6× real-time.
`tiny` is roughly 8–10× cheaper than `small` (39M vs 244M params), and the 2019 i9 is
perhaps 0.6–0.7× the per-core throughput of the 12700K. Net: **~15–25× real-time for
`tiny` on this machine**, so a 5-second utterance should transcribe in roughly
**0.2–0.4 s**, plus model load. That clears 2 s with a lot of headroom — *provided the
model is already resident*. First use after launch pays a cold load (see §2b decisions:
we hide this behind the "listening" state and keep the model for 5 minutes).

**RAM:** the 1477 MB figure is `small`; `tiny` at int8 should be a small fraction of it.
Weights are ~78 MB; realistic resident cost with CTranslate2 buffers is estimated
**150–250 MB while transcribing**, dropping back to ~0 when we unload. This is exactly why
the model must be **lazy-loaded and unloaded on an idle timer** — a resident ASR model
would eat the entire idle budget by itself.

**Why not Apple's Speech framework as default?** It is genuinely free RAM-wise and
Apple-maintained, but: (a) on-device recognition is documented as less accurate than the
cloud path, (b) `SFSpeechRecognizer` on macOS has a history of crashes
(<https://developer.apple.com/forums/thread/715034>), (c) it demands its own
`NSSpeechRecognitionUsageDescription` and privacy consent, and (d) I cannot test any of it
from here. It ships as a *selectable* backend behind the same interface, off by default,
flagged unverified.

---

## 3. Text-to-speech

- **Floor and default: macOS `say`.** Zero dependencies, zero resident RAM, ships with the
  OS, starts instantly, and it is a subprocess we can kill on cancel. On a throttling
  machine this is not a compromise, it is the right answer.
- **Optional better local voice: Piper.** `pip install piper-tts` and there are x86_64
  macOS builds (<https://github.com/itsabhishekolkha/piper-x64-build>,
  <https://www.thoughtasylum.com/2025/08/25/text-to-speech-on-macos-with-piper/>). But the
  reported cost is real: medium-quality voices have been reported at **>2 GB RAM** and
  slower-than-realtime synthesis on modest hardware
  (<https://localaimaster.com/blog/piper-tts-setup-guide>), and "high" voices add seconds
  of delay. **Off by default**, selectable in settings, with the RAM warning in the UI.

---

## 4. Global hotkey / push-to-talk without Xcode

`pynput` gives us `keyboard.GlobalHotKeys` and `keyboard.Listener`, pure Python over
PyObjC/Quartz. macOS permissions required:

| Permission | Why | Where |
| --- | --- | --- |
| **Input Monitoring** | Read key events while Otto is *not* frontmost — this is what makes a global push-to-talk key work at all | System Settings → Privacy & Security → Input Monitoring |
| **Accessibility** | Required for UI scripting via System Events (§5), and pynput *silently fails* without it | System Settings → Privacy & Security → Accessibility |
| **Microphone** | Recording | Prompted on first record |

The failure mode that will bite the user: **pynput fails silently** rather than erroring
when the host process is not a trusted accessibility client
(<https://github.com/moses-palmer/pynput/issues/389>,
<https://github.com/moses-palmer/pynput/issues/416>). The permission attaches to the
*binary that runs Python* — Terminal.app, iTerm, or the python3 executable — not to our
script. So `SETUP.md` must tell the user to grant it to their terminal, and Otto must
**self-diagnose**: on start, check trust and show a red menu-bar state with a direct link
to the settings pane rather than appearing to work.

Sources: <https://manpages.ubuntu.com/manpages/noble/man3/pynput.3.html>,
<https://www.sweepformac.com/guides/mac-input-monitoring-permission/>,
<https://voiceinput.app/en/help/permissions/>

---

## 5. macOS automation

- **`osascript` + System Events** is the path with no Xcode and no build step. System
  Events exposes UI elements semantically: `button "OK" of window 1 of process "Safari"`,
  `menu item "New Window" of menu "File" …`, and a `click` command that takes an element
  rather than coordinates.
  Sources: <https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/AutomatetheUserInterface.html>,
  <https://github.com/JXA-Cookbook/JXA-Cookbook/wiki/System-Events>,
  <https://www.oreilly.com/library/view/applescript-the-definitive/0596102119/ch24s02.html>
- **Prerequisite:** the Accessibility API must be enabled for the calling process, or every
  UI-scripting call fails with "not allowed assistive access" (errAEEventNotPermitted /
  -1743). Permission is keyed to the signing identity of the caller, which for us is the
  terminal or the Python binary. (<https://discussions.apple.com/thread/254078469>)
- **Injection is the real hazard.** `osascript` compiles its argument as *source*. Any
  untrusted value interpolated into that source is arbitrary code execution — AppleScript
  can call `do shell script`. The safe pattern is `on run argv` with values passed as
  **runtime arguments** after the script, never string-formatted in. Otto enforces this in
  one place and has a test that a payload like `" & (do shell script "id") & "` is typed
  as literal text.
- **Screenshots are not the loop.** There is no `click at x,y` tool in Otto at all.

---

## 6. Local LLM on a 2019 Intel i9 — honestly

This is the number that decides whether Otto feels usable tomorrow.

| Setup | Measured / reported | Source |
| --- | --- | --- |
| Llama 3.2 3B, CPU-only, Intel Ultra 5 125H (a *newer* chip than the user's) | **~2 tok/s** | <https://collabnix.com/best-ollama-models-in-2025-complete-performance-comparison/> |
| Qwen 2.5 7B, same CPU-only setup | ~10 tok/s (reported; inconsistent with the 3B figure above, treat both as loose) | same |
| Llama 3.1 8B CPU-only, general estimate | 8–12 tok/s | same |
| Llama 3.2 3B Q4_K_M on an Intel *iGPU* (Iris Xe) | 20–30 tok/s | same |
| Llama 3.3 70B on **Groq** | **280–394 tok/s** | <https://www.aipricing.guru/groq-pricing/>, <https://tokenmix.ai/blog/groq-api-pricing> |
| Groq free tier | 30 RPM / 6,000 TPM / 1,000 RPD, no credit card | <https://tokenmix.ai/blog/groq-free-tier-limits-2026> |
| Cerebras free tier | ~30 RPM / 1,000 RPD, **1M tokens/day**, smaller catalogue | <https://tokenmix.ai/blog/groq-free-tier-limits-2026> |

The published CPU numbers disagree with each other (2 tok/s for a 3B and 10 tok/s for a 7B
cannot both be right), so treat the honest range for a **3B at Q4 on the user's 2019 i9 as
roughly 5–15 tok/s**, worse once the fans ramp and the chip throttles.

**What that means for an agent loop.** Otto's supervisor does a plan call plus one call per
step plus a summary — call it 4 model calls for "open my project and run the tests", each
emitting 100–300 tokens of JSON. At 8 tok/s that is **50–150 seconds of pure generation**,
during which the fans are audible. At Groq's ~300 tok/s the same loop is **2–4 seconds**.

**Recommendation, stated plainly in SETUP.md:** use a **hybrid**. Point the Planner and
Supervisor at a fast cloud endpoint (Groq or Cerebras free tier) and let simple, frequent,
one-shot commands ("open Safari") be handled by the **rule-based fast path that needs no
model at all** — Otto ships a deterministic intent matcher so the most common commands
never touch an LLM, which is worth more on this hardware than any model choice. Local
Ollama with `qwen2.5:3b` or `llama3.2:3b` remains fully supported and is the right choice
when the user wants zero network, with the latency stated up front, not discovered.

---

## 7. Wake word / always-listening — rejected for v1

Wake-word engines themselves are cheap: Porcupine is cited at **<1.4 MB RAM and <8% of one
Raspberry Pi 3 core**, and openWakeWord runs 15–20 models on a single Pi 3 core
(<https://picovoice.ai/blog/complete-guide-to-wake-word/>,
<https://medium.com/@alirezakenarsarianhari/yet-another-wake-word-detection-engine-a2486d36d8d4>).

The cost is not the model, it is **everything around it**: an open microphone stream and an
inference tick running forever on a laptop that throttles and whose fans are audible, plus
a permanently-held mic indicator in the menu bar, plus false triggers. The budget says
"~0% idle CPU", and always-on listening is definitionally not 0%.

**v1 is push-to-talk only.** This is a deliberate design choice, stated as such in the
README, not a missing feature. The hook to add a wake word later exists (the audio capture
layer is behind an interface) but it is not wired up.

---

## Summary of choices

| Concern | Choice | Because |
| --- | --- | --- |
| Menu bar | `rumps` / PyObjC | Tens of MB, no toolchain, one process |
| ASR | `faster-whisper` `base`(default)/`tiny`, int8, **lazy + unload after 5 min idle** | No torch, ~78 MB weights at tiny, well inside 2 s |
| TTS | macOS `say` | Zero RAM, zero deps, instant |
| Hotkey | `pynput` global hotkey, push-to-talk | No Xcode; self-diagnoses missing permissions |
| Automation | `osascript` + System Events, `on run argv` only | Semantic, no coordinates, injection-safe |
| LLM | Provider abstraction; **no-LLM rule-based fast path**; hybrid cloud-planner recommended | 3B on this CPU is 5–15 tok/s; a 4-call loop is a minute |
| Memory | SQLite + scoped `LIKE` | No embedding model, no vector DB |
| Wake word | None in v1 | Always-on listening violates the idle-CPU budget |
