# 🐝 Ambient Bee v2

DIY ambient memory — Beats Flex + Samsung Voice Recorder + Termux + Claude

**Repo:** https://github.com/sancoisaacs/ambient-bee

---

## Install (one paste into Termux)

```bash
pkg install python -y && python -c "import urllib.request; urllib.request.urlretrieve('https://raw.githubusercontent.com/sancoisaacs/ambient-bee/main/ambient_bee.py', '/data/data/com.termux/files/home/ambient_bee.py')" && python ~/ambient_bee.py --setup
```

That's it. Setup handles everything else interactively.

---

## First time in Termux (manual alternative)

```bash
# 1. Give Termux storage access (one-time)
termux-setup-storage

# 2. Set your API key (add to ~/.bashrc to persist)
export MISTRAL_API_KEY=your_key_here

# 3. Run setup — checks and installs everything
python ~/ambient_bee.py --setup
```

Setup will walk you through:
- ffmpeg, rclone, termux-api, cronie (pkg)
- faster-whisper, requests (pip)
- rclone Google Drive config
- 1 AM cron job
- Termux widget shortcuts (~/.shortcuts/)

---

## rclone Google Drive (if not done yet)

```bash
rclone config
# → n (new remote)
# → name: gdrive
# → type: drive (Google Drive)
# → leave client_id blank → enter
# → scope: 1 (full access)
# → follow browser OAuth flow
```

Test it:
```bash
rclone lsd gdrive:
```

---

## Cron (1 AM auto-run)

```bash
# Enable cron daemon
sv-enable crond
sv up crond

# Add job (--setup does this, but manual option:)
crontab -e
# Add line:
# 0 1 * * * python /data/data/com.termux/files/home/ambient_bee.py --run-once
```

---

## Termux Widgets

Install **Termux:Widget** from F-Droid.

`--setup` creates these in `~/.shortcuts/`:

| Widget | Does |
|--------|------|
| 🐝 Run Bee | `--run-once`: process + sync + exit |
| ☁️ Sync Drive | rclone copy to Drive |
| 📋 Today Log | toast with today's summary |
| 🗑 Clear Done | wipe _DONE folder |

Long-press home screen → Widgets → Termux Widget → drag to home.

---

## Samsung Voice Recorder tips

- Settings → **Record via Bluetooth** → ON
- This lets Beats Flex be the mic while phone is in pocket/locked
- Voice Recorder keeps recording through Doze — native app advantage

---

## Whisper model sizes (trade speed vs quality)

```bash
# Faster, smaller — good for clear Beats Flex audio
export BEE_WHISPER_MODEL=tiny.en

# Balanced — default
export BEE_WHISPER_MODEL=small

# Better quality, slower on phone CPU
export BEE_WHISPER_MODEL=base.en
```

Add to `~/.bashrc` to persist.

---

## Daily flow

```
Night: phone records ambient audio via Beats Flex
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
