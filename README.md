# transmizer

Local audio transcription + summarization. Drop recordings in, get structured markdown notes out. No cloud, no accounts, no data leaving your machine.

## How it works

1. **Transcribe** — [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (OpenAI Whisper running locally) converts audio to text
2. **Classify** — meeting type is inferred from the transcript automatically (or you can force it)
3. **Summarize** — [Ollama](https://ollama.com) runs a local LLM against a prompt template from `prompts/`
4. **Output** — a structured markdown file lands in `output/`

## Requirements

- Python 3.11+
- [ffmpeg](https://ffmpeg.org) on your PATH — `winget install ffmpeg`
- [Ollama](https://ollama.com) running with at least one model pulled — `ollama pull llama3.2`

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Process everything in input/
python transmizer.py

# Specific file(s)
python transmizer.py recording.m4a

# Force a meeting type (skips inference)
python transmizer.py --type medical appointment.wav

# Watch input/ and process new files as they arrive
python transmizer.py --watch

# Transcribe only — no summarization
python transmizer.py --transcript-only

# Don't append the raw transcript to the output
python transmizer.py --no-transcript
```

## HiDock P1 auto-sync

`p1_sync.py` pulls recordings straight off a HiDock P1 over USB (the same protocol hinotes.com uses, no browser needed) and runs each one through transmizer. The device is only read from — nothing on it is changed or deleted.

```bash
python p1_sync.py            # sync once now
python p1_sync.py --status   # what has been downloaded / processed
python p1_sync.py --watch    # stay running; sync whenever the P1 is plugged in

.\install_p1_watcher.ps1             # run --watch hidden at every logon (Task Scheduler)
.\install_p1_watcher.ps1 -Uninstall
```

- Settings are under `p1_sync:` in `config.yaml` (`since`, `output_dir`, `delete_audio_after_success`, `resync_minutes`).
- `p1_state.json` remembers which recordings are done. Delete an entry to reprocess that recording.
- Each recording becomes its own note (no session merging), with the meeting type inferred from the transcript.
- A Windows toast appears when a download starts and when notes are ready; click it to open the note.
- Logs: `logs/p1_sync.log` (sync runs) and `logs/p1_watch.log` (watcher).
- Only one program can hold the device: close any hinotes.com tab before plugging in, or the sync reports it as busy.

## Smart merge (for split recordings)

When multiple files are detected, transmizer automatically groups them by timestamp and merges them into one session. The merge happens at the audio level — ffmpeg concatenates the files before transcription, so Whisper sees the full context without artificial breaks.

```bash
# Merge is the default — just run it
python transmizer.py

# Opt out: process each file independently
python transmizer.py --no-merge

# Tighten the grouping window (default: 60 min)
python transmizer.py --merge-gap 30

# Ask the LLM to verify files are from the same session (only used without ffmpeg)
python transmizer.py --merge-llm-check
```

Files are grouped by the timestamp in their filename (e.g. `20240130024338.wav`) or their modification time. Files within the gap window are treated as one session. ffmpeg must be on your PATH for audio-level merging — install with `winget install ffmpeg`.

## Meeting types

Type is resolved in this order:

| Priority | Source | Example |
|---|---|---|
| 1 | `--type` flag | `--type medical` |
| 2 | Filename pattern | `1on1_alice.m4a`, `doctor_checkup.wav` |
| 3 | LLM inference | reads first ~300 words of transcript |
| 4 | Fallback | `general` |

**Filename patterns:**

| Pattern | Type |
|---|---|
| `1on1_*`, `oneone_*`, `1-on-1_*` | `1on1` |
| `doctor_*`, `medical_*`, `clinic_*`, `appt_*` | `medical` |
| `meeting_*`, `sync_*`, `standup_*`, `retro_*`, `team_*` | `work` |

## Prompt templates

Each meeting type has a prompt in `prompts/`:

```
prompts/
  work.md       team meetings, standups, syncs
  1on1.md       one-on-one conversations
  medical.md    doctor visits, appointments
  general.md    catch-all fallback
```

Edit these freely — they're plain markdown with two placeholders:

- `{date}` — today's date (YYYY-MM-DD)
- `{transcript}` — the full transcript text

**Adding a new type:** create `prompts/lecture.md` and the LLM classifier will automatically start detecting and routing to it.

## Configuration

`config.yaml` controls everything:

```yaml
whisper:
  model: base.en      # tiny.en → large-v3, bigger = slower but more accurate
  device: auto        # auto / cuda / cpu
  compute_type: auto

ollama:
  host: http://localhost:11434
  model: qwen3-coder:30b   # any model you have pulled
  timeout: 600             # seconds
  num_ctx: 32768           # token context window

output:
  include_transcript: true       # append full transcript to output
  transcript_collapsible: true   # wrap it in a <details> block

merge:
  gap_minutes: 60    # max gap between files in the same session
  llm_check: false   # verify relevance before merging
```

### Whisper model guide

| Model | Size | Speed | Notes |
|---|---|---|---|
| `tiny.en` | ~75 MB | fastest | rough, good for drafts |
| `base.en` | ~145 MB | fast | good default |
| `small.en` | ~480 MB | moderate | noticeably better accuracy |
| `medium.en` | ~1.5 GB | slow | great for accented speech or noise |
| `large-v3` | ~3 GB | slowest | best quality, multilingual |

Drop `.en` suffix for non-English audio.

## Output format

Each run produces one markdown file per session in `output/`. The filename comes from the H1 heading the LLM generates. Example for a work meeting:

```markdown
# 2024-01-30 Platform Team Weekly Sync

**Creation Time**: 2024-01-30

## 📅 About Meeting
## 📒 Meeting Outline
## 🔑 Key Decisions
## 📋 Overview
## 🎯 Todo List

<details>
<summary>📜 Full Transcript</summary>
...
</details>
```

For a 1:1: `💭 Top of mind`, `🏆 Achievements`, `🚧 Blockers`, `🖇️ Feedback`, `📖 Growth`, `🎯 Next Milestone`

For medical: `🩺 Reason for Visit`, `📊 Assessment`, `💊 Treatment`, `📋 Instructions`, `🔄 Follow-up`, `❓ Q&A`, `🎯 Next Steps`
