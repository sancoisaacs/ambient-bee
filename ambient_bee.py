#!/usr/bin/env python3
"""
ambient_bee.py v2 — DIY ambient memory for Termux + Google Drive
────────────────────────────────────────────────────────────────
Hardware:  Beats Flex (neck mic) + Samsung Voice Recorder + Termux
Pipeline:  new recording → faster-whisper → Mistral → daily .md → rclone → Drive

QUICK START (run this first):
    python ambient_bee.py --setup

DAILY USE (via cron or Termux widget):
    python ambient_bee.py --run-once   # process everything new, sync, exit
    python ambient_bee.py --watch      # stay running, process as files arrive

SINGLE FILE:
    python ambient_bee.py --transcribe /sdcard/Recordings/Voice001.m4a
"""

import os, sys, time, json, subprocess, argparse, shutil, textwrap
from pathlib import Path
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────

API_KEY    = os.getenv("MISTRAL_API_KEY", "")          # export MISTRAL_API_KEY=sk-...
MODEL      = "mistralai/mistral-large-2512"
BASE_URL   = "https://api.xkiro.com/v1/chat/completions"
WHISPER_MODEL_SIZE = os.getenv("BEE_WHISPER_MODEL", "small")   # tiny.en / base.en / small
GDRIVE_REMOTE      = os.getenv("BEE_GDRIVE_REMOTE", "gdrive:AmbientBee")

REPO_URL     = "https://github.com/sancoisaacs/ambient-bee"
REPO_RAW_URL = "https://raw.githubusercontent.com/sancoisaacs/ambient-bee/main/ambient_bee.py"
BOOTSTRAP    = (
    "pkg install python -y && "
    f"python -c \"import urllib.request; "
    f"urllib.request.urlretrieve('{REPO_RAW_URL}', "
    "'/data/data/com.termux/files/home/ambient_bee.py')\" && "
    "python ~/ambient_bee.py --setup"
)

AUDIO_EXTENSIONS = {".m4a", ".wav", ".mp3", ".3ga", ".ogg"}

POSSIBLE_RECORD_DIRS = [
    Path("/storage/emulated/0/Voice Recorder"),
]

MEMORY_DIR = Path("/storage/emulated/0/Download/_AMBIENT_MEMORY")
DONE_DIR   = Path("/storage/emulated/0/Download/_AMBIENT_DONE")

HALLUCINATIONS = {
    "thank you", "thanks for watching", "subscribe", "bye",
    "music", "laughter", "applause", "[music]", "[laughter]",
}

SYSTEM_PROMPT = """\
You are an ambient memory extractor. The user gives you a raw transcript from
a Beats Flex mic worn around the neck — ambient life capture, not a dictation.

Extract ONLY what is clearly present. Return JSON:
{
  "summary":   "2-3 sentence plain summary",
  "todos":     ["action items, first-person"],
  "people":    ["names mentioned"],
  "followups": ["questions or things to check later"],
  "tags":      ["topic keywords, lowercase"]
}

Rules: no fluff, no invented content, no markdown inside strings.
If the transcript is too noisy or empty, return {"summary": "no useful content", "todos": [], "people": [], "followups": [], "tags": []}.
"""

# ── COLOUR OUTPUT ─────────────────────────────────────────────────────────────

C = {
    "head": "\033[1;36m",   # bold cyan
    "ok":   "\033[32m",     # green
    "warn": "\033[33m",     # yellow
    "err":  "\033[31m",     # red
    "dim":  "\033[2m",      # dim
    "rst":  "\033[0m",      # reset
}

def pr(msg, kind=""):
    prefix = {"ok": "✓ ", "warn": "⚠ ", "err": "✗ ", "head": "▸ "}.get(kind, "  ")
    color  = C.get(kind, "")
    print(f"{color}{prefix}{msg}{C['rst']}")

# ── DEPENDENCY CHECK & SETUP ──────────────────────────────────────────────────

