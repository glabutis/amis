#!/usr/bin/env python3
"""
AMIS Web UI — real-time dashboard served over HTTP/WebSocket.
Run with: python3 web.py --config /etc/amis/config.yaml
"""

import argparse
import asyncio
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


CONFIG: dict = {}
app = FastAPI(title="AMIS")


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def read_status() -> dict:
    path = Path(CONFIG["paths"]["status_file"])
    if not path.exists():
        return {"state": "idle", "updated_at": None}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"state": "idle", "updated_at": None}


def read_log_tail(n: int) -> list[str]:
    log_path = Path(CONFIG["paths"]["log_file"])
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


def read_history(limit: int) -> list[dict]:
    db_path = Path(CONFIG["paths"]["db_file"])
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                backup_folder,
                volume_label,
                COUNT(*)         AS file_count,
                SUM(file_size)   AS total_bytes,
                MIN(ingested_at) AS started_at,
                MAX(ingested_at) AS finished_at
            FROM ingested_files
            GROUP BY backup_folder
            ORDER BY finished_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except (sqlite3.OperationalError, OSError):
        return []


def build_snapshot() -> dict:
    web_cfg = CONFIG.get("web", {})
    return {
        "status": read_status(),
        "log_lines": read_log_tail(web_cfg.get("log_tail_lines", 50)),
        "history": read_history(web_cfg.get("history_limit", 20)),
    }


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self):
        self._connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.add(ws)

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws)

    async def broadcast(self, data: dict):
        payload = json.dumps(data)
        dead = set()
        for ws in self._connections:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._connections -= dead

    @property
    def count(self):
        return len(self._connections)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Background broadcaster — polls state files and pushes to all clients
# ---------------------------------------------------------------------------

async def broadcaster():
    last_snapshot = {}
    while True:
        await asyncio.sleep(0.5)
        if manager.count == 0:
            continue
        snap = build_snapshot()
        # Only broadcast if something changed
        if snap != last_snapshot:
            await manager.broadcast(snap)
            last_snapshot = snap


@app.on_event("startup")
async def startup():
    asyncio.create_task(broadcaster())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    return read_status()


@app.get("/api/snapshot")
async def api_snapshot():
    return build_snapshot()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # Send current state immediately on connect
    try:
        await ws.send_text(json.dumps(build_snapshot()))
        while True:
            # Keep connection alive; broadcaster handles pushes
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTML


