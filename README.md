# 🐝 Ambient Bee v3 — ambient memory with a brain

v3 is a memory system: everything lands in SQLite, an Obsidian vault is regenerated from it, and you can talk to it.

## Android transcription engine

Ambient Bee v3 uses the **native `whisper.cpp` HTTP server already built for Termux/Android**. It does not require the Python `faster-whisper`, CTranslate2, or Tokenizers stack.

The normal run flow is:

```text
recording → FFmpeg 16 kHz mono WAV → whisper.cpp :8080 → Mistral → SQLite → Obsidian vault → rclone
```

When `--run-once` starts, Ambient Bee automatically starts `whisper-server` if it is not already running. It waits for the server to become ready, processes the audio files, and stops **only the server instance it started** when the run finishes. If you already have whisper.cpp running on port 8080, Ambient Bee reuses it and leaves it running.

For `--watch`, Ambient Bee starts the server once and keeps it running for the lifetime of the watcher. Press `Ctrl+C` to stop the watcher and its server if Ambient Bee started it.

## What changed (v2 → v3)

| v2 | v3 |
|---|---|
| Append-only daily `.md` | **SQLite** (`bee.db`) as the source of truth; markdown is a *view* rebuilt every run |
| Whisper ran twice per file | One Whisper pass — transcript + segment timing from the same response |
| Python `faster-whisper` dependency stack | **Native whisper.cpp server** on Android/ARM64 |
| Failed Mistral call → garbage JSON written, file moved to _DONE forever | Failure tracked per file, **auto-retried** up to 3 runs, never lost |
| Timestamp = when cron ran | Timestamp = **when it was recorded** (parsed from filename, else mtime) |
| Todos scattered in JSON blobs | **Todo ledger** with IDs, dedup, `--done N`, open/closed |
| — | **Wake words**: say *"bee todo ..."*, *"bee note ..."*, *"bee remember ..."* into the Flex → captured verbatim, flagged 🐝 |
| — | **`--ask "..."`**: BM25 search over all memory (zero deps) → LLM answers with dates |
| — | **`--brief`**: morning brief (yesterday + open todos), cron 06:30, optional TTS + notification |
| — | **`--digest`**: weekly rollup → `Weekly/YYYY-Www.md`, cron Sunday 18:00 |
| — | **People pages** auto-built |
| — | Obsidian-compatible: frontmatter, `[[wikilinks]]`, `bee/tag` tags, collapsible transcripts |
| — | `--stats` (audio hours, Whisper/LLM seconds per run, failures) |
| — | `--migrate-v2 DIR` imports old `_AMBIENT_MEMORY` logs |

## Install

```bash
termux-setup-storage
pkg update && pkg install python ffmpeg git -y
cp ambient_bee.py ~/ambient_bee.py
echo 'export MISTRAL_API_KEY=your_key' >> ~/.bashrc && source ~/.bashrc
```

Make sure your native whisper.cpp server and model exist at the defaults:

```text
~/whisper.cpp/build/bin/whisper-server
~/whisper.cpp/models/ggml-tiny.en.bin
```

Test the server manually if needed:

```bash
cd ~/whisper.cpp
./build/bin/whisper-server -m models/ggml-tiny.en.bin --host 127.0.0.1 --port 8080
```

Then run:

```bash
python ~/ambient_bee.py --setup
python ~/ambient_bee.py --run-once
```

`--setup` checks for the native whisper.cpp binary/model, FFmpeg, Termux API, rclone, cron, and Python `requests`. It **does not install `faster-whisper`**.

Optional migration:

```bash
python ~/ambient_bee.py --migrate-v2 /storage/emulated/0/Download/_AMBIENT_MEMORY
```

## Whisper configuration

Defaults:

```text
Server: 127.0.0.1:8080
Binary: ~/whisper.cpp/build/bin/whisper-server
Model:  ~/whisper.cpp/models/ggml-tiny.en.bin
```

Override them if required:

```bash
export BEE_WHISPER_SERVER=/path/to/whisper-server
export BEE_WHISPER_MODEL_PATH=/path/to/ggml-model.bin
export BEE_WHISPER_HOST=127.0.0.1
export BEE_WHISPER_PORT=8080
export BEE_WHISPER_TIMEOUT=900
```