DEPS = {
    # (check_fn, install_cmd, note)
    "termux-api pkg": (
        lambda: shutil.which("termux-notification") is not None,
        "pkg install termux-api -y",
        "Required for notifications and wake lock"
    ),
    "ffmpeg": (
        lambda: shutil.which("ffmpeg") is not None,
        "pkg install ffmpeg -y",
        "Required by faster-whisper for audio decoding"
    ),
    "rclone": (
        lambda: shutil.which("rclone") is not None,
        "pkg install rclone -y",
        "Required for Google Drive sync"
    ),
    "cronie": (
        lambda: shutil.which("crond") is not None,
        "pkg install cronie termux-services -y",
        "Required for scheduled 1 AM runs"
    ),
    "faster-whisper": (
        lambda: __import_check("faster_whisper"),
        "pip install faster-whisper",
        "Local transcription engine"
    ),
    "requests": (
        lambda: __import_check("requests"),
        "pip install requests",
        "HTTP calls to Mistral API"
    ),
}

def __import_check(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False

def check_deps(auto_install=False):
    """Check all dependencies. Returns True if everything is ready."""
    print()
    pr("Checking dependencies", "head")
    print()

    missing = []
    for name, (check_fn, install_cmd, note) in DEPS.items():
        ok = False
        try:
            ok = check_fn()
        except Exception:
            pass
        status = f"{C['ok']}✓{C['rst']}" if ok else f"{C['err']}✗{C['rst']}"
        dim = C['dim']
        rst = C['rst']
        print(f"  {status}  {name:<22} {dim}{note}{rst}")
        if not ok:
            missing.append((name, install_cmd))

    print()

    if not missing:
        pr("All dependencies installed", "ok")
        _check_config()
        return True

    pr(f"{len(missing)} missing: {', '.join(n for n, _ in missing)}", "warn")
    print()

    if not auto_install:
        ans = input("  Install missing dependencies now? [Y/n] ").strip().lower()
        if ans == "n":
            pr("Skipping install. Run --setup again when ready.", "warn")
            return False

    print()
    pr("Installing...", "head")
    print()

    all_ok = True
    for name, cmd in missing:
        pr(f"Running: {cmd}", "dim")
        result = subprocess.run(cmd, shell=True, capture_output=False)
        if result.returncode == 0:
            pr(f"{name} installed", "ok")
        else:
            pr(f"{name} failed — run manually: {cmd}", "err")
            all_ok = False

    print()
    if all_ok:
        _check_config()
        _check_rclone()
        _check_cron()
        _check_widgets()
        print()
        pr("Setup complete!", "ok")
        print()
        print(f"  {C['dim']}Repo:      {REPO_URL}{C['rst']}")
        print(f"  {C['dim']}Reinstall: {BOOTSTRAP}{C['rst']}")
    else:
        pr("Some installs failed. Fix manually then re-run --setup.", "err")

    return all_ok

def _check_config():
    print()
    pr("Config check", "head")
    print()

    key = os.getenv("MISTRAL_API_KEY", "")
    if key and not key.startswith("sk-xt-e48724"):   # not the placeholder
        pr("MISTRAL_API_KEY found", "ok")
    else:
        pr("MISTRAL_API_KEY not set", "warn")
        print(textwrap.dedent("""\
            Add to ~/.bashrc (or ~/.zshrc):
              export MISTRAL_API_KEY=your_key_here
            Then: source ~/.bashrc
        """))

    rec_dir = _find_recordings_dir()
    if rec_dir:
        pr(f"Recordings folder: {rec_dir}", "ok")
    else:
        pr("No recordings folder found yet (normal if Samsung Voice Recorder hasn't saved anything)", "warn")

def _check_rclone():
    print()
    pr("rclone / Google Drive", "head")
    print()
    result = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
    remotes = result.stdout.strip()
    if "gdrive" in remotes or "drive" in remotes.lower():
        pr(f"rclone remotes found:\n    {remotes}", "ok")
        pr(f"Will sync to: {GDRIVE_REMOTE}", "ok")
        pr("If that remote name is wrong, set env var: export BEE_GDRIVE_REMOTE=yourremote:AmbientBee", "dim")
    else:
        pr("No Google Drive remote configured", "warn")
        print(textwrap.dedent("""\
            Run:
              rclone config
            → New remote → name it 'gdrive' → Google Drive
            → Follow the OAuth flow in a browser
            → Then re-run: python ambient_bee.py --setup
        """))

def _check_cron():
    print()
    pr("Cron (1 AM schedule)", "head")
    print()
    script_path = Path(__file__).resolve()
    cron_line   = f"0 1 * * * python {script_path} --run-once"

    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout

    if str(script_path) in existing:
        pr("Cron job already configured", "ok")
        return

    pr("No cron job found for ambient_bee", "warn")
    print(f"  Suggested entry:\n    {cron_line}\n")
    ans = input("  Add 1 AM cron job now? [Y/n] ").strip().lower()
    if ans != "n":
        new_cron = existing.rstrip() + f"\n{cron_line}\n"
        proc = subprocess.run(["crontab", "-"], input=new_cron, text=True)
        if proc.returncode == 0:
            pr("Cron job added", "ok")
            print("  Also run:")
            print("    sv-enable crond && sv up crond")
        else:
            pr("Failed to write crontab — add manually", "err")
            print(f"    crontab -e   # then paste: {cron_line}")

def _check_widgets():
    print()
    pr("Termux widgets", "head")
    print()

    shortcuts_dir = Path.home() / ".shortcuts"
    shortcuts_dir.mkdir(exist_ok=True)

    script_path = Path(__file__).resolve()

    widgets = {
        "🐝 Run Bee.sh": f"#!/data/data/com.termux/files/usr/bin/bash\npython {script_path} --run-once\n",
        "☁️ Sync Drive.sh": f"#!/data/data/com.termux/files/usr/bin/bash\nrclone copy {MEMORY_DIR} {GDRIVE_REMOTE} --update\ntermux-toast '☁️ Synced to Drive'\n",
        "📋 Today Log.sh": f"#!/data/data/com.termux/files/usr/bin/bash\ntoday=$(date +%Y-%m-%d)\ncat {MEMORY_DIR}/$today.md | termux-toast -l 10 || termux-toast 'No log yet for today'\n",
        "🗑 Clear Done.sh": f"#!/data/data/com.termux/files/usr/bin/bash\nrm -rf {DONE_DIR}/*\ntermux-toast '🗑 _DONE cleared'\n",
    }

    created = []
    for name, content in widgets.items():
        dest = shortcuts_dir / name
        if not dest.exists():
            dest.write_text(content)
            dest.chmod(0o755)
            created.append(name)
        else:
            pr(f"Already exists: {name}", "dim")

    if created:
        for w in created:
            pr(f"Created: ~/.shortcuts/{w}", "ok")
        print()
        print("  Add to home screen:")
        print("  Long-press home → Widgets → Termux Widget → pick size")
        print("  (install 'Termux:Widget' from F-Droid if not available)")
    else:
        pr("All widget shortcuts already exist", "ok")

# ── TRANSCRIPTION ─────────────────────────────────────────────────────────────

_whisper_model = None   # loaded once, reused

def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
            pr(f"Loading Whisper ({WHISPER_MODEL_SIZE})…", "dim")
            _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
            pr("Whisper ready", "ok")
        except ImportError:
            pr("faster-whisper not installed. Run --setup", "err")
            sys.exit(1)
    return _whisper_model

def transcribe(audio_path: Path) -> str:
    model = _get_whisper()
    try:
        segments, _ = model.transcribe(
            str(audio_path),
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=800,
                speech_pad_ms=400,
                threshold=0.5,
            ),
            condition_on_previous_text=False,   # kills looping hallucinations
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
        )

        cleaned = []
        for seg in segments:
            txt = seg.text.strip()
            if len(txt) < 4:                        continue
            if txt.lower() in HALLUCINATIONS:       continue
            if seg.no_speech_prob > 0.7:            continue
            if seg.end - seg.start < 0.5:           continue
            cleaned.append(txt)

        text = " ".join(cleaned)

        # catch repeat-loop garbage
        words = text.lower().split()
        if len(words) > 10 and len(set(words)) < 3:
            return ""

        return text

    except Exception as e:
        return f"[transcribe error: {e}]"

