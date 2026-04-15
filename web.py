#!/usr/bin/env python3
"""
AMIS Web UI — real-time dashboard served over HTTP/WebSocket.
Run with: python3 web.py --config /etc/amis/config.yaml
"""

import argparse
import asyncio
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

CONFIG_PATH: str = "/etc/amis/config.yaml"
CONFIG: dict = {}

# Last SMB test result — persists in memory, broadcast to all clients
SMB_STATUS: dict = {"state": "untested", "message": "", "tested_at": None}
# states: "untested" | "ok" | "fail"


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def save_config(path: str, cfg: dict):
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
    tmp.rename(path)


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
        "smb_status": SMB_STATUS,
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
# Background broadcaster
# ---------------------------------------------------------------------------

async def broadcaster():
    last_snapshot = {}
    while True:
        await asyncio.sleep(0.5)
        if manager.count == 0:
            continue
        snap = build_snapshot()
        if snap != last_snapshot:
            await manager.broadcast(snap)
            last_snapshot = snap


@app.on_event("startup")
async def startup():
    asyncio.create_task(broadcaster())


# ---------------------------------------------------------------------------
# Settings models
# ---------------------------------------------------------------------------

class SmbSettings(BaseModel):
    host: str
    username: str
    password: str          # "__unchanged__" means keep existing value
    mount_point: str


class OutputSettings(BaseModel):
    crf: int
    preset: str
    resolution: str


class SettingsPayload(BaseModel):
    smb: SmbSettings
    output: OutputSettings


# ---------------------------------------------------------------------------
# Routes — dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTML


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_text(json.dumps(build_snapshot()))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)


@app.get("/api/snapshot")
async def api_snapshot():
    return build_snapshot()


# ---------------------------------------------------------------------------
# Routes — settings
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def api_get_config():
    """Return current config, with the SMB password masked."""
    smb = CONFIG.get("smb", {})
    out = CONFIG.get("output", {})
    return {
        "smb": {
            "host":        smb.get("host", ""),
            "username":    smb.get("username", ""),
            "password":    "••••••••" if smb.get("password") else "",
            "mount_point": smb.get("mount_point", "/mnt/smb"),
        },
        "output": {
            "crf":        out.get("crf", 22),
            "preset":     out.get("preset", "medium"),
            "resolution": out.get("resolution", "1920x1080"),
        },
    }


@app.post("/api/config")
async def api_save_config(payload: SettingsPayload):
    """Validate and persist config changes."""
    global CONFIG

    # Reload fresh copy so we don't clobber fields we don't own
    cfg = load_config(CONFIG_PATH)

    cfg["smb"]["host"]        = payload.smb.host.strip()
    cfg["smb"]["username"]    = payload.smb.username.strip()
    cfg["smb"]["mount_point"] = payload.smb.mount_point.strip()

    # Only update password if the user typed a new one
    if payload.smb.password and not set(payload.smb.password).issubset({"•", "*"}):
        cfg["smb"]["password"] = payload.smb.password

    cfg["output"]["crf"]        = payload.output.crf
    cfg["output"]["preset"]     = payload.output.preset
    cfg["output"]["resolution"] = payload.output.resolution

    save_config(CONFIG_PATH, cfg)
    CONFIG = cfg  # hot-reload in the web process

    return {"ok": True}


