#!/usr/bin/env python3
"""
ambient_bee.py v3 — ambient memory with a brain (Termux + Google Drive)
────────────────────────────────────────────────────────────────────────
Hardware:  Beats Flex (neck mic) + Samsung Voice Recorder + Termux
Pipeline:  recording → faster-whisper → Mistral → SQLite → Obsidian vault → rclone

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
WHISPER_MODEL_SIZE = os.getenv("BEE_WHISPER_MODEL", "small")
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
    "termux-api":     (lambda: shutil.which("termux-notification") is not None, "pkg install termux-api -y",  "notifications, wake lock, TTS"),
    "ffmpeg":         (lambda: shutil.which("ffmpeg") is not None,              "pkg install ffmpeg -y",      "audio decode for whisper"),
    "rclone":         (lambda: shutil.which("rclone") is not None,              "pkg install rclone -y",      "Drive sync"),
    "cronie":         (lambda: shutil.which("crond") is not None,               "pkg install cronie termux-services -y", "scheduled runs"),
    "faster-whisper": (lambda: _imp("faster_whisper"),                          "pip install faster-whisper", "local transcription"),
    "requests":       (lambda: _imp("requests"),                                "pip install requests",       "LLM API"),
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

_whisper = None
def _get_whisper():
    global _whisper
    if _whisper is None:
        try:
            from faster_whisper import WhisperModel
            pr(f"Loading Whisper ({WHISPER_MODEL_SIZE})...", "dim")
            _whisper = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
        except ImportError:
            pr("faster-whisper missing — run --setup", "err"); sys.exit(1)
    return _whisper

def transcribe(audio_path: Path):
    """Single Whisper pass → (clean_text, diarized_text, duration_s). No double decode."""
    model = _get_whisper()
    segments, info = model.transcribe(
        str(audio_path), language="en", beam_size=5, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=800, speech_pad_ms=400, threshold=0.5),
        condition_on_previous_text=False, no_speech_threshold=0.6,
        compression_ratio_threshold=2.4, log_prob_threshold=-1.0)
    clean, dia = [], []
    spk, last_end = 0, 0.0
    for seg in segments:
        txt = seg.text.strip()
        if len(txt) < 4 or txt.lower() in HALLUCINATIONS: continue
        if seg.no_speech_prob > 0.7 or seg.end - seg.start < 0.5: continue
        if seg.start - last_end > 1.2: spk = 1 - spk
        clean.append(txt); dia.append(f"[S{spk+1} {seg.start:.1f}s] {txt}"); last_end = seg.end
    text = " ".join(clean)
    words = text.lower().split()
    if len(words) > 10 and len(set(words)) < 3:
        text, dia = "", []
    return text, "\n".join(dia), float(getattr(info, "duration", 0) or 0)

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
    open_n = con.execute("SELECT count(*) FROM todos WHERE done_at IS NULL").fetchone
