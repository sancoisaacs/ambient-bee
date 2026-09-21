# 🐝 Ambient Bee v3 — ambient memory with a brain

v2 was a transcriber that appended to a daily .md. v3 is a **memory system**: everything lands in SQLite,
an Obsidian vault is regenerated from it, and you can *talk to it*.

## What changed (v2 → v3)

| v2 | v3 |
|---|---|
| Append-only daily `.md` | **SQLite** (`bee.db`) as the source of truth; markdown is a *view* rebuilt every run |
| Whisper ran **twice** per file (transcribe + diarize) | One pass — transcript + diarization from the same segments (~2× faster on phone CPU) |
| Failed Mistral call → garbage JSON written, file moved to _DONE forever | Failure tracked per file, **auto-retried** up to 3 runs, never lost |
| Timestamp = when cron ran (1 AM) | Timestamp = **when it was recorded** (parsed from filename, else mtime) |
| Todos scattered in JSON blobs | **Todo ledger** with IDs, dedup, `--done N`, open/closed |
| — | **Wake words**: say *"bee todo ..."*, *"bee note ..."*, *"bee remember ..."* into the Flex → captured verbatim, flagged 🐝 |
| — | **`--ask "..."`**: BM25 search over all memory (zero deps) → LLM answers with dates |
| — | **`--brief`**: morning brief (yesterday + open todos), cron 06:30, optional TTS + notification |
| — | **`--digest`**: weekly rollup → `Weekly/2026-W39.md`, cron Sunday 18:00 |
| — | **People pages** auto-built: `[[Rob]]` → every day Rob was mentioned |
| — | Obsidian-compatible: frontmatter, `[[wikilinks]]`, `bee/tag` tags, collapsible transcripts |
| — | `--stats` (audio hours, whisper/LLM seconds per run, failures) |
| — | `--migrate-v2 DIR` imports your old `_AMBIENT_MEMORY` logs — nothing lost |

## Install

```bash
termux-setup-storage
pkg update && pkg install python git ffmpeg -y
cp ambient_bee.py ~/ambient_bee.py
echo 'export MISTRAL_API_KEY=your_key' >> ~/.bashrc
source ~/.bashrc
```

### faster-whisper on Termux / Android

The Whisper installation uses a local PyAV build and installs `faster-whisper` without pip dependency resolution.

```bash
pip install "Cython<3.0"
pip install av --no-binary av
pip install faster-whisper==1.2.1 --no-deps
```

Verify:

```bash
python -c "import av; print('PyAV:', av.__version__)"
python -c "import faster_whisper; print('faster-whisper: OK')"
```

Then run the normal Ambient Bee setup:

```bash
python ~/ambient_bee.py --setup
```

`--setup` checks the existing Android-compatible installation and will not replace it with the normal `pip install faster-whisper` resolver path.

Do not replace the working commands above with:

```bash
pip install faster-whisper
```

The normal resolver attempts to install `ctranslate2` and other dependencies independently and can fail on Python 3.14 / ARM64 Android.

Optional v2 migration:

```bash
python ~/ambient_bee.py --migrate-v2 /storage/emulated/0/Download/_AMBIENT_MEMORY
```

## Layout

```text
/storage/emulated/0/Download/AmbientBee/
├── bee.db                 ← source of truth (also mirrored to Drive /_db)
├── _done/                 ← processed audio, safe to delete
└── vault/                 ← open this folder in Obsidian (mobile or desktop via Drive)
    ├── Index.md
    ├── Todos.md
    ├── Daily/2026-09-20.md
    ├── People/Rob.md
    └── Weekly/2026-W39.md
```

Override with `export BEE_HOME=/some/path`.

## Commands

```text
--run-once        ingest new audio → retry failures → rebuild vault → rclone sync → notify
--watch           same, continuously
--ask "q"         "what did I promise Rob"  (add --speak for TTS)
--brief           yesterday + open todos   (--speak / --notify)
--digest          weekly rollup
--todos / --done 12
--stats
--rebuild         regenerate vault from DB (safe any time)
```

## Cron (added by --setup)

```text
0 1  * * *  python ~/ambient_bee.py --run-once
30 6 * * *  python ~/ambient_bee.py --brief --notify
0 18 * * 0  python ~/ambient_bee.py --digest
```

## Widgets (added by --setup)

🐝 Run Bee · ☀️ Brief (spoken) · ❓ Ask Bee (speech → answer spoken back) · ✅ Todos · ☁️ Sync

## Wake words

Speak naturally, then:
- *"bee todo call Ryan about the WBR"* → todo #N, flagged 🐝 explicit, survives even if the LLM summary misses it
- *"bee remember Misaki wants gucchimi columns first"* → kept verbatim under **Said to Bee**
- *"bee note ..."* / *"bee follow up ..."* → same

Regex is lenient about "hey bee", commas, and pauses.

## Morning flow

```text
06:30  notification: "3 recordings yesterday · 5 open todos"
       tap ☀️ Brief → phone reads it to you
       tap ❓ Ask Bee → "what did Rob ask for" → answered from memory
```

Or open Claude with Drive MCP → `vault/Daily/YYYY-MM-DD.md` — still works, now much richer.

## Env vars

```text
MISTRAL_API_KEY        required for LLM extraction
BEE_MODEL              default: mistralai/mistral-large-2512
BEE_BASE_URL           default: https://api.xkiro.com/v1/chat/completions
BEE_WHISPER_MODEL      default: small
BEE_GDRIVE_REMOTE      default: gdrive:AmbientBee
BEE_HOME               default: /storage/emulated/0/Download/AmbientBee
```

## Troubleshooting

**Whisper slow** → `export BEE_WHISPER_MODEL=tiny.en`

**Whisper install fails on Android/Python 3.14** → use the Termux installation sequence in the Install section exactly:

```bash
pip install "Cython<3.0"
pip install av --no-binary av
pip install faster-whisper==1.2.1 --no-deps
```

**Do not run `pip install faster-whisper` afterward**, because that invokes the normal dependency resolver again.

**No recordings found** → record once in Samsung Voice Recorder; Bee searches the known recording locations.

**Mistral errors** → check `echo $MISTRAL_API_KEY`.

**Drive not syncing** → `rclone config` → create the `gdrive` remote.

**Cron not running** → `sv-enable crond && sv up crond`.

**Rebuild Markdown** → `python ~/ambient_bee.py --rebuild`.

## Architecture

```text
Samsung Voice Recorder
        ↓
     audio file
        ↓
   faster-whisper
        ↓
 transcript + diarization
        ↓
      Mistral
        ↓
      SQLite
        ↓
   Obsidian vault
        ↓
      rclone
```

SQLite remains the source of truth. Markdown is a regenerated view rebuilt from the database.
