# Setting up Otto — about five minutes

Written for a 2019 Intel MacBook Pro. Every step assumes you have not installed
anything yet.

---

## 1. Install (about two minutes)

```zsh
cd ~/otto          # wherever you cloned this
./setup.sh
```

`setup.sh` is safe to run again at any time. It creates a `.venv`, installs a short
list of pre-built wheels (nothing compiles, nothing needs Rust or Xcode), offers to
download the speech model, runs the test suite, and prints an environment report.

If it finishes with anything in yellow, that is a warning, not a failure — Otto runs
without every optional piece and the report says what is missing.

## 2. Start it

```zsh
./run.sh
```

A 🎙 appears in your menu bar. That icon *is* the app.

**Otto works right now, before you install any AI model.** Try, from the menu's
"Type a command…":

- `open Safari`
- `create a folder called Test on my Desktop`
- `remember that my projects live in ~/Projects`
- `what do you remember`

If you would rather stay in the terminal, `./run.sh --repl` gives you the same Otto
with a prompt instead of a menu bar.

---

## 3. Grant the three macOS permissions

This is the step that most often goes wrong, so read the detail.

**Grant them to the application that launches Otto** — Terminal.app, or iTerm, or
whichever terminal you used. macOS attaches these permissions to the *running
process's* app, not to Otto's files. And **macOS only reads them at process start**,
so after granting one, quit Otto (menu → Quit Otto) and run `./run.sh` again.

### Accessibility — required to drive other apps

*System Settings → Privacy & Security → Accessibility*

You will see a list with toggles. Click the **+** button, navigate to
`/Applications/Utilities/Terminal.app` (or your terminal), add it, and make sure its
toggle is **on**. If your terminal is already listed but off, switching it on is
enough.

**What you will actually see the first time Otto tries to click something:** a dialog
headed *"Terminal.app" wants access to control "System Events"* with **Don't Allow**
and **OK**. Choose **OK**. If you have already clicked Don't Allow once, macOS will
not ask again — reset it with:

```zsh
tccutil reset AppleEvents
tccutil reset Accessibility
```

and relaunch Otto.

### Input Monitoring — required for the push-to-talk hotkey

*System Settings → Privacy & Security → Input Monitoring*

Same idea: **+**, add your terminal, toggle **on**, relaunch.

**This one fails silently.** Without Input Monitoring the hotkey listener starts
without any error and then simply never fires — Otto looks fine and does nothing when
you press the key. That is why Otto has a menu item **"Check permissions…"** that
tells you straight out whether it is trusted.

### Microphone — required to hear you

*System Settings → Privacy & Security → Microphone*

macOS normally prompts the first time you record: *"Terminal.app" would like to access
the microphone*, with **Don't Allow** and **OK**. Choose **OK**.

**If no prompt appears**, add your terminal manually with **+**. If Otto says
"I didn't hear anything" every time, that is this permission — a denied microphone
returns silence rather than an error, and Otto tells you so rather than blaming the
speech model.

### Checking

```zsh
./run.sh --check
```

or the **Check permissions…** menu item. Both say which are granted.

---

## 4. Talk to it

Press **⌃⌥Space** (Control-Option-Space). The icon turns 🔴. Speak. Press it again.
Otto transcribes, acts, and tells you what it did out loud.

It is **push-to-talk on purpose**. Otto is not listening the rest of the time — there
is no wake word and no open microphone, which is the main reason it costs nothing when
idle on a laptop that throttles. See README → "Deliberate omissions".

To change the key, edit `~/.otto/config.json`:

```json
{ "hotkey": "<ctrl>+<alt>+space" }
```

---

## 5. Adding a language model — the honest version