# ---------------------------------------------------------------------------
# Dashboard HTML — single-file, no build step, no external deps
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AMIS — Media Ingestion</title>
<style>
  :root {
    --bg:       #0d0d0d;
    --surface:  #161616;
    --border:   #2a2a2a;
    --text:     #e0e0e0;
    --muted:    #666;
    --green:    #00e676;
    --amber:    #ffab00;
    --red:      #ff5252;
    --blue:     #40c4ff;
    --font:     'SF Mono', 'Fira Code', 'Consolas', monospace;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
    font-size: 13px;
    min-height: 100vh;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  header h1 {
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 3px;
    color: var(--green);
  }
  header p {
    font-size: 11px;
    color: var(--muted);
    margin-top: 2px;
  }
  #conn-pill {
    font-size: 11px;
    padding: 4px 10px;
    border-radius: 20px;
    border: 1px solid var(--border);
    color: var(--muted);
    transition: all 0.3s;
  }
  #conn-pill.live { color: var(--green); border-color: var(--green); }
  #conn-pill.dead { color: var(--red);   border-color: var(--red); }

  /* ── Layout ── */
  main {
    display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: auto auto;
    gap: 16px;
    padding: 20px 24px;
    max-width: 1400px;
    margin: 0 auto;
  }
  @media (max-width: 900px) {
    main { grid-template-columns: 1fr; }
  }

  /* ── Cards ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 18px 20px;
  }
  .card-title {
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 14px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 8px;
  }

  /* ── Status card ── */
  #status-card { grid-column: 1 / -1; }
  #status-row {
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
  }
  #state-badge {
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    padding: 5px 14px;
    border-radius: 4px;
    border: 1px solid;
  }
  #state-badge.idle       { color: var(--muted); border-color: var(--muted); }
  #state-badge.mounting,
  #state-badge.scanning,
  #state-badge.ejecting   { color: var(--amber); border-color: var(--amber); }
  #state-badge.transcoding { color: var(--green); border-color: var(--green);
                             box-shadow: 0 0 8px rgba(0,230,118,0.2); }
  #state-badge.done       { color: var(--blue);  border-color: var(--blue); }
  #state-badge.error      { color: var(--red);   border-color: var(--red); }

  .stat-chip {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .stat-chip .label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }
  .stat-chip .value { font-size: 14px; color: var(--text); }

  /* ── Progress card ── */
  #encode-card { position: relative; }
  #encode-card.hidden { display: none; }

  #file-name { font-size: 15px; margin-bottom: 12px; color: var(--green); word-break: break-all; }
  #progress-bar-wrap {
    background: #222;
    border-radius: 4px;
    height: 8px;
    overflow: hidden;
    margin-bottom: 10px;
  }
  #progress-bar {
    height: 100%;
    background: var(--green);
    border-radius: 4px;
    transition: width 0.4s ease;
    width: 0%;
  }
  #progress-stats {
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 14px;
  }
  #progress-stats span { color: var(--text); }

  /* ── Queue card ── */
  #queue-card.hidden { display: none; }
  #queue-list { list-style: none; }
  #queue-list li {
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 8px;
  }
  #queue-list li:last-child { border-bottom: none; }
  #queue-list li::before { content: "›"; color: var(--border); }

  /* ── History card ── */
  #history-card { grid-column: 1 / -1; }
  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left;
    font-size: 10px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--muted);
    padding: 0 12px 10px 0;
    font-weight: 400;
  }
  td {
    padding: 8px 12px 8px 0;
    border-top: 1px solid var(--border);
    color: var(--text);
    vertical-align: middle;
  }
  tr:first-child td { border-top: none; }
  .badge-folder {
    font-size: 12px;
    padding: 2px 8px;
    background: #1e2a1e;
    border: 1px solid #2a422a;
    border-radius: 4px;
    color: var(--green);
  }
  #no-history { color: var(--muted); font-style: italic; }

  /* ── Log card ── */
  #log-card { grid-column: 1 / -1; }
  #log-output {
    background: #0a0a0a;
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 12px;
    height: 220px;
    overflow-y: auto;
    font-size: 11px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .log-info  { color: #ccc; }
  .log-warn  { color: var(--amber); }
  .log-error { color: var(--red); }
  .log-debug { color: #444; }
  .log-trigger { color: var(--blue); }
</style>
</head>
<body>

<header>
  <div>
    <h1>AMIS</h1>
    <p>Automatic Media Ingestion Server</p>
  </div>
  <div id="conn-pill" class="dead">● Connecting…</div>
</header>

<main>

  <!-- Status banner -->
  <div class="card" id="status-card">
    <div class="card-title">System Status</div>
    <div id="status-row">
      <div id="state-badge" class="idle">Idle</div>
      <div class="stat-chip">
        <span class="label">Device</span>
        <span class="value" id="stat-device">—</span>
      </div>
      <div class="stat-chip">
        <span class="label">Card</span>
        <span class="value" id="stat-card">—</span>
      </div>
      <div class="stat-chip">
        <span class="label">Backup</span>
        <span class="value" id="stat-backup">—</span>
      </div>
      <div class="stat-chip">
        <span class="label">Progress</span>
        <span class="value" id="stat-progress">—</span>
      </div>
    </div>
  </div>

  <!-- Current encode -->
  <div class="card hidden" id="encode-card">
    <div class="card-title">Current Encode</div>
    <div id="file-name">—</div>
    <div id="progress-bar-wrap">
      <div id="progress-bar"></div>
    </div>
    <div id="progress-stats">
      <div>FPS &nbsp;<span id="stat-fps">—</span></div>
      <div>Speed &nbsp;<span id="stat-speed">—</span></div>
      <div>File &nbsp;<span id="stat-file-n">—</span></div>
      <div>Done &nbsp;<span id="stat-done">—</span></div>
      <div>Failed &nbsp;<span id="stat-failed">—</span></div>
    </div>
  </div>

  <!-- Queue -->
  <div class="card hidden" id="queue-card">
    <div class="card-title">Queue</div>
    <ul id="queue-list"></ul>
  </div>

  <!-- History -->
  <div class="card" id="history-card">
    <div class="card-title">Backup History</div>
    <p id="no-history" style="display:none">No backups yet.</p>
    <table id="history-table">
      <thead>
        <tr>
          <th>Backup Folder</th>
          <th>Card</th>
          <th>Files</th>
          <th>Size</th>
          <th>Completed</th>
        </tr>
      </thead>
      <tbody id="history-body"></tbody>
    </table>
  </div>

  <!-- Log -->
  <div class="card" id="log-card">
    <div class="card-title">Log</div>
    <div id="log-output"></div>
  </div>

</main>

<script>
const $ = id => document.getElementById(id);

// ── WebSocket ──────────────────────────────────────────────────────────────
let ws;
let reconnectDelay = 1000;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    $('conn-pill').className = 'live';
    $('conn-pill').textContent = '● Live';
    reconnectDelay = 1000;
  };

  ws.onmessage = e => {
    const snap = JSON.parse(e.data);
    applyStatus(snap.status);
    applyLog(snap.log_lines);
    applyHistory(snap.history);
  };

  ws.onclose = () => {
    $('conn-pill').className = 'dead';
    $('conn-pill').textContent = '● Reconnecting…';
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 10000);
  };
}