def diarize(audio_path: Path) -> str:
    """Reuse already-loaded Whisper model for diarization (no double load)."""
    model = _get_whisper()
    try:
        segments, _ = model.transcribe(
            str(audio_path),
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=800),
        )
        out = []
        cur_spk, last_end = 0, 0
        for seg in segments:
            if seg.start - last_end > 1.2:
                cur_spk = 1 - cur_spk
            out.append(f"[S{cur_spk + 1} {seg.start:.1f}s] {seg.text.strip()}")
            last_end = seg.end
        return "\n".join(out) if out else ""
    except Exception:
        return ""

# ── MISTRAL API ───────────────────────────────────────────────────────────────

def call_mistral(transcript: str, retries=2) -> dict:
    if not API_KEY:
        return {"error": "MISTRAL_API_KEY not set — run --setup"}

    import requests as req
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"Transcript:\n{transcript[:8000]}"},
        ],
        "response_format": {"type": "json_object"},
    }

    for attempt in range(retries + 1):
        try:
            r = req.post(BASE_URL, headers=headers, json=body, timeout=30)
            if not r.ok:
                if attempt < retries:
                    time.sleep(3)
                    continue
                return {"error": r.text}
            content = r.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception as e:
            if attempt < retries:
                time.sleep(3)
                continue
            return {"error": str(e)}