@app.post("/api/test-smb")
async def api_test_smb(payload: SmbSettings):
    """
    Try mounting the SMB share with the supplied credentials.
    Mounts to a temp directory and immediately unmounts.
    Updates SMB_STATUS and broadcasts to all connected clients.
    """
    global SMB_STATUS

    from datetime import datetime

    host        = payload.host.strip()
    username    = payload.username.strip()

    # If password is the masked placeholder, use the saved one
    password = payload.password
    if not password or set(password).issubset({"•", "*"}):
        password = CONFIG.get("smb", {}).get("password", "")

    if not host or not username:
        SMB_STATUS = {"state": "fail", "message": "Host and username are required.", "tested_at": datetime.now().isoformat()}
        await manager.broadcast(build_snapshot())
        return JSONResponse({"ok": False, "message": SMB_STATUS["message"]})

    with tempfile.TemporaryDirectory() as tmp_mount:
        result = subprocess.run(
            [
                "mount", "-t", "cifs",
                host, tmp_mount,
                "-o", f"username={username},password={password},vers=3.0",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode == 0:
            subprocess.run(["umount", tmp_mount], capture_output=True)
            SMB_STATUS = {
                "state": "ok",
                "message": f"Connected to {host}",
                "tested_at": datetime.now().isoformat(),
            }
            await manager.broadcast(build_snapshot())
            return {"ok": True, "message": SMB_STATUS["message"]}
        else:
            err = result.stderr.strip() or "Unknown error"
            err = err.replace(password, "***") if password else err
            SMB_STATUS = {"state": "fail", "message": err, "tested_at": datetime.now().isoformat()}
            await manager.broadcast(build_snapshot())
            return JSONResponse({"ok": False, "message": err})


# ---------------------------------------------------------------------------
# Dashboard HTML
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
    --surface2: #1e1e1e;
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
    overflow-x: hidden;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    position: sticky;
    top: 0;
    z-index: 10;
  }
  header h1 { font-size: 18px; font-weight: 700; letter-spacing: 3px; color: var(--green); }
  header p  { font-size: 11px; color: var(--muted); margin-top: 2px; }

  .header-right { display: flex; align-items: center; gap: 12px; }

  #conn-pill {
    font-size: 11px; padding: 4px 10px; border-radius: 20px;
    border: 1px solid var(--border); color: var(--muted); transition: all 0.3s;
  }
  #conn-pill.live { color: var(--green); border-color: var(--green); }
  #conn-pill.dead { color: var(--red);   border-color: var(--red); }

  #settings-btn {
    background: none; border: 1px solid var(--border); color: var(--muted);
    border-radius: 6px; padding: 5px 10px; cursor: pointer; font-size: 16px;
    transition: all 0.2s; font-family: var(--font);
  }
  #settings-btn:hover { border-color: var(--text); color: var(--text); }

  /* ── Layout ── */
  main {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    padding: 20px 24px;
    max-width: 1400px;
    margin: 0 auto;
  }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }

  /* ── Cards ── */
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 18px 20px;
  }
  .card-title {
    font-size: 10px; letter-spacing: 2px; text-transform: uppercase;
    color: var(--muted); margin-bottom: 14px;
    border-bottom: 1px solid var(--border); padding-bottom: 8px;
  }

  /* ── Status card ── */
  #status-card { grid-column: 1 / -1; }
  #status-row { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }

  #state-badge {
    font-size: 12px; font-weight: 700; letter-spacing: 1.5px;
    text-transform: uppercase; padding: 5px 14px; border-radius: 4px; border: 1px solid;
  }
  #state-badge.idle        { color: var(--muted); border-color: var(--muted); }
  #state-badge.mounting,
  #state-badge.scanning,
  #state-badge.ejecting    { color: var(--amber); border-color: var(--amber); }
  #state-badge.transcoding { color: var(--green); border-color: var(--green);
                              box-shadow: 0 0 8px rgba(0,230,118,0.2); }
  #state-badge.done        { color: var(--blue);  border-color: var(--blue); }
  #state-badge.error       { color: var(--red);   border-color: var(--red); }

  .stat-chip { display: flex; flex-direction: column; gap: 2px; }
  .stat-chip .label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }
  .stat-chip .value { font-size: 14px; color: var(--text); }

  /* ── Encode card ── */
  #encode-card.hidden { display: none; }
  #file-name { font-size: 15px; margin-bottom: 12px; color: var(--green); word-break: break-all; }
  #progress-bar-wrap { background: #222; border-radius: 4px; height: 8px; overflow: hidden; margin-bottom: 10px; }
  #progress-bar { height: 100%; background: var(--green); border-radius: 4px; transition: width 0.4s ease; width: 0%; }
  #progress-stats { display: flex; gap: 20px; flex-wrap: wrap; color: var(--muted); font-size: 12px; }
  #progress-stats span { color: var(--text); }

  /* ── Queue card ── */
  #queue-card.hidden { display: none; }
  #queue-list { list-style: none; }
  #queue-list li {
    padding: 6px 0; border-bottom: 1px solid var(--border);
    color: var(--muted); display: flex; align-items: center; gap: 8px;
  }
  #queue-list li:last-child { border-bottom: none; }
  #queue-list li::before { content: "›"; color: var(--border); }

  /* ── History card ── */
  #history-card { grid-column: 1 / -1; }
  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left; font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase;
    color: var(--muted); padding: 0 12px 10px 0; font-weight: 400;
  }
  td { padding: 8px 12px 8px 0; border-top: 1px solid var(--border); color: var(--text); vertical-align: middle; }
  tr:first-child td { border-top: none; }
  .badge-folder {
    font-size: 12px; padding: 2px 8px; background: #1e2a1e;
    border: 1px solid #2a422a; border-radius: 4px; color: var(--green);
  }
  #no-history { color: var(--muted); font-style: italic; }

  /* ── Log card ── */
  #log-card { grid-column: 1 / -1; }
  #log-output {
    background: #0a0a0a; border: 1px solid var(--border); border-radius: 4px;
    padding: 12px; height: 220px; overflow-y: auto; font-size: 11px;
    line-height: 1.6; white-space: pre-wrap; word-break: break-all;
  }
  .log-info    { color: #ccc; }
  .log-warn    { color: var(--amber); }
  .log-error   { color: var(--red); }
  .log-debug   { color: #444; }
  .log-trigger { color: var(--blue); }

  /* ── SMB status chip ── */
  .smb-chip {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 11px; padding: 5px 12px; border-radius: 4px;
    border: 1px solid; cursor: pointer; transition: all 0.3s;
    user-select: none; margin-left: auto;
  }
  .smb-chip:hover { opacity: 0.8; }
  .smb-chip.untested { color: var(--muted); border-color: var(--border); }
  .smb-chip.ok       { color: var(--green); border-color: var(--green); background: rgba(0,230,118,0.06); }
  .smb-chip.fail     { color: var(--red);   border-color: var(--red);   background: rgba(255,82,82,0.06); }

  /* ── Settings drawer ── */
  #drawer-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,0.6); z-index: 100; backdrop-filter: blur(2px);
  }
  #drawer-overlay.open { display: block; }

  #drawer {
    position: fixed; top: 0; right: -480px; width: 460px; height: 100vh;
    background: #111; border-left: 1px solid var(--border);
    overflow-y: auto; z-index: 101; transition: right 0.28s ease; padding: 0;
    display: flex; flex-direction: column;
  }
  #drawer.open { right: 0; }
  @media (max-width: 500px) { #drawer { width: 100vw; right: -100vw; } }

  .drawer-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 18px 22px; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; background: #111; z-index: 1;
  }
  .drawer-header h2 { font-size: 14px; letter-spacing: 2px; color: var(--text); }
  #drawer-close {
    background: none; border: none; color: var(--muted); font-size: 20px;
    cursor: pointer; line-height: 1; padding: 4px 8px;
  }
  #drawer-close:hover { color: var(--text); }

  .drawer-body { padding: 24px 22px; flex: 1; }

  .settings-section {
    margin-bottom: 28px; padding-bottom: 28px; border-bottom: 1px solid var(--border);
  }
  .settings-section:last-child { border-bottom: none; }
  .settings-section-title {
    font-size: 10px; letter-spacing: 2px; text-transform: uppercase;
    color: var(--muted); margin-bottom: 16px;
  }

  .field { margin-bottom: 14px; }
  .field label { display: block; font-size: 11px; color: var(--muted); margin-bottom: 5px; }
  .field input, .field select {
    width: 100%; background: var(--surface2); border: 1px solid var(--border);
    color: var(--text); border-radius: 5px; padding: 8px 10px;
    font-family: var(--font); font-size: 13px; outline: none;
    transition: border-color 0.2s;
  }
  .field input:focus, .field select:focus { border-color: var(--green); }
  .field select option { background: #1e1e1e; }

  .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

  .pw-wrap { position: relative; }
  .pw-wrap input { padding-right: 36px; }
  .pw-toggle {
    position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
    background: none; border: none; color: var(--muted); cursor: pointer;
    font-size: 14px; padding: 2px 4px;
  }
  .pw-toggle:hover { color: var(--text); }

  .hint { font-size: 10px; color: var(--muted); margin-top: 4px; }

  /* ── Test / Save buttons ── */
  .drawer-footer {
    padding: 16px 22px; border-top: 1px solid var(--border);
    display: flex; gap: 10px; align-items: center;
    position: sticky; bottom: 0; background: #111;
  }
  .btn {
    font-family: var(--font); font-size: 12px; font-weight: 600;
    letter-spacing: 1px; padding: 8px 18px; border-radius: 5px;
    cursor: pointer; border: 1px solid; transition: all 0.2s;
  }
  .btn-ghost  { background: none; color: var(--muted); border-color: var(--border); }
  .btn-ghost:hover  { color: var(--text); border-color: var(--text); }
  .btn-test   { background: none; color: var(--blue); border-color: var(--blue); }
  .btn-test:hover   { background: rgba(64,196,255,0.1); }
  .btn-save   { background: var(--green); color: #000; border-color: var(--green); }
  .btn-save:hover   { opacity: 0.85; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  #test-result {
    font-size: 11px; margin-left: 4px; flex: 1; text-align: right;
    transition: color 0.2s;
  }
  #test-result.ok  { color: var(--green); }
  #test-result.err { color: var(--red); }
  #test-result.pending { color: var(--amber); }
</style>
</head>
<body>

<header>
  <div>
    <h1>AMIS</h1>
    <p>Automatic Media Ingestion Server</p>
  </div>
  <div class="header-right">
    <div id="conn-pill" class="dead">● Connecting…</div>
    <button id="settings-btn" onclick="openDrawer()" title="Settings">⚙</button>
  </div>
</header>

<main>
  <!-- Status -->
  <div class="card" id="status-card">
    <div class="card-title">System Status</div>
    <div id="status-row">
      <div id="state-badge" class="idle">Idle</div>
      <div class="stat-chip"><span class="label">Device</span><span class="value" id="stat-device">—</span></div>
      <div class="stat-chip"><span class="label">Card</span><span class="value" id="stat-card">—</span></div>
      <div class="stat-chip"><span class="label">Backup</span><span class="value" id="stat-backup">—</span></div>
      <div class="stat-chip"><span class="label">Progress</span><span class="value" id="stat-progress">—</span></div>
      <div id="smb-chip" class="smb-chip untested" title="" onclick="openDrawer()">
        <span id="smb-chip-dot">●</span> SMB <span id="smb-chip-label">Untested</span>
      </div>
    </div>
  </div>

  <!-- Encode -->
  <div class="card hidden" id="encode-card">
    <div class="card-title">Current Encode</div>
    <div id="file-name">—</div>
    <div id="progress-bar-wrap"><div id="progress-bar"></div></div>
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
      <thead><tr><th>Backup Folder</th><th>Card</th><th>Files</th><th>Size</th><th>Completed</th></tr></thead>
      <tbody id="history-body"></tbody>
    </table>
  </div>

  <!-- Log -->
  <div class="card" id="log-card">
    <div class="card-title">Log</div>
    <div id="log-output"></div>
  </div>
</main>

<!-- Settings drawer -->
<div id="drawer-overlay" onclick="closeDrawer()"></div>
<div id="drawer">
  <div class="drawer-header">
    <h2>SETTINGS</h2>
    <button id="drawer-close" onclick="closeDrawer()">✕</button>
  </div>
  <div class="drawer-body">

    <!-- SMB -->
    <div class="settings-section">
      <div class="settings-section-title">SMB Share</div>

      <div class="field">
        <label>Share Path</label>
        <input id="smb-host" type="text" placeholder="//192.168.1.100/media">
        <div class="hint">UNC path to your network share</div>
      </div>

      <div class="field-row">
        <div class="field">
          <label>Username</label>
          <input id="smb-user" type="text" autocomplete="off">
        </div>
        <div class="field">
          <label>Password</label>
          <div class="pw-wrap">
            <input id="smb-pass" type="password" autocomplete="new-password" placeholder="unchanged">
            <button class="pw-toggle" onclick="togglePw()" title="Show/hide">👁</button>
          </div>
        </div>
      </div>

      <div class="field">
        <label>Mount Point</label>
        <input id="smb-mount" type="text" placeholder="/mnt/smb">
        <div class="hint">Where the share is mounted on this server</div>
      </div>
    </div>

    <!-- Output -->
    <div class="settings-section">
      <div class="settings-section-title">Encode Settings</div>

      <div class="field-row">
        <div class="field">
          <label>CRF Quality (0–51)</label>
          <input id="out-crf" type="number" min="0" max="51" step="1">
          <div class="hint">Lower = better quality, larger file</div>
        </div>
        <div class="field">
          <label>Resolution</label>
          <select id="out-res">
            <option value="1920x1080">1080p (1920×1080)</option>
            <option value="1280x720">720p (1280×720)</option>
            <option value="3840x2160">4K (3840×2160)</option>
          </select>
        </div>
      </div>

      <div class="field">
        <label>FFmpeg Preset</label>
        <select id="out-preset">
          <option value="ultrafast">ultrafast — fastest, largest</option>
          <option value="superfast">superfast</option>
          <option value="veryfast">veryfast</option>
          <option value="faster">faster</option>
          <option value="fast">fast</option>
          <option value="medium">medium — balanced (default)</option>
          <option value="slow">slow</option>
          <option value="slower">slower</option>
          <option value="veryslow">veryslow — slowest, smallest</option>
        </select>
        <div class="hint">Slower preset = smaller file at same quality</div>
      </div>
    </div>

  </div><!-- /drawer-body -->

  <div class="drawer-footer">
    <button class="btn btn-test" id="btn-test" onclick="testSmb()">Test Connection</button>
    <span id="test-result"></span>
    <button class="btn btn-save" id="btn-save" onclick="saveSettings()">Save</button>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

// ── WebSocket ──────────────────────────────────────────────────────────────
let ws, reconnectDelay = 1000;

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
    applySmbStatus(snap.smb_status);
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

  $('encode-card').classList.toggle('hidden', !active);
  $('queue-card').classList.toggle('hidden', !anyCard);

  if (active) {
    $('file-name').textContent = s.current_file || '—';
    const pct = s.progress_pct || 0;
    $('progress-bar').style.width = pct + '%';
    $('stat-progress').textContent = pct.toFixed(1) + '%';
    $('stat-fps').textContent   = s.fps > 0 ? s.fps.toFixed(1) : '—';
    $('stat-speed').textContent = s.speed || '—';
    $('stat-file-n').textContent = s.total_files ? `${s.current_file_index} / ${s.total_files}` : '—';
    $('stat-done').textContent   = s.completed_files ?? '—';
    $('stat-failed').textContent = s.failed_files ?? '—';
  } else {
    $('stat-progress').textContent = state === 'done' ? '100%' : '—';
  }

  const qList = $('queue-list');
  qList.innerHTML = '';
  (s.queue || []).forEach(name => {
    const li = document.createElement('li');
    li.textContent = name;
    qList.appendChild(li);
  });
}

// ── History ────────────────────────────────────────────────────────────────
function fmtBytes(b) {
  if (!b) return '—';
  const gb = b / 1073741824;
  return gb >= 1 ? gb.toFixed(2) + ' GB' : (b / 1048576).toFixed(0) + ' MB';
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
      <td>${fmtDate(r.finished_at)}</td>`;
    tbody.appendChild(tr);
  });
}

// ── Log ────────────────────────────────────────────────────────────────────
function applyLog(lines) {
  const out = $('log-output');
  if (!lines || !lines.length) return;
  out.innerHTML = lines.map(l => {
    let cls = 'log-info';
    if (l.includes('[ERROR]'))    cls = 'log-error';
    else if (l.includes('[WARN')) cls = 'log-warn';
    else if (l.includes('[DEBUG')) cls = 'log-debug';
    else if (l.includes('[TRIGGER]')) cls = 'log-trigger';
    const s = l.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return `<div class="${cls}">${s}</div>`;
  }).join('');
  out.scrollTop = out.scrollHeight;
}

// ── SMB status chip ────────────────────────────────────────────────────────
function applySmbStatus(s) {
  if (!s) return;
  const chip = $('smb-chip');
  const label = $('smb-chip-label');
  chip.className = `smb-chip ${s.state}`;

  if (s.state === 'ok') {
    label.textContent = 'Connected';
    chip.title = s.message + (s.tested_at ? '\n' + fmtDate(s.tested_at) : '');
  } else if (s.state === 'fail') {
    label.textContent = 'Failed';
    chip.title = s.message + (s.tested_at ? '\n' + fmtDate(s.tested_at) : '');
  } else {
    label.textContent = 'Untested';
    chip.title = 'Click to open Settings and test the SMB connection';
  }
}

// ── Settings drawer ────────────────────────────────────────────────────────
function openDrawer() {
  $('drawer-overlay').classList.add('open');
  $('drawer').classList.add('open');
  loadSettings();
}
function closeDrawer() {
  $('drawer-overlay').classList.remove('open');
  $('drawer').classList.remove('open');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

async function loadSettings() {
  const res = await fetch('/api/config');
  const cfg = await res.json();

  $('smb-host').value  = cfg.smb.host;
  $('smb-user').value  = cfg.smb.username;
  $('smb-pass').value  = '';   // never pre-fill password
  $('smb-pass').placeholder = cfg.smb.password ? 'unchanged (set)' : 'not set';
  $('smb-mount').value = cfg.smb.mount_point;

  $('out-crf').value    = cfg.output.crf;
  $('out-res').value    = cfg.output.resolution;
  $('out-preset').value = cfg.output.preset;

  $('test-result').textContent = '';
  $('test-result').className = '';
}

function togglePw() {
  const f = $('smb-pass');
  f.type = f.type === 'password' ? 'text' : 'password';
}

function buildSmbPayload() {
  return {
    host:        $('smb-host').value.trim(),
    username:    $('smb-user').value.trim(),
    password:    $('smb-pass').value || '••••••••',
    mount_point: $('smb-mount').value.trim(),
  };
}

async function testSmb() {
  const btn = $('btn-test');
  const result = $('test-result');
  btn.disabled = true;
  result.className = 'pending';
  result.textContent = 'Testing…';

  try {
    const res = await fetch('/api/test-smb', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(buildSmbPayload()),
    });
    const data = await res.json();
    result.className = data.ok ? 'ok' : 'err';
    result.textContent = data.message;
  } catch(e) {
    result.className = 'err';
    result.textContent = 'Request failed.';
  } finally {
    btn.disabled = false;
  }
}

async function saveSettings() {
  const btn = $('btn-save');
  btn.disabled = true;
  btn.textContent = 'Saving…';

  const payload = {
    smb: buildSmbPayload(),
    output: {
      crf:        parseInt($('out-crf').value),
      preset:     $('out-preset').value,
      resolution: $('out-res').value,
    },
  };

  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.ok) {
      btn.textContent = 'Saved ✓';
      btn.style.background = 'var(--blue)';
      setTimeout(() => {
        btn.textContent = 'Save';
        btn.style.background = '';
        btn.disabled = false;
      }, 2000);
    } else {
      btn.textContent = 'Error';
      btn.disabled = false;
    }
  } catch(e) {
    btn.textContent = 'Error';
    btn.disabled = false;
  }
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

    global CONFIG, CONFIG_PATH
    CONFIG_PATH = args.config
    CONFIG = load_config(CONFIG_PATH)

    port = CONFIG.get("web", {}).get("port", 8080)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
