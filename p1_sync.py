#!/usr/bin/env python3
"""p1_sync — pull new recordings off a HiDock P1 and run them through transmizer.

  python p1_sync.py            sync once (device optional: also finishes pending work)
  python p1_sync.py --watch    stay running; sync whenever the P1 is plugged in
  python p1_sync.py --status   show what the state file knows

The device is only ever read from. Settings live under `p1_sync:` in config.yaml.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import msvcrt
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import hidock_usb
import transmizer as tz

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "p1_state.json"
LOG_DIR = ROOT / "logs"
SYNC_LOG = LOG_DIR / "p1_sync.log"
WATCH_LOG = LOG_DIR / "p1_watch.log"

DEFAULTS = {
    "output_dir": "output",
    "download_dir": "input/p1",
    "since": None,
    "delete_audio_after_success": True,
    "poll_seconds": 5,
    "resync_minutes": 30,
    "max_attempts": 3,
}

# P1 recordings are 96 kbps MP3
BYTES_PER_SECOND = 12_000
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

log = logging.getLogger("p1_sync")


def load_settings() -> tuple[dict, dict]:
    cfg = tz.load_config(ROOT / "config.yaml")
    conf = {**DEFAULTS, **(cfg.get("p1_sync") or {})}
    for key in ("output_dir", "download_dir"):
        conf[key] = (ROOT / conf[key]).resolve()
    since = conf["since"]
    if isinstance(since, str):
        since = datetime.fromisoformat(since)
    elif isinstance(since, date) and not isinstance(since, datetime):
        since = datetime(since.year, since.month, since.day)
    conf["since"] = since
    return cfg, conf


# ── state ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


@contextmanager
def single_instance(name: str):
    """Yield True if we hold the named lock, False if another process does."""
    LOG_DIR.mkdir(exist_ok=True)
    handle = open(LOG_DIR / f"{name}.lock", "a+")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        handle.close()


# ── notifications ────────────────────────────────────────────────────────────

def notify(title: str, body: str, open_path: Path | None = None) -> None:
    launch = f" activationType=\"protocol\" launch={quoteattr(open_path.as_uri())}" if open_path else ""
    xml = (
        f"<toast{launch}><visual><binding template=\"ToastGeneric\">"
        f"<text>{escape(title)}</text><text>{escape(body)}</text>"
        "</binding></visual></toast>"
    )
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:P1_TOAST_XML);"
        "$t = New-Object Windows.UI.Notifications.ToastNotification $x;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:P1_TOAST_APP).Show($t)"
    )
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "P1_TOAST_XML": xml, "P1_TOAST_APP": TOAST_APP_ID},
            capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Toast failed: %s", exc)


# ── device phase ─────────────────────────────────────────────────────────────

def _maybe_still_recording(f: hidock_usb.DeviceFile, recording_name: str | None) -> bool:
    if f.name == recording_name:
        return True
    ends = f.recorded_at + timedelta(seconds=f.size / BYTES_PER_SECOND)
    return abs((datetime.now() - ends).total_seconds()) < 120


def pull(state: dict, conf: dict, announce: bool) -> None:
    """Download recordings the state file hasn't seen. Quietly does nothing if no device."""
    if not hidock_usb.is_present():
        log.info("P1 not connected; skipping download phase.")
        return
    try:
        with hidock_usb.HiDock() as dock:
            files = dock.list_files()
            recording = dock.recording_in_progress()
            wanted = []
            for f in files:
                if f.recorded_at is None or (conf["since"] and f.recorded_at < conf["since"]):
                    continue
                entry = state.get(f.name)
                if entry and not (entry["status"] == "downloaded" and not Path(entry["audio"]).exists()):
                    continue
                if _maybe_still_recording(f, recording):
                    log.info("Skipping %s (looks like it is still recording).", f.name)
                    continue
                wanted.append(f)
            log.info("%s: %d recording(s) on device, %d new.", dock.model, len(files), len(wanted))
            if not wanted:
                return
            if announce:
                notify("HiDock P1 connected", f"Downloading {len(wanted)} new recording(s)…")
            conf["download_dir"].mkdir(parents=True, exist_ok=True)
            for f in sorted(wanted, key=lambda f: f.recorded_at):
                dest = conf["download_dir"] / f"{f.recorded_at:%Y%m%d%H%M%S}.mp3"
                t0 = time.time()
                dock.download(f, dest)
                log.info("Downloaded %s → %s (%.1f MB, %.0fs)",
                         f.name, dest.name, f.size / 1e6, time.time() - t0)
                state[f.name] = {
                    "status": "downloaded",
                    "size": f.size,
                    "recorded_at": f.recorded_at.isoformat(),
                    "audio": str(dest),
                    "attempts": 0,
                }
                save_state(state)
    except hidock_usb.DeviceBusyError as exc:
        log.warning("%s", exc)
        if announce:
            notify("HiDock P1 is busy", "Close the hinotes.com tab, then unplug and replug the P1.")
    except Exception as exc:
        log.exception("Device phase failed: %s", exc)
        if announce:
            notify("HiDock P1 sync failed", f"{exc}\nSee {SYNC_LOG}")


# ── local phase ──────────────────────────────────────────────────────────────

def _ollama_up(host: str) -> bool:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=3):
            return True
    except Exception:
        return False


