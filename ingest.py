#!/usr/bin/env python3
"""
AMIS - Automatic Media Ingestion Server
Detects Sony SD card insertion, transcodes to H.265 1080p, uploads to SMB share.
"""

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(config_path: str = "/etc/amis/config.yaml") -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_file: str) -> logging.Logger:
    log_dir = Path(log_file).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("amis")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ---------------------------------------------------------------------------
# Status writer — communicates with the web UI via a shared JSON file
# ---------------------------------------------------------------------------

class StatusWriter:
    """Writes pipeline state atomically to a JSON file read by the web server."""

    _DEFAULTS = {
        "state": "idle",        # idle | mounting | scanning | transcoding | ejecting | error | done
        "device": None,
        "card_label": None,
        "current_file": None,
        "current_file_index": 0,
        "total_files": 0,
        "completed_files": 0,
        "failed_files": 0,
        "progress_pct": 0.0,
        "fps": 0.0,
        "speed": None,
        "backup_folder": None,
        "started_at": None,
        "updated_at": None,
        "queue": [],
    }

    def __init__(self, status_path: str):
        self._path = Path(status_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._state = dict(self._DEFAULTS)
        self._flush()

    def update(self, **kwargs):
        self._state.update(kwargs)
        self._state["updated_at"] = datetime.now().isoformat()
        self._flush()

    def reset(self):
        self._state = dict(self._DEFAULTS)
        self._state["updated_at"] = datetime.now().isoformat()
        self._flush()

    def _flush(self):
        # Atomic write: temp file → rename so the web server never reads a partial file
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.rename(self._path)


# ---------------------------------------------------------------------------
# Database — deduplication tracking
# ---------------------------------------------------------------------------

def open_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingested_files (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            filename      TEXT NOT NULL,
            file_size     INTEGER NOT NULL,
            volume_label  TEXT,
            backup_folder TEXT NOT NULL,
            ingested_at   TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_file
        ON ingested_files (filename, file_size, volume_label)
    """)
    conn.commit()
    return conn


def already_ingested(conn: sqlite3.Connection, filename: str, file_size: int, volume_label: str) -> bool:
    row = conn.execute(
        "SELECT id FROM ingested_files WHERE filename=? AND file_size=? AND volume_label=?",
        (filename, file_size, volume_label),
    ).fetchone()
    return row is not None


def mark_ingested(conn: sqlite3.Connection, filename: str, file_size: int, volume_label: str, backup_folder: str):
    conn.execute(
        """
        INSERT OR IGNORE INTO ingested_files (filename, file_size, volume_label, backup_folder, ingested_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (filename, file_size, volume_label, backup_folder, datetime.now().isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# SD card helpers
# ---------------------------------------------------------------------------

def mount_sd_card(device: str, mount_point: str, logger: logging.Logger) -> bool:
    Path(mount_point).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["mount", device, mount_point], capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"Failed to mount {device}: {result.stderr.strip()}")
        return False
    logger.info(f"Mounted {device} at {mount_point}")
    return True


def unmount_sd_card(mount_point: str, device: str, logger: logging.Logger):
    subprocess.run(["sync"], check=False)
    time.sleep(1)

    result = subprocess.run(["umount", mount_point], capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(f"umount failed: {result.stderr.strip()}")
    else:
        logger.info(f"Unmounted {mount_point}")

    eject_result = subprocess.run(["eject", device], capture_output=True, text=True)
    if eject_result.returncode == 0:
        logger.info(f"Ejected {device} — safe to remove card")
    else:
        logger.warning(f"eject returned non-zero: {eject_result.stderr.strip()}")


def get_volume_label(mount_point: str) -> str:
    try:
        fm = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", mount_point],
            capture_output=True, text=True
        )
        device = fm.stdout.strip()
        if device:
            bl = subprocess.run(
                ["blkid", "-s", "LABEL", "-o", "value", device],
                capture_output=True, text=True
            )
            label = bl.stdout.strip()
            if label:
                return label
    except Exception:
        pass
    return Path(mount_point).name


def find_media_files(mount_point: str, sony_config: dict) -> list[Path]:
    root = Path(mount_point)
    found = []
    extensions = {e.lower() for e in sony_config["extensions"]}

    for rel_path in sony_config["media_paths"]:
        search_dir = root / rel_path
        if not search_dir.is_dir():
            continue
        for f in search_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in extensions:
                found.append(f)

    return found


# ---------------------------------------------------------------------------
# SMB share helpers
# ---------------------------------------------------------------------------

def ensure_smb_mounted(smb_config: dict, logger: logging.Logger) -> bool:
    mount_point = smb_config["mount_point"]
    Path(mount_point).mkdir(parents=True, exist_ok=True)

    result = subprocess.run(["mountpoint", "-q", mount_point], capture_output=True)
    if result.returncode == 0:
        return True

    mount_result = subprocess.run(
        [
            "mount", "-t", "cifs",
            smb_config["host"], mount_point,
            "-o", f"username={smb_config['username']},password={smb_config['password']},vers=3.0",
        ],
        capture_output=True, text=True
    )
    if mount_result.returncode != 0:
        logger.error(f"Failed to mount SMB share: {mount_result.stderr.strip()}")
        return False

    logger.info(f"Mounted SMB share at {mount_point}")
    return True


def next_backup_folder(smb_mount: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    prefix = f"{today}_Backup_"
    smb_root = Path(smb_mount)

    existing = sorted(
        [d.name for d in smb_root.iterdir() if d.is_dir() and d.name.startswith(prefix)]
    )

    if existing:
        last_num = int(existing[-1].split("_")[-1])
        next_num = last_num + 1
    else:
        next_num = 1

    return f"{prefix}{next_num:03d}"


# ---------------------------------------------------------------------------
# FFmpeg transcoding with progress reporting
# ---------------------------------------------------------------------------

def get_duration_us(src: Path) -> int:
    """Return clip duration in microseconds via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(src),
        ],
        capture_output=True, text=True
    )
    try:
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                return int(float(stream.get("duration", 0)) * 1_000_000)
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return 0


def transcode(
    src: Path,
    dst: Path,
    output_config: dict,
    logger: logging.Logger,
    status: StatusWriter,
) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration_us = get_duration_us(src)

    cmd = [
        "ffmpeg",
        "-i", str(src),
        "-c:v", output_config["codec"],
        "-crf", str(output_config["crf"]),
        "-preset", output_config["preset"],
        "-vf", f"scale={output_config['resolution'].replace('x', ':')}",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-progress", "pipe:1",   # machine-readable progress on stdout
        "-nostats",               # suppress human-readable stats on stderr
        "-y",
        str(dst),
    ]

    logger.info(f"Transcoding: {src.name} → {dst.name}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Parse FFmpeg progress output line by line
    for line in proc.stdout:
        line = line.strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()

        if key == "out_time_us" and duration_us > 0:
            try:
                pct = min(100.0, int(val) / duration_us * 100)
                status.update(progress_pct=round(pct, 1))
            except ValueError:
                pass
        elif key == "fps":
            try:
                status.update(fps=round(float(val), 1))
            except ValueError:
                pass
        elif key == "speed":
            if val not in ("N/A", ""):
                status.update(speed=val)

    proc.wait()

    if proc.returncode != 0:
        stderr_tail = (proc.stderr.read() if proc.stderr else "")[-2000:]
        logger.error(f"FFmpeg failed for {src.name}:\n{stderr_tail}")
        if dst.exists():
            dst.unlink()
        return False

    src_mb = src.stat().st_size / 1_048_576
    dst_mb = dst.stat().st_size / 1_048_576
    ratio = (1 - dst_mb / src_mb) * 100 if src_mb > 0 else 0
    logger.info(f"Done: {src.name} | {src_mb:.1f} MB → {dst_mb:.1f} MB ({ratio:.0f}% smaller)")
    status.update(progress_pct=100.0)
    return True


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_ingest(device: str, config: dict, logger: logging.Logger):
    sd_mount = config["paths"]["sd_mount_point"]
    smb_cfg = config["smb"]
    out_cfg = config["output"]
    status = StatusWriter(config["paths"]["status_file"])

    status.update(state="mounting", device=device, started_at=datetime.now().isoformat())

    # 1. Mount SD card
    if not mount_sd_card(device, sd_mount, logger):
        status.update(state="error")
        return

    try:
        volume_label = get_volume_label(sd_mount)
        logger.info(f"Card label: {volume_label}")
        status.update(card_label=volume_label, state="scanning")

        # 2. Find media files
        media_files = find_media_files(sd_mount, config["sony"])
        if not media_files:
            logger.info("No media files found on card — nothing to do.")
            status.update(state="done")
            return

        logger.info(f"Found {len(media_files)} media file(s) on card")

        # 3. Dedup check
        conn = open_db(config["paths"]["db_file"])
        to_process = []
        for f in media_files:
            size = f.stat().st_size
            if already_ingested(conn, f.name, size, volume_label):
                logger.info(f"Skipping (already ingested): {f.name}")
            else:
                to_process.append(f)

        if not to_process:
            logger.info("All files already ingested — nothing to do.")
            status.update(state="done")
            return

        logger.info(f"{len(to_process)} new file(s) to ingest")

        # 4. Mount SMB share
        if not ensure_smb_mounted(smb_cfg, logger):
            status.update(state="error")
            return

        # 5. Create backup folder
        backup_name = next_backup_folder(smb_cfg["mount_point"])
        backup_path = Path(smb_cfg["mount_point"]) / backup_name
        backup_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Backup folder: {backup_name}")

        status.update(
            total_files=len(to_process),
            backup_folder=backup_name,
            queue=[f.name for f in to_process],
        )

        # 6. Transcode each file
        success_count = 0
        fail_count = 0

        for i, src in enumerate(to_process, start=1):
            remaining_queue = [f.name for f in to_process[i:]]
            status.update(
                state="transcoding",
                current_file=src.name,
                current_file_index=i,
                completed_files=success_count,
                failed_files=fail_count,
                progress_pct=0.0,
                fps=0.0,
                speed=None,
                queue=remaining_queue,
            )

            dst = backup_path / f"{src.stem}{out_cfg['extension']}"
            ok = transcode(src, dst, out_cfg, logger, status)

            if ok:
                mark_ingested(conn, src.name, src.stat().st_size, volume_label, backup_name)
                success_count += 1
            else:
                fail_count += 1

        logger.info(
            f"Ingest complete — {success_count} succeeded, {fail_count} failed | Backup: {backup_name}"
        )
        conn.close()
        status.update(
            state="ejecting",
            completed_files=success_count,
            failed_files=fail_count,
            current_file=None,
            queue=[],
        )

    finally:
        # 7. Always eject
        unmount_sd_card(sd_mount, device, logger)
        status.update(state="done")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AMIS - Automatic Media Ingestion Server")
    parser.add_argument("device", help="Block device to ingest (e.g. /dev/sdb1)")
    parser.add_argument("--config", default="/etc/amis/config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config["paths"]["log_file"])

    logger.info(f"=== AMIS triggered for device: {args.device} ===")

    try:
        run_ingest(args.device, config, logger)
    except Exception as e:
        logger.exception(f"Unhandled error during ingest: {e}")
        sys.exit(1)

    logger.info("=== AMIS session complete ===")


if __name__ == "__main__":
    main()