Otto handles the common commands without any model. A model is only needed for
requests that have to be *planned* ("open my project, run the tests, and tell me what
broke").

On your machine this choice matters more than usual, so here are the numbers:

| Option | Speed on a 2019 i9 | Privacy | Cost |
| --- | --- | --- | --- |
| **No model** (default) | instant | total | free |
| **Ollama, `qwen2.5:3b`** | ~5–15 tokens/sec → a 4-call agent loop takes **50–150 seconds**, fans audible | total, nothing leaves the Mac | free |
| **Groq / Cerebras free tier** | ~300 tokens/sec → the same loop takes **2–4 seconds**, Mac stays cool | your *text* leaves the machine; audio and file contents never do unless you turn that on | free tier, no card |

### The recommendation: a hybrid

Leave the common commands on the no-model fast path, and point the planner at a fast
cloud endpoint. That is the combination that feels good on this hardware.

**Groq** (fastest free tier, no credit card):

1. Sign up at <https://console.groq.com>, create an API key.
2. Store it in your Keychain — never in a file:
   ```zsh
   security add-generic-password -U -s otto-assistant -a groq -w 'YOUR_KEY_HERE'
   ```
3. Edit `~/.otto/config.json`:
   ```json
   {
     "providers": {
       "fast":   {"kind": "groq", "model": "llama-3.1-8b-instant",
                  "api_key_name": "groq"},
       "strong": {"kind": "groq", "model": "llama-3.3-70b-versatile",
                  "api_key_name": "groq"}
     }
   }
   ```
4. Restart Otto. The menu will show **Model: llama-3.3-70b-versatile ☁️** — the cloud
   icon is always there when a cloud model is in use.

**Cerebras** is the same shape with `"kind": "cerebras"` and a key from
<https://cloud.cerebras.ai>; its free tier is more generous on daily volume.

**Fully local instead:**

```zsh
brew install ollama
ollama pull qwen2.5:3b
```

```json
{
  "providers": {
    "fast":   {"kind": "ollama", "model": "qwen2.5:3b"},
    "strong": {"kind": "ollama", "model": "qwen2.5:3b"}
  }
}
```

Nothing leaves your Mac. It is slower; you now know by how much.

**Mixing the two** is the point of having two tiers: `ollama` for `fast`, Groq for
`strong`, or the reverse. Also supported: `openai`, `anthropic`, `gemini`, `lmstudio`,
`llamacpp`, `openrouter`, `together`, or any OpenAI-compatible URL via
`{"kind": "openai_compatible", "base_url": "..."}`.

---

## 6. Everything you can change

`~/.otto/config.json`, all optional:

| Key | Default | What it does |
| --- | --- | --- |
| `hotkey` | `<ctrl>+<alt>+space` | Push-to-talk key |
| `asr_model` | `base` | `tiny` is faster and lighter, `base` is more accurate |
| `asr_idle_unload_seconds` | `300` | How long before the speech model is dropped from RAM |
| `tts` | `say` | `say`, `piper`, or `none` |
| `tts_voice` | *(system)* | e.g. `Daniel`, `Samantha` — `say -v ?` lists them |
| `speak_results` | `true` | Whether Otto talks back |
| `fast_path` | `true` | The no-model command matcher |
| `workspace_roots` | Desktop, Documents, Downloads, Projects | The **only** folders Otto may touch |
| `max_parallel` | `4` | Concurrent plan steps |
| `console_port` | `8787` | Developer console, bound to 127.0.0.1 |

## 7. Where Otto keeps things

| Path | What |
| --- | --- |
| `~/.otto/config.json` | Settings. Never contains a key. |
| `~/.otto/memory.sqlite3` | What Otto remembers. Readable with any SQLite tool. |
| `~/.otto/audit.jsonl` | Every tool call, including refused ones, secrets redacted. |
| Keychain, service `otto-assistant` | API keys. |

To make Otto forget everything: open the developer console (menu → *What Otto
remembers…*), where every row is listed with a delete button. Or delete
`~/.otto/memory.sqlite3`.

---

## 8. When something does not work

| Symptom | Cause | Fix |
| --- | --- | --- |
| Hotkey does nothing, no error | Input Monitoring not granted | §3, then **relaunch** |
| "Otto is not allowed assistive access" | Accessibility not granted | §3, then **relaunch** |
| "I didn't hear anything" every time | Microphone denied | §3; `tccutil reset Microphone` |
| No menu-bar icon | `rumps`/PyObjC missing | `./run.sh --check`; use `./run.sh --repl` meanwhile |
| First voice command is slow | The speech model is loading | Only the first one after five idle minutes |
| "No language model is configured" | Expected | §5 — or stick to simple commands |
| A folder is "outside the allowed folders" | Deliberate | Add it to `workspace_roots` |
| Fans spin up on every request | A local model is planning | Switch the `strong` tier to Groq (§5) |