def ensure_ollama(host: str) -> bool:
    if _ollama_up(host):
        return True
    if "localhost" not in host and "127.0.0.1" not in host:
        return False
    exe = shutil.which("ollama")
    if not exe:
        return False
    log.info("Ollama is not running; starting it.")
    subprocess.Popen(
        [exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    for _ in range(30):
        time.sleep(1)
        if _ollama_up(host):
            return True
    return False


def process(state: dict, cfg: dict, conf: dict) -> tuple[list[Path], int]:
    """Transcribe + summarise everything pending. Returns (notes written, failures)."""
    pending = sorted(
        ((name, e) for name, e in state.items() if e["status"] in ("downloaded", "transcribed")),
        key=lambda item: item[1]["recorded_at"],
    )
    if not pending:
        return [], 0
    if not ensure_ollama(cfg["ollama"]["host"]):
        log.warning("Ollama is unreachable; recordings will be transcribed but not summarised.")

    notes: list[Path] = []
    failures = 0
    model = None
    for i, (name, entry) in enumerate(pending, 1):
        log.info("[%d/%d] %s", i, len(pending), name)
        out = None
        error = None
        try:
            transcript = Path(entry.get("transcript", ""))
            if entry["status"] == "transcribed" and transcript.is_file():
                out = tz.process_transcript_file(transcript, conf["output_dir"], cfg) or transcript
            else:
                model = model or tz.load_whisper(cfg)
                out = tz.process_file(Path(entry["audio"]), conf["output_dir"], cfg, model=model)
        except Exception as exc:
            log.exception("Processing %s failed: %s", name, exc)
            error = str(exc)

        if out is not None and out.suffix == ".md":
            entry.update(status="done", note=str(out))
            entry.pop("transcript", None)
            notes.append(out)
            if conf["delete_audio_after_success"]:
                Path(entry["audio"]).unlink(missing_ok=True)
        elif out is None and error is None:
            entry["status"] = "empty"
            log.warning("%s produced no transcript; audio kept at %s", name, entry["audio"])
        else:
            failures += 1
            entry["attempts"] = entry.get("attempts", 0) + 1
            if out is not None:
                entry.update(status="transcribed", transcript=str(out))
            if entry["attempts"] >= conf["max_attempts"]:
                entry["status"] = "failed"
                log.error("Giving up on %s after %d attempts.", name, entry["attempts"])
        entry["updated"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
    return notes, failures


def sync_once(announce_busy: bool = True) -> int:
    with single_instance("p1_sync") as acquired:
        if not acquired:
            log.info("Another sync is already running.")
            return 0
        cfg, conf = load_settings()
        state = load_state()
        pull(state, conf, announce_busy)
        notes, failures = process(state, cfg, conf)

    if notes:
        titles = "\n".join(n.stem for n in notes[:3])
        more = f"\n…and {len(notes) - 3} more" if len(notes) > 3 else ""
        notify(
            f"{len(notes)} meeting note{'s' if len(notes) != 1 else ''} ready",
            titles + more,
            notes[0] if len(notes) == 1 else conf["output_dir"],
        )
    if failures:
        notify("HiDock P1 sync", f"{failures} recording(s) could not be finished.\nSee {SYNC_LOG}")
    log.info("Sync finished: %d note(s), %d failure(s).", len(notes), failures)
    return 1 if failures else 0


# ── watcher ──────────────────────────────────────────────────────────────────

def _run_sync_subprocess(periodic: bool) -> None:
    if SYNC_LOG.exists() and SYNC_LOG.stat().st_size > 5_000_000:
        SYNC_LOG.replace(SYNC_LOG.with_suffix(".log.1"))
    python = Path(sys.executable).with_name("python.exe")
    cmd = [str(python), str(Path(__file__).resolve())] + (["--quiet-busy"] if periodic else [])
    with SYNC_LOG.open("ab") as out:
        result = subprocess.run(
            cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    log.info("Sync exited with code %d.", result.returncode)


def watch() -> None:
    with single_instance("p1_watch") as acquired:
        if not acquired:
            log.info("Another watcher is already running; exiting.")
            return
        _, conf = load_settings()
        resync_s = conf["resync_minutes"] * 60
        log.info("Watching for the HiDock P1 (poll %ss, resync %s min).",
                 conf["poll_seconds"], conf["resync_minutes"])
        was_present = False
        last_sync = 0.0
        while True:
            try:
                present = hidock_usb.is_present()
            except Exception as exc:
                log.warning("USB scan failed: %s", exc)
                present = False
            just_connected = present and not was_present
            resync_due = present and resync_s and time.monotonic() - last_sync >= resync_s
            if just_connected or resync_due:
                if just_connected:
                    log.info("P1 connected.")
                    time.sleep(3)
                try:
                    _run_sync_subprocess(periodic=not just_connected)
                except Exception as exc:
                    log.exception("Could not run sync: %s", exc)
                last_sync = time.monotonic()
            elif was_present and not present:
                log.info("P1 disconnected.")
            was_present = present
            time.sleep(conf["poll_seconds"])


# ── CLI ──────────────────────────────────────────────────────────────────────

def print_status() -> None:
    _, conf = load_settings()
    state = load_state()
    counts: dict[str, int] = {}
    for entry in state.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print(f"Device connected: {hidock_usb.is_present()}")
    print(f"Since: {conf['since']}  Output: {conf['output_dir']}")
    print(f"Tracked recordings: {counts or 'none'}")
    for name, entry in sorted(state.items(), key=lambda item: item[1]["recorded_at"]):
        detail = Path(entry["note"]).name if entry.get("note") else ""
        print(f"  {entry['recorded_at']}  {entry['status']:11}  {name}  {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", action="store_true", help="Run forever; sync on device connect")
    parser.add_argument("--status", action="store_true", help="Show tracked recordings and exit")
    parser.add_argument("--quiet-busy", action="store_true",
                        help="No toast when the device is in use or has nothing new (periodic re-checks)")
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if args.watch:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            WATCH_LOG, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    else:
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    if args.status:
        print_status()
    elif args.watch:
        watch()
    else:
        sys.exit(sync_once(announce_busy=not args.quiet_busy))


if __name__ == "__main__":
    main()