Ambient Bee normalizes `.m4a`, `.wav`, `.mp3`, `.3ga`, and `.ogg` recordings with FFmpeg to 16 kHz mono PCM WAV before sending them to `/inference` with `response_format=verbose_json`.

The returned `text`, `duration`, and timestamped `segments` are converted into Ambient Bee's existing transcript/diarized representation. The rest of the v3 pipeline is unchanged.

## Layout

```text
/storage/emulated/0/Download/AmbientBee/
├── bee.db                 ← source of truth (also mirrored to Drive /_db)
├── _done/                 ← processed audio, safe to delete
└── vault/                 ← Obsidian-compatible vault
    ├── Index.md
    ├── Todos.md
    ├── Daily/YYYY-MM-DD.md
    ├── People/Name.md
    └── Weekly/YYYY-Www.md
```

Override with:

```bash
export BEE_HOME=/some/path
```

## Commands

```text
--setup            check dependencies, rclone, cron, widgets
--check            dependency/config check
--run-once         ingest new audio → Whisper → Mistral → rebuild vault → sync → notify
--watch            same continuously; Whisper server stays alive while watching
--transcribe FILE  process one audio file
--ask "q"          search memory and answer
--brief            yesterday + open todos
--digest           weekly rollup
--todos / --done N todo ledger
--stats            database/run statistics
--rebuild          regenerate vault from SQLite
--migrate-v2 DIR   import old v2 memory logs
```

## Cron

```text
0 1  * * *  python ~/ambient_bee.py --run-once
30 6 * * *  python ~/ambient_bee.py --brief --notify
0 18 * * 0  python ~/ambient_bee.py --digest
```

## Wake words

Speak naturally, then:

- *"bee todo follow up on the MBR"* → todo #N, flagged 🐝 explicit
- *"bee remember the agreed layout"* → kept verbatim under **Said to Bee**
- *"bee note ..."* / *"bee follow up ..."* → same

## Environment variables

| Variable | Default |
|---|---|
| `MISTRAL_API_KEY` | — |
| `BEE_MODEL` | `mistralai/mistral-large-2512` |
| `BEE_BASE_URL` | `https://api.xkiro.com/v1/chat/completions` |
| `BEE_WHISPER_SERVER` | `~/whisper.cpp/build/bin/whisper-server` |
| `BEE_WHISPER_MODEL_PATH` | `~/whisper.cpp/models/ggml-tiny.en.bin` |
| `BEE_WHISPER_HOST` | `127.0.0.1` |
| `BEE_WHISPER_PORT` | `8080` |
| `BEE_WHISPER_TIMEOUT` | `900` seconds |
| `BEE_GDRIVE_REMOTE` | `gdrive:AmbientBee` |
| `BEE_HOME` | `/storage/emulated/0/Download/AmbientBee` |

## Troubleshooting

**Whisper server won't start**

Check:

```bash
ls -l ~/whisper.cpp/build/bin/whisper-server
ls -lh ~/whisper.cpp/models/ggml-tiny.en.bin
```

Then test manually:

```bash
cd ~/whisper.cpp
./build/bin/whisper-server -m models/ggml-tiny.en.bin --host 127.0.0.1 --port 8080
```

**Port 8080 is already in use**

That's okay if it is your whisper.cpp server. Ambient Bee detects the running server and reuses it. Otherwise set another port with `BEE_WHISPER_PORT` and start the server on that port.

**Audio fails to decode**

Ambient Bee converts source recordings through FFmpeg before Whisper. Test manually:

```bash
ffmpeg -i "/path/to/file.m4a" -ar 16000 -ac 1 -c:a pcm_s16le /tmp/test.wav
```

**File keeps failing**

`--stats` shows attempts. After 3 attempts a file is skipped until its attempts are reset:

```bash
sqlite3 bee.db "UPDATE recordings SET attempts=0 WHERE status='failed'"
```

**Vault looks wrong**

```bash
python ~/ambient_bee.py --rebuild
```

SQLite is the source of truth; the vault is disposable.

**rclone auth expired**

```bash
rclone config reconnect gdrive:
```

Note: `rclone sync` mirrors vault → Drive and can delete remote files not present in the vault. The database is copied separately to `.../_db`.