connect();

// ── Status ─────────────────────────────────────────────────────────────────
function applyStatus(s) {
  const state = s.state || 'idle';
  const badge = $('state-badge');
  badge.textContent = state.charAt(0).toUpperCase() + state.slice(1);
  badge.className = state;

  $('stat-device').textContent = s.device || '—';
  $('stat-card').textContent   = s.card_label || '—';
  $('stat-backup').textContent = s.backup_folder || '—';

  const active = state === 'transcoding';
  const anyCard = ['mounting','scanning','transcoding','ejecting'].includes(state);

  // Encode card
  $('encode-card').classList.toggle('hidden', !active);
  $('queue-card').classList.toggle('hidden', !anyCard);

  if (active) {
    $('file-name').textContent = s.current_file || '—';

    const pct = s.progress_pct || 0;
    $('progress-bar').style.width = pct + '%';
    $('stat-progress').textContent = pct.toFixed(1) + '%';

    $('stat-fps').textContent    = s.fps > 0 ? s.fps.toFixed(1) : '—';
    $('stat-speed').textContent  = s.speed || '—';
    $('stat-file-n').textContent = s.total_files
      ? `${s.current_file_index} / ${s.total_files}`
      : '—';
    $('stat-done').textContent   = s.completed_files ?? '—';
    $('stat-failed').textContent = s.failed_files ?? '—';
  } else {
    $('stat-progress').textContent = state === 'done' ? '100%' : '—';
  }

  // Queue
  const qList = $('queue-list');
  qList.innerHTML = '';
  const queue = s.queue || [];
  if (queue.length === 0 && anyCard) {
    const li = document.createElement('li');
    li.textContent = 'No files queued';
    li.style.color = 'var(--muted)';
    qList.appendChild(li);
  }
  queue.forEach(name => {
    const li = document.createElement('li');
    li.textContent = name;
    qList.appendChild(li);
  });
}

// ── History ────────────────────────────────────────────────────────────────
function fmtBytes(bytes) {
  if (!bytes) return '—';
  const gb = bytes / 1073741824;
  return gb >= 1 ? gb.toFixed(2) + ' GB' : (bytes / 1048576).toFixed(0) + ' MB';
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

function applyHistory(rows) {
  const tbody = $('history-body');
  tbody.innerHTML = '';

  if (!rows || rows.length === 0) {
    $('no-history').style.display = 'block';
    $('history-table').style.display = 'none';
    return;
  }

  $('no-history').style.display = 'none';
  $('history-table').style.display = 'table';

  rows.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><span class="badge-folder">${r.backup_folder}</span></td>
      <td>${r.volume_label || '—'}</td>
      <td>${r.file_count}</td>
      <td>${fmtBytes(r.total_bytes)}</td>
      <td>${fmtDate(r.finished_at)}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ── Log ────────────────────────────────────────────────────────────────────
function applyLog(lines) {
  const out = $('log-output');
  if (!lines || lines.length === 0) return;

  out.innerHTML = lines.map(l => {
    let cls = 'log-info';
    if (l.includes('[ERROR]'))   cls = 'log-error';
    else if (l.includes('[WARN')) cls = 'log-warn';
    else if (l.includes('[DEBUG')) cls = 'log-debug';
    else if (l.includes('[TRIGGER]')) cls = 'log-trigger';
    const safe = l.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return `<div class="${cls}">${safe}</div>`;
  }).join('');

  // Auto-scroll to bottom
  out.scrollTop = out.scrollHeight;
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AMIS Web UI")
    parser.add_argument("--config", default="/etc/amis/config.yaml")
    args = parser.parse_args()

    global CONFIG
    CONFIG = load_config(args.config)

    port = CONFIG.get("web", {}).get("port", 8080)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