# ── FILE HANDLING ─────────────────────────────────────────────────────────────

def _find_recordings_dir() -> Path | None:
    for p in POSSIBLE_RECORD_DIRS:
        try:
            if p.exists() and any(True for _ in p.iterdir()):
                return p
        except PermissionError:
            continue
    return None

def _ensure_dirs():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)

def _wait_until_written(f: Path, wait=2) -> bool:
    """Return True when file size is stable and > 5 KB."""
    try:
        s1 = f.stat().st_size
        time.sleep(wait)
        s2 = f.stat().st_size
        return s1 == s2 and s1 > 5000
    except Exception:
        return False

def move_to_done(audio_path: Path):
    dest = DONE_DIR / audio_path.name
    try:
        shutil.move(str(audio_path), str(dest))
        pr(f"Moved to _DONE: {audio_path.name}", "dim")
    except Exception as e:
        pr(f"Could not move {audio_path.name}: {e}", "warn")

def sync_to_drive():
    if not shutil.which("rclone"):
        pr("rclone not found — skipping Drive sync", "warn")
        return False
    result = subprocess.run(
        ["rclone", "copy", str(MEMORY_DIR), GDRIVE_REMOTE, "--update"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        pr(f"Synced → {GDRIVE_REMOTE}", "ok")
        return True
    else:
        pr(f"Drive sync failed: {result.stderr.strip()}", "err")
        return False

def _notify(title: str, msg: str):
    if shutil.which("termux-notification"):
        subprocess.run(
            ["termux-notification", "--title", title, "--content", msg, "--priority", "high"],
            capture_output=True,
        )

def _wake_lock(acquire=True):
    tool = "termux-wake-lock" if acquire else "termux-wake-unlock"
    if shutil.which(tool):
        subprocess.run([tool], capture_output=True)

# ── CORE PROCESSING ───────────────────────────────────────────────────────────

def process_file(audio_path: Path) -> bool:
    """Transcribe → extract → save → move. Returns True on success."""
    size_mb = audio_path.stat().st_size / 1e6
    pr(f"Processing: {audio_path.name} ({size_mb:.1f} MB)", "head")

    transcript = transcribe(audio_path)
    if len(transcript.strip()) < 10:
        pr("Too short / silence filtered — skipping", "dim")
        move_to_done(audio_path)
        return False

    diarized = diarize(audio_path)
    combined = diarized if diarized else transcript

    pr(f"Transcript ({len(transcript)} chars): {transcript[:120]}…", "dim")

    memory = call_mistral(combined)
    if "error" in memory:
        pr(f"Mistral error: {memory['error']}", "err")

    today    = datetime.now().strftime("%Y-%m-%d")
    log_file = MEMORY_DIR / f"{today}.md"

    with open(log_file, "a", encoding="utf-8") as f:
        ts = datetime.now().strftime("%H:%M")
        f.write(f"\n## {ts} — {audio_path.name}\n\n")
        f.write(f"**Transcript:**\n{transcript[:1500]}\n\n")
        if diarized:
            f.write(f"**Diarized:**\n{diarized[:1500]}\n\n")
        f.write(f"**Memory:**\n```json\n{json.dumps(memory, indent=2)}\n```\n\n---\n")

    pr(f"Saved → {log_file.name}", "ok")
    print(json.dumps(memory, indent=2))

    move_to_done(audio_path)
    return True

# ── MODES ─────────────────────────────────────────────────────────────────────

def run_once():
    """Process all pending files, sync to Drive, send notification."""
    _ensure_dirs()
    _wake_lock(True)

    record_dir = _find_recordings_dir()
    if not record_dir:
        pr("No recordings folder found", "err")
        _wake_lock(False)
        return

    files = sorted(
        f for f in record_dir.glob("*.*")
        if f.suffix.lower() in AUDIO_EXTENSIONS and f.is_file()
    )

    if not files:
        pr("No new audio files found", "dim")
        _wake_lock(False)
        return

    pr(f"Found {len(files)} file(s) to process", "head")
    print()

    processed = 0
    for f in files:
        if _wait_until_written(f):
            if process_file(f):
                processed += 1
            print()

    sync_to_drive()

    summary = f"{processed}/{len(files)} recordings processed"
    _notify("🐝 Ambient Bee", summary)
    pr(summary, "ok")
    pr(f"Memory log: {MEMORY_DIR}", "dim")

    _wake_lock(False)

def watch_loop():
    """Stay running. Process new files as they appear."""
    _ensure_dirs()
    _wake_lock(True)

    record_dir = _find_recordings_dir() or POSSIBLE_RECORD_DIRS[0]
    pr(f"Watching: {record_dir}", "head")
    pr(f"Memory:   {MEMORY_DIR}", "dim")
    pr(f"Drive:    {GDRIVE_REMOTE}", "dim")
    print("Ctrl+C to stop\n")

    seen = {p.name for p in record_dir.glob("*.*") if p.is_file()}

    try:
        while True:
            for f in record_dir.glob("*.*"):
                if f.name not in seen and f.suffix.lower() in AUDIO_EXTENSIONS:
                    if _wait_until_written(f):
                        process_file(f)
                        sync_to_drive()
                        seen.add(f.name)
                        print()
            time.sleep(5)
    except KeyboardInterrupt:
        pr("\nStopped. Memory at " + str(MEMORY_DIR), "ok")
    finally:
        _wake_lock(False)

# ── ENTRYPOINT ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="🐝 Ambient Bee v2 — DIY ambient memory for Termux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Use --setup to get started. Repo: https://github.com/sancoisaacs/ambient-bee",
            Examples:
              python ambient_bee.py --setup              check & install deps
              python ambient_bee.py --run-once           process + sync + exit  ← cron/widget
              python ambient_bee.py --watch              stay running
              python ambient_bee.py --
