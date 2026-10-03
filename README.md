# Moodle Quiz Solver

Multi-agent Moodle quiz automation with majority voting, a desktop control panel,
Telegram/Discord remote control, and continuous attempt monitoring.

**Author:** Kemus — https://github.com/STLKem

---

## Features

- Solve quizzes by Moodle `view.php?id=...` URL
- Parallel AI agents with majority voting
- Desktop GUI (native window) or browser panel — keys, toggles, live logs, history
- `watch-attempts` — poll for unfinished attempts and solve them
- Telegram / Discord bots for remote start/stop/solve
- Optional Moodle chat bot
- Per-question debug artifacts under `logs/`

---

## Requirements

- **Python 3.11+**
- Windows / Linux / macOS
- Optional: Playwright Chromium (visual / scraper mode)

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

---

## Quick start

### 1) Create config

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` (Moodle URL, username, password, API keys), **or** fill everything later in the GUI **Settings** tab.

Interactive wizard (terminal):

```bash
python main.py setup
```

> **Never commit `config.yaml`.** It is gitignored and may contain passwords and API keys.

### 2) Pick how you want to run

| Mode | Command | Best for |
|------|---------|----------|
| **Desktop GUI** | `python main.py desktop` | Everyday use, no terminal |
| **Browser GUI** | `python main.py gui` | Same panel in the browser |
| **Windows exe** | `Release\MoodleSolver.exe` | After building with the script below |
| **Terminal / CLI** | `python main.py …` | Scripts, servers, automation |

---

## GUI (recommended)

### Desktop window

```bash
python main.py desktop
# or
python launch.py
```

Opens a native window with the full control panel.

### Browser

```bash
python main.py gui
```

Then open: **http://127.0.0.1:8787**

Optional flags:

```bash
python main.py gui --host 127.0.0.1 --port 8787 --config ./config.yaml
python main.py gui --desktop   # same as `desktop`
```

### Build Windows `.exe`

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_exe.ps1
```

Result:

```text
Release\MoodleSolver.exe
```

Copy the whole `Release\` folder (exe + `_internal` + optional `config.yaml`).  
Double-click `MoodleSolver.exe` to open the GUI.

### What each GUI tab does

| Tab | Purpose |
|-----|---------|
| **Dashboard** | Service status, quick solve, live log stream |
| **Solve** | Run one quiz by URL / `view.php?id`, watch solve log |
| **Services** | Start / stop **Watch**, **Remote bots**, **Chat bot** |
| **Settings** | Moodle login, all API keys, agent toggles, DeepSeek/OpenRouter model lists, solver / monitor / remote / chat options |
| **History** | Past attempts from `logs/attempt_*` |
| **Logs** | Files from `remote_runs/` (watch / solve / bots) |

Typical GUI workflow:

1. Open **Settings** → enter Moodle credentials and API keys → enable agents → **Save**
2. **Dashboard** or **Solve** → paste quiz URL / id → **Start solve**
3. Or **Services** → **Start** watch-attempts for continuous monitoring
4. Check **Logs** / **History** if something fails

---

## Terminal (CLI)

All commands use `config.yaml` by default (`--config` / `-c` to override).

### Setup wizard

```bash
python main.py setup
```

### Solve a quiz

```bash
python main.py solve --url "https://your-moodle/mod/quiz/view.php?id=123456"
```

Or by quiz id only (base URL taken from config):

```bash
python main.py solve --quiz-id 123456
```

Interactive quiz picker (no URL):

```bash
python main.py solve
```

### List quizzes

```bash
python main.py list-quizzes
```

### Watch unfinished attempts

One scan (exit code `1` if anything found):

```bash
python main.py watch-attempts --once
```

Continuous loop:

```bash
python main.py watch-attempts --watch
```

Useful flags:

```bash
python main.py watch-attempts --watch --auto-exclude
python main.py watch-attempts --watch --no-list-courses --config ./config.yaml
```

### Remote bots (Telegram + Discord)

```bash
python main.py remote-bots
```

Configure tokens and allowed user IDs in `config.yaml` → `remote:`  
(or in the GUI **Settings** → Remote bots).

DM commands:

| Message | Action |
|---------|--------|
| `start` | Start `watch-attempts` (single instance) |
| `stop` | Stop watch |
| `status` | Show watch status / log path |
| quiz URL or numeric id | One-off `solve` |

If `chat_bot.model` is set and `auto_start_with_remote_bots` is not `false`, the Moodle chat bot can start alongside remote-bots.

### Moodle chat bot

List conversations / chats:

```bash
python main.py list-chats
```

Run the bot:

```bash
python main.py chat-bot
python main.py chat-bot --conv-id 42073
python main.py chat-bot --chat-id 12345
```

Model comes from `chat_bot.model` in config (e.g. `deepseek-chat` or `groq_qwen3_32b`).

### Other commands

```bash
# Mark / inspect an attempt helper
python main.py mark-attempt --help

# Benchmark enabled models
python main.py benchmark-models

# GUI / desktop (see above)
python main.py gui
python main.py desktop
```

### Help

```bash
python main.py --help
python main.py solve --help
```

---

## Configuration tips

### API keys (one per provider)

| Provider | Config field | Get key |
|----------|--------------|---------|
| Cerebras | `agents.cerebras_api_key` | https://cloud.cerebras.ai/ |
| Groq | `agents.groq_api_key` | https://console.groq.com/ |
| Google Gemini | `agents.google_api_key` | https://aistudio.google.com/ |
| DeepSeek | `agents.deepseek_api_key` | https://platform.deepseek.com/ |
| OpenRouter | `agents.openrouter_api_key` | https://openrouter.ai/ |
| GitHub Models | `agents.github_api_key` | https://github.com/settings/tokens |

Enable models under `agents.enabled`, or via `deepseek_models` / `openrouter_models` lists (also editable in the GUI).

### Solver

```yaml
solver:
  auto_finish: false   # keep attempt open; submit manually in Moodle
  question_delay: 0
  show_reasoning: false
  logs_dir: logs
```

### Monitor

```yaml
monitor:
  poll_seconds: 60
  exclude_quiz_ids: []   # quiz ids to ignore while watching
```

---

## Logs & history

| Path | Contents |
|------|----------|
| `logs/attempt_<ID>/` | Per-question HTML, prompts, votes, optional screenshots |
| `logs/attempt_<ID>/attempt_summary.json` | Compact summary |
| `remote_runs/*.log` | Watch / solve / bots / chat process logs |

Browse them in the GUI (**History** / **Logs**) or open the folders directly.

---

## Project layout

```text
agents/               AI providers, registry, voting, orchestrator
moodle/               Moodle API + scraper
prompts/              Prompt templates
remote/               Telegram / Discord / process runners
gui/                  Control panel (API + static UI + desktop launcher)
packaging/            PyInstaller spec + build scripts
tests/                Health / utility scripts
main.py               CLI entry point
launch.py             Desktop entry (also used by the exe)
solver.py             Quiz solving pipeline
config.example.yaml   Template — copy to config.yaml
```

---

## Safety

- Do **not** commit `config.yaml` or `Release/` (both gitignored).
- Prefer `solver.auto_finish: false` and review answers in Moodle before submit.
- If a provider returns `401` / invalid key, update that key in Settings or `config.yaml`.

---

## License

Proprietary. See `LICENSE`. Do not use, copy, or distribute without permission from the author.
