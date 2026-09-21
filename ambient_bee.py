#!/usr/bin/env python3
"""
ambient_bee.py v3 — ambient memory with a brain (Termux + Google Drive)
────────────────────────────────────────────────────────────────────────
Hardware:  Beats Flex (neck mic) + Samsung Voice Recorder + Termux
Pipeline:  recording → FFmpeg → whisper.cpp server → Mistral → SQLite → Obsidian vault → rclone

    python ambient_bee.py --setup            check/install deps, cron, widgets
    python ambient_bee.py --run-once         ingest new audio, retry failures, rebuild vault, sync
    python ambient_bee.py --watch            stay running
    python ambient_bee.py --ask "what did I promise Rob"
    python ambient_bee.py --brief            morning brief (yesterday + open todos)
    python ambient_bee.py --digest           weekly rollup → Weekly/YYYY-Www.md
    python ambient_bee.py --todos            open todo ledger
    python ambient_bee.py --done 12          close todo #12
    python ambient_bee.py --stats

Wake words (say them into the Flex): "bee note ...", "bee todo ...", "bee remember ..."
"""

import os, sys, re, time, json, math, sqlite3, subprocess, argparse, shutil, textwrap
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter

# ── CONFIG ────────────────────────────────────────────────────────────────────

API_KEY   = os.getenv("MISTRAL_API_KEY", "")
MODEL     = os.getenv("BEE_MODEL", "mistralai/mistral-large-2512")
BASE_URL  = os.getenv("BEE_BASE_URL", "https://api.xkiro.com/v1/chat/completions")
WHISPER_MODEL_SIZE = os.getenv("BEE_WHISPER_MODEL", "tiny.en")
WHISPER_SERVER_BIN = Path(os.getenv("BEE_WHISPER_SERVER", str(Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-server")))
WHISPER_MODEL_PATH = Path(os.getenv("BEE_WHISPER_MODEL_PATH", str(Path.home() / "whisper.cpp" / "models" / "ggml-tiny.en.bin")))
WHISPER_HOST = os.getenv("BEE_WHISPER_HOST", "127.0.0.1")
WHISPER_PORT = int(os.getenv("BEE_WHISPER_PORT", "8080"))
GDRIVE_REMOTE      = os.getenv("BEE_GDRIVE_REMOTE", "gdrive:AmbientBee")

BEE_HOME   = Path(os.getenv("BEE_HOME", "/storage/emulated/0/Download/AmbientBee"))
VAULT_DIR  = BEE_HOME / "vault"          # Obsidian-compatible, mirrored to Drive
DB_PATH    = BEE_HOME / "bee.db"
DONE_DIR   = BEE_HOME / "_done"

AUDIO_EXTENSIONS = {".m4a", ".wav", ".mp3", ".3ga", ".ogg"}
POSSIBLE_RECORD_DIRS = [
    Path("/storage/emulated/0/Recordings"),
    Path("/storage/emulated/0/SamsungVoiceRecorder"),
    Path("/storage/emulated/0/Voice Recorder"),
    Path("/storage/emulated/0/Download"),
]

HALLUCINATIONS = {"thank you", "thanks for watching", "subscribe", "bye",
                  "music", "laughter", "applause", "[music]", "[laughter]"}

WAKE_RE = re.compile(
    r"\b(?:hey\s+)?bee[,\s]+(note|todo|to do|remember|follow\s*up)[,:\s]+(.+?)(?=(?:\bbee\b)|[.!?]\s|$)",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """\
You are an ambient memory extractor. Input is a raw transcript from a neck-worn mic —
ambient life capture, not dictation. Extract ONLY what is clearly present. Return JSON:
{
  "summary":   "2-3 sentence plain summary",
  "todos":     ["action items, first-person, concrete"],
  "people":    ["proper names mentioned"],
  "followups": ["open questions / things to check later"],
  "decisions": ["decisions or commitments made"],
  "tags":      ["topic keywords, lowercase, 1-2 words each"]
}
Lines marked EXPLICIT NOTE were spoken deliberately by the user — always keep them.
No fluff, no invented content. If transcript is noise/empty:
{"summary":"no useful content","todos":[],"people":[],"followups":[],"decisions":[],"tags":[]}
"""

ASK_PROMPT = """\
You answer questions about the user's own life from their ambient memory log.
Use ONLY the dated memory snippets provided. Cite dates like (2026-09-18).
If the answer isn't in the snippets, say so plainly. Be short and direct.
"""

DIGEST_PROMPT = """\
Write a weekly digest from these daily memory summaries. Sections (markdown):
## Themes  ## People  ## Decisions  ## Still open  ## One-line mood read
Be concrete, first-person to the reader ("you"), no fluff.
"""

# ── OUTPUT ────────────────────────────────────────────────────────────────────

C = {"head": "\033[1;36m", "ok": "\033[32m", "warn": "\033[33m",
     "err": "\033[31m", "dim": "\033[2m", "rst": "\033[0m"}

def pr(msg, kind=""):
    prefix = {"ok": "✓ ", "warn": "⚠ ", "err": "✗ ", "head": "▸ "}.get(kind, "  ")
    print(f"{C.get(kind, '')}{prefix}{msg}{C['rst']}")

def _notify(title, msg):
    if shutil.which("termux-notification"):
        subprocess.run(["termux-notification", "--title", title, "--content", msg,
                        "--priority", "high"], capture_output=True)

def _wake_lock(acquire=True):
    tool = "termux-wake-lock" if acquire else "termux-wake-unlock"
    if shutil.which(tool):
        subprocess.run([tool], capture_output=True)

# ── STORE (SQLite) ────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings(
  id INTEGER PRIMARY KEY, file TEXT UNIQUE, recorded_at TEXT, processed_at TEXT,
  duration_s REAL, chars INTEGER, status TEXT, transcript TEXT, diarized TEXT,
  explicit_notes TEXT, error TEXT, attempts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY, rec_id INTEGER UNIQUE, summary TEXT, todos TEXT, people TEXT,
  followups TEXT, decisions TEXT, tags TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS todos(
  id INTEGER PRIMARY KEY, rec_id INTEGER, text TEXT, norm TEXT UNIQUE, created TEXT,
  done_at TEXT, explicit INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS people(
  name TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, mentions INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, started TEXT, finished TEXT, files INTEGER, ok INTEGER,
  failed INTEGER, whisper_s REAL, llm_s REAL);
"""

def db():
    BEE_HOME.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con

def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

def _j(x):   # tolerant json list load
    try:
        v = json.loads(x) if isinstance(x, str) else x
        return v if isinstance(v, list) else []
    except Exception:
        return []

def store_memory(con, rec_id, recorded_at, mem, explicit_notes):
    con.execute("INSERT OR REPLACE INTO memories(rec_id,summary,todos,people,followups,decisions,tags,raw) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (rec_id, mem.get("summary", ""), json.dumps(mem.get("todos", [])),
                 json.dumps(mem.get("people", [])), json.dumps(mem.get("followups", [])),
                 json.dumps(mem.get("decisions", [])), json.dumps(mem.get("tags", [])), json.dumps(mem)))
    day = recorded_at[:10]
    for t in mem.get("todos", []) or []:
        n = _norm(t)
        if len(n) < 4: continue
        con.execute("INSERT OR IGNORE INTO todos(rec_id,text,norm,created,explicit) VALUES(?,?,?,?,?)",
                    (rec_id, t.strip(), n, day, 0))
    for kind, body in explicit_notes:
        if kind in ("todo", "to do"):
            con.execute("INSERT OR IGNORE INTO todos(rec_id,text,norm,created,explicit) VALUES(?,?,?,?,1)",
                        (rec_id, body, _norm(body), day))
    for p in mem.get("people", []) or []:
        p = p.strip().title()
        if not (2 <= len(p) <= 40): continue
        con.execute("INSERT INTO people(name,first_seen,last_seen,mentions) VALUES(?,?,?,1) "
                    "ON CONFLICT(name) DO UPDATE SET last_seen=max(last_seen,excluded.last_seen), mentions=mentions+1",
                    (p, day, day))
    con.commit()

# ── DEPENDENCIES / SETUP ──────────────────────────────────────────────────────

def _imp(name):
    try: __import__(name); return True
    except ImportError: return False

DEPS = {
    "termux-api": (lambda: shutil.which("termux-notification") is not None, "pkg install termux-api -y", "notifications, wake lock, TTS"),
    "ffmpeg":     (lambda: shutil.which("ffmpeg") is not None, "pkg install ffmpeg -y", "audio normalization for whisper.cpp"),
    "whisper.cpp": (lambda: WHISPER_SERVER_BIN.exists() and WHISPER_MODEL_PATH.exists(), "build/install whisper.cpp server + model", "local transcription"),
    "rclone":     (lambda: shutil.which("rclone") is not None, "pkg install rclone -y", "Drive sync"),
    "cronie":     (lambda: shutil.which("crond") is not None, "pkg install cronie termux-services -y", "scheduled runs"),
    "requests":   (lambda: _imp("requests"), "pip install requests", "Whisper server + LLM HTTP API"),
}

def check_deps(auto_install=False):
    print(); pr("Checking dependencies", "head"); print()
    missing = []
    for name, (fn, cmd, note) in DEPS.items():
        ok = False
        try: ok = fn()
        except Exception: pass
        print(f"  {C['ok'] + '✓' if ok else C['err'] + '✗'}{C['rst']}  {name:<16} {C['dim']}{note}{C['rst']}")
        if not ok: missing.append((name, cmd))
    print()
    if missing:
        pr(f"{len(missing)} missing", "warn")
        if not auto_install and input("  Install now? [Y/n] ").strip().lower() == "n":
            return False
        for name, cmd in missing:
            pr(f"Running: {cmd}", "dim")
            if subprocess.run(cmd, shell=True).returncode != 0:
                pr(f"{name} failed — run manually: {cmd}", "err"); return False
    pr("Dependencies OK", "ok")
    _check_config(); _check_rclone(); _check_cron(); _check_widgets()
    return True

def _check_config():
    print(); pr("Config", "head"); print()
    pr("MISTRAL_API_KEY set" if API_KEY else "MISTRAL_API_KEY not set → export MISTRAL_API_KEY=...", "ok" if API_KEY else "warn")
    rd = _find_recordings_dir()
    pr(f"Recordings: {rd}" if rd else "No recordings folder yet (record once in Samsung Voice Recorder)", "ok" if rd else "warn")
    pr(f"Bee home:   {BEE_HOME}", "dim"); pr(f"Vault:      {VAULT_DIR}  (open in Obsidian)", "dim")

def _check_rclone():
    print(); pr("rclone / Drive", "head"); print()
    r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True).stdout.strip()
    if r:
        pr(f"Remotes: {r.replace(chr(10), ', ')}", "ok"); pr(f"Sync target: {GDRIVE_REMOTE}", "ok")
    else:
        pr("No remote. Run: rclone config → new remote 'gdrive' → Google Drive", "warn")

def _check_cron():
    print(); pr("Cron", "head"); print()
    sp = Path(__file__).resolve()
    lines = [f"0 1 * * * python {sp} --run-once",
             f"30 6 * * * python {sp} --brief --notify",
             f"0 18 * * 0 python {sp} --digest"]
    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    if str(sp) in existing:
        pr("Cron jobs present", "ok"); return
    print("  Will add:\n    " + "\n    ".join(lines) + "\n")
    if input("  Add now? [Y/n] ").strip().lower() != "n":
        new = existing.rstrip() + "\n" + "\n".join(lines) + "\n"
        if subprocess.run(["crontab", "-"], input=new, text=True).returncode == 0:
            pr("Added (also: sv-enable crond && sv up crond)", "ok")
        else:
            pr("Failed — crontab -e and paste manually", "err")

def _check_widgets():
    print(); pr("Termux widgets", "head"); print()
    sd = Path.home() / ".shortcuts"; sd.mkdir(exist_ok=True)
    sp = Path(__file__).resolve(); sh = "#!/data/data/com.termux/files/usr/bin/bash\n"
    widgets = {
        "🐝 Run Bee.sh":     sh + f"python {sp} --run-once\n",
        "☀️ Brief.sh":       sh + f"python {sp} --brief --speak\n",
        "❓ Ask Bee.sh":     sh + f"q=$(termux-dialog speech -t 'Ask Bee' | python -c 'import sys,json;print(json.load(sys.stdin)[\"text\"])')\n[ -n \"$q\" ] && python {sp} --ask \"$q\" --speak\n",
        "✅ Todos.sh":       sh + f"python {sp} --todos | termux-toast -l 12\n",
        "☁️ Sync Drive.sh":  sh + f"rclone sync {VAULT_DIR} {GDRIVE_REMOTE} && termux-toast '☁️ synced'\n",
    }
    for name, content in widgets.items():
        d = sd / name
        if not d.exists():
            d.write_text(content); d.chmod(0o755); pr(f"Created ~/.shortcuts/{name}", "ok")
        else:
            pr(f"Exists: {name}", "dim")

# ── TRANSCRIPTION ─────────────────────────────────────────────────────────────

_whisper_server = None


def _whisper_url(path=""):
    return f"http://{WHISPER_HOST}:{WHISPER_PORT}{path}"


def _whisper_server_ready():
    try:
        import requests as req
        r = req.get(_whisper_url("/"), timeout=2)
        return r.ok and "Whisper.cpp Server" in r.text
    except Exception:
        return False


def _start_whisper_server():
    """Start the native whisper.cpp HTTP server if it is not already running."""
    global _whisper_server
    if _whisper_server_ready():
        pr(f"Whisper.cpp already running on {WHISPER_HOST}:{WHISPER_PORT}", "dim")
        return False
    if not WHISPER_SERVER_BIN.exists():
        raise RuntimeError(f"whisper-server not found: {WHISPER_SERVER_BIN}")
    if not WHISPER_MODEL_PATH.exists():
        raise RuntimeError(f"Whisper model not found: {WHISPER_MODEL_PATH}")
    pr(f"Starting whisper.cpp ({WHISPER_MODEL_PATH.name})...", "dim")
    _whisper_server = subprocess.Popen(
        [str(WHISPER_SERVER_BIN), "-m", str(WHISPER_MODEL_PATH),
         "--host", WHISPER_HOST, "--port", str(WHISPER_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    for _ in range(30):
        if _whisper_server_ready():
            pr(f"Whisper.cpp ready on {WHISPER_HOST}:{WHISPER_PORT}", "ok")
            return True
        if _whisper_server.poll() is not None:
            raise RuntimeError("whisper-server exited while starting")
        time.sleep(0.5)
    _stop_whisper_server()
    raise RuntimeError("Timed out waiting for whisper-server")


def _stop_whisper_server():
    """Stop only the whisper.cpp server started by Ambient Bee."""
    global _whisper_server
    if _whisper_server is not None:
        if _whisper_server.poll() is None:
            _whisper_server.terminate()
            try:
                _whisper_server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _whisper_server.kill()
                _whisper_server.wait(timeout=2)
        _whisper_server = None


def _normalize_audio(audio_path: Path):
    """Convert source audio to 16 kHz mono PCM WAV for whisper.cpp."""
    import tempfile
    fd, tmp_name = tempfile.mkstemp(prefix="bee_", suffix=".wav", dir=str(BEE_HOME))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        r = subprocess.run(["ffmpeg", "-y", "-i", str(audio_path), "-ar", "16000", "-ac", "1",
                            "-c:a", "pcm_s16le", str(tmp)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {r.stderr[-500:]}")
        return tmp
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def transcribe(audio_path: Path):
    """Single Whisper pass via native whisper.cpp → (clean_text, diarized_text, duration_s)."""
    import requests as req
    _start_whisper_server()
    wav = _normalize_audio(audio_path)
    try:
        with wav.open("rb") as fh:
            r = req.post(
                _whisper_url("/inference"),
                files={"file": (wav.name, fh, "audio/wav")},
                data={"temperature": "0.0", "response_format": "verbose_json"},
                timeout=max(120, int(os.getenv("BEE_WHISPER_TIMEOUT", "900"))),
            )
        if not r.ok:
            raise RuntimeError(f"whisper.cpp HTTP {r.status_code}: {r.text[:500]}")
        result = r.json()
    finally:
        wav.unlink(missing_ok=True)

    clean, dia = [], []
    spk, last_end = 0, 0.0
    for seg in result.get("segments", []) or []:
        txt = str(seg.get("text", "")).strip()
        start = float(seg.get("start", 0) or 0)
        end = float(seg.get("end", start) or start)
        no_speech = float(seg.get("no_speech_prob", 0) or 0)
        if len(txt) < 4 or txt.lower() in HALLUCINATIONS: continue
        if no_speech > 0.7 or end - start < 0.5: continue
        if start - last_end > 1.2: spk = 1 - spk
        clean.append(txt); dia.append(f"[S{spk+1} {start:.1f}s] {txt}"); last_end = end
    text = " ".join(clean).strip()
    words = text.lower().split()
    if len(words) > 10 and len(set(words)) < 3:
        text, dia = "", []
    duration = float(result.get("duration", 0) or 0)
    return text, "\n".join(dia), duration

def extract_wake_notes(text):
    """'bee todo call Rob about egress' → [('todo','call Rob about egress')]"""
    out = []
    for m in WAKE_RE.finditer(text):
        kind, body = m.group(1).lower().replace(" ", ""), m.group(2).strip(" ,.")
        if len(body) > 3: out.append((kind if kind != "todo" else "todo", body))
    return out

# ── LLM ───────────────────────────────────────────────────────────────────────

def llm(system, user, json_mode=True, retries=2, timeout=40):
    if not API_KEY: return {"error": "MISTRAL_API_KEY not set"} if json_mode else "MISTRAL_API_KEY not set"
    import requests as req
    body = {"model": MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode: body["response_format"] = {"type": "json_object"}
    for attempt in range(retries + 1):
        try:
            r = req.post(BASE_URL, headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                         json=body, timeout=timeout)
            if not r.ok: raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            content = r.json()["choices"][0]["message"]["content"]
            return json.loads(content) if json_mode else content
        except Exception as e:
            if attempt < retries: time.sleep(3 * (attempt + 1)); continue
            return {"error": str(e)} if json_mode else f"[llm error: {e}]"

# ── LOCAL SEARCH (BM25, zero deps) ────────────────────────────────────────────

def _tok(s): return re.findall(r"[a-z0-9]{2,}", s.lower())

def search(con, query, k=8):
    rows = con.execute("SELECT r.id, r.recorded_at, r.transcript, m.summary, m.todos, m.decisions, m.people "
                       "FROM recordings r LEFT JOIN memories m ON m.rec_id=r.id WHERE r.status='ok'").fetchall()
    docs = [(r, _tok(" ".join(str(r[c] or "") for c in ("transcript", "summary", "todos", "decisions", "people")))) for r in rows]
    if not docs: return []
    N, avg = len(docs), sum(len(d) for _, d in docs) / len(docs)
    df = Counter(t for _, d in docs for t in set(d))
    q = _tok(query); scored = []
    for r, d in docs:
        tf = Counter(d); s = 0.0
        for t in q:
            if t not in tf: continue
            idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / avg))
        if s > 0: scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:k]]

def ask(con, question, speak=False):
    hits = search(con, question)
    if not hits:
        ans = "Nothing in memory matches that."
    else:
        ctx = "\n\n".join(f"({h['recorded_at'][:10]}) {h['summary'] or ''}\nDECISIONS: {h['decisions']}\nTODOS: {h['todos']}\n"
                          f"TRANSCRIPT: {(h['transcript'] or '')[:900]}" for h in hits)
        ans = llm(ASK_PROMPT, f"Question: {question}\n\nMemory:\n{ctx}", json_mode=False)
    print(ans)
    if speak: _speak(ans)
    return ans

def _speak(text):
    if shutil.which("termux-tts-speak"):
        subprocess.run(["termux-tts-speak", text[:600]], capture_output=True)

# ── VAULT (Obsidian-compatible markdown, rebuilt from DB) ──────────────────────

def _link(name): return f"[[{name}]]"

def build_vault(con, days=None):
    """Regenerate Daily/, People/, Todos.md, Index.md from SQLite. Idempotent."""
    for sub in ("Daily", "People", "Weekly"): (VAULT_DIR / sub).mkdir(parents=True, exist_ok=True)
    q = "SELECT r.*, m.summary, m.todos, m.people, m.followups, m.decisions, m.tags FROM recordings r " \
        "LEFT JOIN memories m ON m.rec_id=r.id WHERE r.status='ok'"
    rows = con.execute(q + " ORDER BY r.recorded_at").fetchall()
    by_day = {}
    for r in rows: by_day.setdefault(r["recorded_at"][:10], []).append(r)
    if days: by_day = {d: v for d, v in by_day.items() if d in days}

    for day, recs in by_day.items():
        tags = sorted({t for r in recs for t in _j(r["tags"])})
        people = sorted({p.title() for r in recs for p in _j(r["people"])})
        out = [f"---\ndate: {day}\ntags: [{', '.join('bee/' + t.replace(' ', '-') for t in tags)}]\n---",
               f"# {day}", ""]
        if people: out += ["**People:** " + ", ".join(_link(p) for p in people), ""]
        for r in recs:
            ts = r["recorded_at"][11:16]
            out += [f"## {ts} · {r['file']}  ({(r['duration_s'] or 0)/60:.0f} min)", "",
                    r["summary"] or "_no summary_", ""]
            ex = _j(r["explicit_notes"])
            if ex: out += ["**Said to Bee:**"] + [f"- 🐝 *{k}*: {b}" for k, b in ex] + [""]
            for label, key in (("Decisions", "decisions"), ("Todos", "todos"), ("Follow-ups", "followups")):
                items = _j(r[key])
                if items: out += [f"**{label}:**"] + [f"- {'[ ] ' if key == 'todos' else ''}{i}" for i in items] + [""]
            out += ["<details><summary>Transcript</summary>", "", (r["transcript"] or "")[:4000], "", "</details>", "", "---", ""]
        (VAULT_DIR / "Daily" / f"{day}.md").write_text("\n".join(out), encoding="utf-8")

    # People pages
    for p in con.execute("SELECT * FROM people ORDER BY mentions DESC").fetchall():
        hits = con.execute("SELECT r.recorded_at, m.summary FROM memories m JOIN recordings r ON r.id=m.rec_id "
                           "WHERE lower(m.people) LIKE ? ORDER BY r.recorded_at DESC", (f"%{p['name'].lower()}%",)).fetchall()
        lines = [f"# {p['name']}", "", f"Mentions: {p['mentions']} · first {p['first_seen']} · last {p['last_seen']}", ""]
        lines += [f"- [[Daily/{h['recorded_at'][:10]}|{h['recorded_at'][:10]}]] — {h['summary']}" for h in hits[:60]]
        (VAULT_DIR / "People" / f"{p['name']}.md").write_text("\n".join(lines), encoding="utf-8")

    # Todos ledger
    todos = con.execute("SELECT * FROM todos ORDER BY done_at IS NOT NULL, created DESC").fetchall()
    lines = ["# Todos", "", "## Open", ""]
    lines += [f"- [ ] `#{t['id']}` {t['text']}  _({t['created']}{' · 🐝' if t['explicit'] else ''})_" for t in todos if not t["done_at"]] or ["_nothing open_"]
    lines += ["", "## Done", ""]
    lines += [f"- [x] `#{t['id']}` {t['text']}  _({t['created']} → {t['done_at']})_" for t in todos if t["done_at"]][:100]
    (VAULT_DIR / "Todos.md").write_text("\n".join(lines), encoding="utf-8")

    # Index
    stats = con.execute("SELECT count(*) n, sum(duration_s) d FROM recordings WHERE status='ok'").fetchone()
    open_n = con.execute("SELECT count(*) FROM todos WHERE done_at IS NULL").fetchone()[0]
    idx = ["# 🐝 Ambient Bee", "", f"{stats['n'] or 0} recordings · {(stats['d'] or 0)/3600:.1f} h captured · {open_n} open todos",
           "", "- [[Todos]]", "- Daily: " + " · ".join(f"[[Daily/{d}|{d}]]" for d in sorted(by_day)[-14:][::-1]),
           "- People: " + " · ".join(_link(p["name"]) for p in con.execute("SELECT name FROM people ORDER BY mentions DESC LIMIT 25")),
           "- Weekly: " + " · ".join(f"[[Weekly/{f.stem}|{f.stem}]]" for f in sorted((VAULT_DIR / 'Weekly').glob('*.md'))[::-1][:8])]
    (VAULT_DIR / "Index.md").write_text("\n".join(idx), encoding="utf-8")

# ── BRIEF / DIGEST / TODOS ────────────────────────────────────────────────────

def brief(con, speak=False, notify=False):
    y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    recs = con.execute("SELECT r.recorded_at, m.summary, m.decisions FROM recordings r JOIN memories m ON m.rec_id=r.id "
                       "WHERE r.recorded_at LIKE ? ORDER BY r.recorded_at", (y + "%",)).fetchall()
    todos = con.execute("SELECT id,text,created FROM todos WHERE done_at IS NULL ORDER BY explicit DESC, created DESC LIMIT 12").fetchall()
    out = [f"☀️ Brief — {datetime.now():%a %d %b}", ""]
    if recs:
        out += [f"Yesterday ({len(recs)} recordings):"] + [f"  • {r['summary']}" for r in recs if r["summary"]]
        dec = [d for r in recs for d in _j(r["decisions"])]
        if dec: out += ["", "Decided:"] + [f"  • {d}" for d in dec]
    else:
        out += ["Yesterday: nothing captured."]
    out += ["", f"Open todos ({len(todos)}):"] + [f"  • #{t['id']} {t['text']}" for t in todos]
    text = "\n".join(out); print(text)
    if notify: _notify("☀️ Bee brief", f"{len(recs)} recordings yesterday · {len(todos)} open todos")
    if speak: _speak(re.sub(r"[•#☀️]", "", text))
    return text

def digest(con):
    iso = datetime.now().isocalendar(); tag = f"{iso[0]}-W{iso[1]:02d}"
    start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    recs = con.execute("SELECT r.recorded_at, m.summary, m.decisions, m.people, m.todos FROM recordings r JOIN memories m "
                       "ON m.rec_id=r.id WHERE r.recorded_at >= ? ORDER BY r.recorded_at", (start,)).fetchall()
    if not recs: pr("Nothing this week", "warn"); return
    src = "\n".join(f"({r['recorded_at'][:10]}) {r['summary']} | decisions={r['decisions']} | people={r['people']}" for r in recs)
    body = llm(DIGEST_PROMPT, src, json_mode=False)
    (VAULT_DIR / "Weekly").mkdir(parents=True, exist_ok=True)
    (VAULT_DIR / "Weekly" / f"{tag}.md").write_text(f"# Week {tag}\n\n{body}\n", encoding="utf-8")
    pr(f"Weekly/{tag}.md written", "ok"); print(body)

def list_todos(con):
    rows = con.execute("SELECT id,text,created,explicit FROM todos WHERE done_at IS NULL ORDER BY explicit DESC, created DESC").fetchall()
    if not rows: print("No open todos 🎉"); return
    for t in rows: print(f"#{t['id']:<4} {'🐝 ' if t['explicit'] else ''}{t['text']}  ({t['created']})")

def done_todo(con, tid):
    con.execute("UPDATE todos SET done_at=? WHERE id=?", (datetime.now().strftime("%Y-%m-%d"), tid)); con.commit()
    pr(f"#{tid} closed", "ok")

def stats(con):
    r = con.execute("SELECT count(*) n, sum(duration_s) d, sum(status='failed') f FROM recordings").fetchone()
    t = con.execute("SELECT sum(done_at IS NULL) o, sum(done_at IS NOT NULL) c FROM todos").fetchone()
    runs = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5").fetchall()
    print(f"recordings {r['n']}  ({(r['d'] or 0)/3600:.1f} h audio)  failed {r['f'] or 0}")
    print(f"todos open {t['o'] or 0}  closed {t['c'] or 0}")
    print(f"people {con.execute('SELECT count(*) FROM people').fetchone()[0]}")
    for x in runs:
        print(f"run {x['started'][:16]}  files={x['files']} ok={x['ok']} failed={x['failed']}  whisper={x['whisper_s']:.0f}s llm={x['llm_s']:.0f}s")

# ── FILES / SYNC ──────────────────────────────────────────────────────────────

def _find_recordings_dir():
    for p in POSSIBLE_RECORD_DIRS:
        try:
            if p.exists() and any(True for _ in p.iterdir()): return p
        except PermissionError: continue
    return None

def _stable(f: Path, wait=2):
    try:
        s1 = f.stat().st_size; time.sleep(wait); return s1 == f.stat().st_size and s1 > 5000
    except Exception: return False

def _recorded_at(f: Path):
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})[-_ T]?(\d{2})[-_:]?(\d{2})", f.name)
    if m:
        try: return datetime(*map(int, m.groups())).strftime("%Y-%m-%dT%H:%M")
        except ValueError: pass
    return datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%dT%H:%M")

def sync_to_drive():
    if not shutil.which("rclone"): pr("rclone missing — skip sync", "warn"); return False
    r = subprocess.run(["rclone", "sync", str(VAULT_DIR), GDRIVE_REMOTE, "--fast-list"], capture_output=True, text=True)
    r2 = subprocess.run(["rclone", "copy", str(DB_PATH), GDRIVE_REMOTE.rsplit("/", 1)[0] + "/_db", "--update"], capture_output=True, text=True)
    ok = r.returncode == 0
    pr(f"Synced vault → {GDRIVE_REMOTE}" if ok else f"Sync failed: {r.stderr.strip()[:200]}", "ok" if ok else "err")
    return ok

# ── CORE PROCESSING ───────────────────────────────────────────────────────────

def process_file(con, audio_path: Path, timers):
    rec_at = _recorded_at(audio_path)
    pr(f"{audio_path.name} ({audio_path.stat().st_size/1e6:.1f} MB, recorded {rec_at})", "head")
    cur = con.execute("INSERT INTO recordings(file,recorded_at,status,attempts) VALUES(?,?,?,0) "
                      "ON CONFLICT(file) DO UPDATE SET attempts=attempts+1 RETURNING id", (audio_path.name, rec_at, "processing"))
    rec_id = cur.fetchone()[0]; con.commit()
    try:
        t0 = time.time(); text, dia, dur = transcribe(audio_path); timers["whisper"] += time.time() - t0
        if len(text.strip()) < 10:
            con.execute("UPDATE recordings SET status='silent', processed_at=?, duration_s=? WHERE id=?",
                        (datetime.now().isoformat(timespec="minutes"), dur, rec_id)); con.commit()
            pr("silence — skipped", "dim"); _move_done(audio_path); return False
        notes = extract_wake_notes(text)
        prompt = (dia or text)[:9000]
        if notes: prompt = "EXPLICIT NOTES:\n" + "\n".join(f"- {k}: {b}" for k, b in notes) + "\n\nTRANSCRIPT:\n" + prompt
        t0 = time.time(); mem = llm(SYSTEM_PROMPT, prompt); timers["llm"] += time.time() - t0
        if "error" in mem: raise RuntimeError(mem["error"])
        con.execute("UPDATE recordings SET status='ok', processed_at=?, duration_s=?, chars=?, transcript=?, diarized=?, explicit_notes=?, error=NULL WHERE id=?",
                    (datetime.now().isoformat(timespec="minutes"), dur, len(text), text, dia, json.dumps(notes), rec_id))
        store_memory(con, rec_id, rec_at, mem, notes)
        pr(f"{mem.get('summary','')[:110]}", "ok")
        if notes: pr(f"{len(notes)} explicit bee note(s) captured", "ok")
        _move_done(audio_path); return True
    except Exception as e:
        con.execute("UPDATE recordings SET status='failed', error=? WHERE id=?", (str(e)[:500], rec_id)); con.commit()
        pr(f"failed (will retry next run): {e}", "err"); return False

def _move_done(p: Path):
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    try: shutil.move(str(p), str(DONE_DIR / p.name))
    except Exception as e: pr(f"could not move {p.name}: {e}", "warn")

def retry_failed(con, timers):
    """Files that failed at the LLM step still sit in the recordings dir (never moved) → picked up again naturally.
    Failed rows with attempts>=3 are left alone and flagged in --stats."""
    n = con.execute("SELECT count(*) FROM recordings WHERE status='failed' AND attempts>=3").fetchone()[0]
    if n: pr(f"{n} file(s) permanently failed after 3 attempts — see --stats", "warn")

def run_once():
    con = db(); _wake_lock(True)
    server_started = False
    started = datetime.now().isoformat(timespec="seconds"); timers = {"whisper": 0.0, "llm": 0.0}
    try:
        server_started = _start_whisper_server()
        rd = _find_recordings_dir()
        if not rd: pr("No recordings folder", "err"); return
        skip = {r[0] for r in con.execute("SELECT file FROM recordings WHERE status='failed' AND attempts>=3")}
        files = sorted(f for f in rd.glob("*.*") if f.suffix.lower() in AUDIO_EXTENSIONS and f.is_file() and f.name not in skip)
        ok = fail = 0
        if files:
            pr(f"{len(files)} file(s) to process", "head"); print()
            for f in files:
                if not _stable(f): continue
                if process_file(con, f, timers): ok += 1
                else: fail += 1
                print()
        else:
            pr("No new audio", "dim")
        retry_failed(con, timers)
        build_vault(con); pr("Vault rebuilt", "ok")
        sync_to_drive()
        con.execute("INSERT INTO runs(started,finished,files,ok,failed,whisper_s,llm_s) VALUES(?,?,?,?,?,?,?)",
                    (started, datetime.now().isoformat(timespec="seconds"), len(files), ok, fail, timers["whisper"], timers["llm"]))
        con.commit()
        open_n = con.execute("SELECT count(*) FROM todos WHERE done_at IS NULL").fetchone()[0]
        _notify("🐝 Ambient Bee", f"{ok}/{len(files)} processed · {open_n} open todos")
    finally:
        if server_started: _stop_whisper_server()
        _wake_lock(False)

def watch_loop():
    con = db(); _wake_lock(True)
    server_started = False
    rd = _find_recordings_dir() or POSSIBLE_RECORD_DIRS[0]
    server_started = _start_whisper_server()
    pr(f"Watching {rd}  (Ctrl+C to stop)", "head")
    seen = {p.name for p in rd.glob("*.*")}
    try:
        while True:
            for f in rd.glob("*.*"):
                if f.name not in seen and f.suffix.lower() in AUDIO_EXTENSIONS and _stable(f):
                    process_file(con, f, {"whisper": 0.0, "llm": 0.0}); build_vault(con, days={_recorded_at(f)[:10]}); sync_to_drive(); seen.add(f.name)
            time.sleep(5)
    except KeyboardInterrupt:
        pr("stopped", "ok")
    finally:
        if server_started: _stop_whisper_server()
        _wake_lock(False)

# ── MIGRATION FROM v2 ─────────────────────────────────────────────────────────

def migrate_v2(old_dir: Path):
    """Import v2 _AMBIENT_MEMORY/*.md logs into the DB so nothing is lost."""
    con = db(); n = 0
    for md in sorted(old_dir.glob("*.md")):
        day = md.stem
        for block in md.read_text(encoding="utf-8").split("\n## ")[1:]:
            head, _, rest = block.partition("\n")
            m = re.match(r"(\d{2}:\d{2}) — (.+)", head.strip())
            if not m: continue
            ts, fname = m.groups()
            tr = re.search(r"\*\*Transcript:\*\*\n(.*?)\n\n", rest, re.S)
            js = re.search(r"```json\n(.*?)\n```", rest, re.S)
            try: mem = json.loads(js.group(1)) if js else {}
            except Exception: mem = {}
            cur = con.execute("INSERT OR IGNORE INTO recordings(file,recorded_at,processed_at,status,transcript,chars,explicit_notes) VALUES(?,?,?,?,?,?,'[]')",
                              (fname, f"{day}T{ts}", f"{day}T{ts}", "ok", tr.group(1) if tr else "", len(tr.group(1)) if tr else 0))
            rid = con.execute("SELECT id FROM recordings WHERE file=?", (fname,)).fetchone()[0]
            if mem and "error" not in mem: store_memory(con, rid, f"{day}T{ts}", mem, [])
            n += 1
    build_vault(con); pr(f"Imported {n} v2 entries → vault rebuilt", "ok")

# ── ENTRYPOINT ────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="🐝 Ambient Bee v3", formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=textwrap.dedent(__doc__.split("Hardware")[0]))
    p.add_argument("--setup", action="store_true"); p.add_argument("--check", action="store_true")
    p.add_argument("--run-once", action="store_true"); p.add_argument("--watch", action="store_true")
    p.add_argument("--transcribe", type=str, metavar="FILE")
    p.add_argument("--ask", type=str, metavar="QUESTION"); p.add_argument("--brief", action="store_true")
    p.add_argument("--digest", action="store_true"); p.add_argument("--todos", action="store_true")
    p.add_argument("--done", type=int, metavar="ID"); p.add_argument("--stats", action="store_true")
    p.add_argument("--rebuild", action="store_true", help="rebuild vault from DB")
    p.add_argument("--migrate-v2", type=str, metavar="DIR", help="import old _AMBIENT_MEMORY folder")
    p.add_argument("--speak", action="store_true"); p.add_argument("--notify", action="store_true")
    a = p.parse_args()
    print(f"\n{C['head']}🐝 Ambient Bee v3{C['rst']}\n")

    if a.setup or a.check: check_deps(auto_install=False)
    elif a.run_once: run_once()
    elif a.watch: watch_loop()
    elif a.transcribe:
        con = db(); process_file(con, Path(a.transcribe), {"whisper": 0.0, "llm": 0.0}); build_vault(con)
    elif a.ask: ask(db(), a.ask, speak=a.speak)
    elif a.brief: brief(db(), speak=a.speak, notify=a.notify)
    elif a.digest: digest(db())
    elif a.todos: list_todos(db())
    elif a.done is not None: done_todo(db(), a.done)
    elif a.stats: stats(db())
    elif a.rebuild: build_vault(db()); pr("Vault rebuilt", "ok")
    elif a.migrate_v2: migrate_v2(Path(a.migrate_v2))
    else: p.print_help()

if __name__ == "__main__":
    main()
