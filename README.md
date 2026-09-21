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
pkg update && pkg install python git -y
cp ambient_bee.py ~/ambient_bee.py
echo 'export MISTRAL_API_KEY=your_key' >> ~/.bashrc && source ~/.bashrc
python ~/ambient_bee.py --setup          # deps, rclone check, 3 cron jobs, 5 widgets
python ~/ambient_bee.py --migrate-v2 /storage/emulated/0/Download/_AMBIENT_MEMORY   # optional
```

## Layout

```
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

```
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

```
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

```
06:30  notification: "3 recordings yesterday · 5 open todos"
       tap ☀️ Brief → phone reads it to you
       tap ❓ Ask Bee → "what did Rob ask for" → answered from memory
```

Or open Claude with Drive MCP → `vault/Daily/YYYY-MM-DD.md` — still works, now much richer.

## Env vars

| var | default |
|---|---|
| `MISTRAL_API_KEY` | — |
| `BEE_MODEL` | `mistralai/mistral-large-2512` |
| `BEE_BASE_URL` | xkiro endpoint |
| `BEE_WHISPER_MODEL` | `small` (use `tiny.en` if slow) |
| `BEE_GDRIVE_REMOTE` | `gdrive:AmbientBee` |
| `BEE_HOME` | `/storage/emulated/0/Download/AmbientBee` |

## Troubleshooting

- **Whisper slow** → `export BEE_WHISPER_MODEL=tiny.en`
- **File keeps failing** → `--stats` shows attempts; after 3 it's skipped. Fix key/network, then `sqlite3 bee.db "UPDATE recordings SET attempts=0 WHERE status='failed'"`
- **Vault looks wrong** → `--rebuild` (DB is truth, vault is disposable)
- **rclone auth expired** → `rclone config reconnect gdrive:`
- **Note: `rclone sync` mirrors vault → Drive (deletes remote files not in vault). DB goes to `.../_db` via copy.**
1 AM:  cron triggers --run-once
       → transcribe all new files (Whisper, local, offline)
       → extract memory/todos (Mistral API)
       → append to /Download/_AMBIENT_MEMORY/YYYY-MM-DD.md
       → rclone sync → gdrive:AmbientBee/
       → move audio → /Download/_AMBIENT_DONE/
       → termux-notification "X recordings processed"

Morning: open Claude → "summarise my Ambient Bee log from last night"
         → Drive MCP reads the .md → done
```

---

## File locations

| Path | What |
|------|------|
| `/storage/emulated/0/Download/_AMBIENT_MEMORY/` | Daily `.md` logs |
| `/storage/emulated/0/Download/_AMBIENT_DONE/` | Processed audio (safe to delete) |
| `gdrive:AmbientBee/` | Mirror in Google Drive |
| `~/.shortcuts/` | Termux widget scripts |

---

## Troubleshooting

**"No recordings folder found"** — open Samsung Voice Recorder and make one recording first; the folder only exists after first use.

**Whisper very slow** — switch to `tiny.en`. First run downloads the model (~150MB for small).

**rclone auth expired** — run `rclone config reconnect gdrive:` to refresh OAuth.

**Cron not running** — check `sv status crond`; if down, `sv up crond`.

**Termux killed overnight** — acquire wake lock: `termux-wake-lock` before long runs (--run-once does this automatically).
