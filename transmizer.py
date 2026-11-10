#!/usr/bin/env python3
"""transmizer — local audio transcription + summarization"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from textwrap import dedent

import yaml

PROMPTS_DIR = Path(__file__).parent / "prompts"

# ─── constants ────────────────────────────────────────────────────────────────

AUDIO_EXTS = frozenset(
    ".mp3 .mp4 .m4a .wav .ogg .flac .webm .opus .wma .aac .caf .aiff .au".split()
)

# Preferred extension order when the same stem has multiple formats.
# Earlier = higher priority; .opus is last because it often fails to decode.
_EXT_PRIORITY = ".wav .mp3 .m4a .flac .aac .ogg .webm .wma .caf .aiff .au .opus".split()


def dedup_by_stem(files: list[Path]) -> list[Path]:
    """Keep only one file per (directory, stem) pair, preferring higher-priority extensions."""
    best: dict[tuple[Path, str], Path] = {}
    for f in files:
        key = (f.parent, f.stem)
        current = best.get(key)
        if current is None:
            best[key] = f
        else:
            cur_rank = _EXT_PRIORITY.index(current.suffix.lower()) if current.suffix.lower() in _EXT_PRIORITY else len(_EXT_PRIORITY)
            new_rank = _EXT_PRIORITY.index(f.suffix.lower()) if f.suffix.lower() in _EXT_PRIORITY else len(_EXT_PRIORITY)
            if new_rank < cur_rank:
                best[key] = f
    return [best[k] for k in sorted(best, key=lambda k: str(k))]

TYPE_PATTERNS: dict[str, re.Pattern] = {
    "1on1": re.compile(r"1.?on.?1|one[-_]?on[-_]?one", re.I),
    "medical": re.compile(
        r"doctor|medical|clinic|health|dr[._]|appointment|appt|therapy|therapist"
        r"|dentist|physio|hospital|checkup|check[-_]up",
        re.I,
    ),
    "work": re.compile(
        r"meeting|standup|stand[-_]?up|sync|retro|sprint|kickoff|kick[-_]?off"
        r"|team|weekly|monthly|quarterly|review|planning|grooming|scrum|agile",
        re.I,
    ),
}

DEFAULT_CONFIG = {
    "whisper": {
        "model": "base.en",  # tiny.en / base.en / small.en / medium.en / large-v3
        "device": "cuda",
        "compute_type": "float16",  # float16 is optimal for NVIDIA
        "device_index": 0,          # GPU for tiny/base models
        "device_index_large": None, # GPU for small/medium/large; falls back to device_index
        "language": None,
        "cache_dir": None,  # override ~/.cache/huggingface/hub
    },
    "ollama": {
        "host": "http://localhost:11434",
        "model": "qwen3-coder:30b",
        "timeout": 600,
        "num_ctx": 32768,
    },
    "output": {
        "include_transcript": True,
        "transcript_collapsible": True,
    },
    "merge": {
        "gap_minutes": 60.0,   # files within this window are grouped
        "llm_check": False,    # double-check relevance with LLM (slower)
    },
}

# ─── prompts ──────────────────────────────────────────────────────────────────

def build_prompt(meeting_type: str, date: str, transcript: str) -> str:
    prompt_file = PROMPTS_DIR / f"{meeting_type}.md"
    if not prompt_file.exists():
        prompt_file = PROMPTS_DIR / "general.md"
    if not prompt_file.exists():
        print(f"ERROR: prompt file not found: {prompt_file}", file=sys.stderr)
        sys.exit(1)
    template = prompt_file.read_text(encoding="utf-8")
    return template.replace("{date}", date).replace("{transcript}", transcript)


# ─── config ───────────────────────────────────────────────────────────────────

def load_config(config_path: Path) -> dict:
    cfg = {k: (v.copy() if isinstance(v, dict) else v) for k, v in DEFAULT_CONFIG.items()}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        for section, values in user.items():
            if isinstance(values, dict) and section in cfg and isinstance(cfg[section], dict):
                cfg[section].update(values)
            else:
                cfg[section] = values
    return cfg


# ─── meeting type detection ───────────────────────────────────────────────────

def detect_meeting_type(path: Path) -> str:
    name = path.stem
    for mtype, pattern in TYPE_PATTERNS.items():
        if pattern.search(name):
            return mtype
    return "general"


# ─── smart merge ──────────────────────────────────────────────────────────────

def parse_filename_timestamp(stem: str) -> datetime | None:
    """Try common timestamp patterns embedded in filenames."""
    candidates = [
        # YYYYMMDDHHmmSS  (e.g. 20240130024338)
        (re.compile(r'(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})'),
         lambda g: datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]), int(g[5]))),
        # YYYYMMDDHHmm
        (re.compile(r'(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})'),
         lambda g: datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]))),
        # YYYY-MM-DD_HH-MM-SS  or  YYYY-MM-DDTHH:MM:SS
        (re.compile(r'(\d{4})[-_](\d{2})[-_](\d{2})[T_ ](\d{2})[-:_](\d{2})[-:_](\d{2})'),
         lambda g: datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]), int(g[5]))),
        # YYYY-MM-DD_HH-MM
        (re.compile(r'(\d{4})[-_](\d{2})[-_](\d{2})[T_ ](\d{2})[-:_](\d{2})'),
         lambda g: datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]))),
        # YYYY-MM-DD
        (re.compile(r'(\d{4})[-_](\d{2})[-_](\d{2})'),
         lambda g: datetime(int(g[0]), int(g[1]), int(g[2]))),
    ]
    for pattern, builder in candidates:
        m = pattern.search(stem)
        if m:
            try:
                return builder(m.groups())
            except (ValueError, TypeError):
                continue
    return None


def get_file_time(p: Path) -> float:
    """Best-effort: parse timestamp from filename, fall back to mtime."""
    ts = parse_filename_timestamp(p.stem)
    return ts.timestamp() if ts else p.stat().st_mtime


def group_files(files: list[Path], max_gap_minutes: float) -> list[list[Path]]:
    """Cluster audio files into sessions based on temporal proximity."""
    if not files:
        return []
    sorted_files = sorted(files, key=get_file_time)
    groups: list[list[Path]] = [[sorted_files[0]]]
    for f in sorted_files[1:]:
        gap_mins = (get_file_time(f) - get_file_time(groups[-1][-1])) / 60.0
        if gap_mins <= max_gap_minutes:
            groups[-1].append(f)
        else:
            groups.append([f])
    return groups


def infer_meeting_type(transcript: str, cfg: dict) -> str:
    """Classify the recording type from a short transcript excerpt via LLM."""
    available = [p.stem for p in sorted(PROMPTS_DIR.glob("*.md"))]
    if not available:
        return "general"

    excerpt = " ".join(transcript.split()[:300])
    types_str = ", ".join(available)
    prompt = (
        "/no_think\n"
        "Classify the following transcript excerpt into exactly one recording type.\n\n"
        f"Available types: {types_str}\n\n"
        f"TRANSCRIPT EXCERPT:\n{excerpt}\n\n"
        f"Reply with ONLY the type name ({types_str})."
    )
    result = _ollama_quick(prompt, cfg, num_ctx=2048).strip().lower()
    answer = result.split()[0] if result else ""
    if answer in available:
        return answer
    for t in available:
        if t in answer:
            return t
    return "general"


def resolve_meeting_type(
    path: Path,
    transcript: str,
    explicit: str | None,
    cfg: dict,
) -> str:
    """Explicit flag > filename pattern > LLM inference."""
    if explicit:
        return explicit
    detected = detect_meeting_type(path)
    if detected != "general":
        return detected
    print("  Inferring type from transcript...", end=" ", flush=True)
    inferred = infer_meeting_type(transcript, cfg)
    print(inferred)
    return inferred


def _ollama_quick(prompt: str, cfg: dict, num_ctx: int = 4096) -> str:
    """Fire a short Ollama request; returns empty string on failure."""
    oc = cfg["ollama"]
    payload = json.dumps({
        "model": oc["model"],
        "prompt": prompt,
        "stream": False,
        "keep_alive": "10m",
        "options": {"num_ctx": num_ctx},
    }).encode()
    req = urllib.request.Request(
        f"{oc['host']}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["response"].strip()
    except Exception:
        return ""


def check_content_relevance(transcript_a: str, transcript_b: str, cfg: dict) -> bool:
    """Ask the LLM whether two transcript excerpts are from the same session."""
    a = " ".join(transcript_a.split()[:200])
    b = " ".join(transcript_b.split()[:200])
    prompt = (
        "/no_think\n"
        "Do the following two audio transcript excerpts come from the same meeting or session? "
        "Consider topic continuity, participants, and conversational flow.\n\n"
        f"EXCERPT A:\n{a}\n\n"
        f"EXCERPT B:\n{b}\n\n"
        "Reply with ONLY 'yes' or 'no'."
    )
    answer = _ollama_quick(prompt, cfg)
    return answer.lower().startswith("y") if answer else True  # assume same on failure


def concat_audio(files: list[Path]) -> Path | None:
    """Use ffmpeg to concatenate audio files into one temp file. Returns path or None on failure."""
    import subprocess
    import tempfile

    list_file = Path(tempfile.mktemp(suffix=".txt"))
    tmp_audio = Path(tempfile.mktemp(suffix=files[0].suffix))
    try:
        list_file.write_text(
            "\n".join(f"file '{f.resolve()}'" for f in files), encoding="utf-8"
        )
        result = subprocess.run(
            ["ffmpeg", "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c", "copy", str(tmp_audio), "-y", "-loglevel", "error"],
            capture_output=True,
        )
        if result.returncode != 0:
            print(f"  ffmpeg error: {result.stderr.decode().strip()}", file=sys.stderr)
            return None
        return tmp_audio
    except FileNotFoundError:
        print("  ffmpeg not found — falling back to per-file transcription.", file=sys.stderr)
        return None
    finally:
        list_file.unlink(missing_ok=True)


def merge_transcripts(pairs: list[tuple[Path, str]]) -> str:
    """Join transcripts with recorder-break markers."""
    if len(pairs) == 1:
        return pairs[0][1]
    total = len(pairs)
    parts = []
    for i, (path, text) in enumerate(pairs, 1):
        ts = parse_filename_timestamp(path.stem)
        ts_str = ts.strftime("%H:%M") if ts else path.name
        parts.append(f"[Recording {i}/{total} — {path.name} ({ts_str})]\n{text}")
    return "\n\n[--- Recording break (recorder limit reached) ---]\n\n".join(parts)


# ─── transcription ────────────────────────────────────────────────────────────

_SMALL_MODELS = {"tiny", "tiny.en", "base", "base.en"}


def _pick_device_index(cfg: dict) -> int:
    """Route tiny/base to device_index; small/medium/large to device_index_large."""
    w = cfg["whisper"]
    model = w.get("model", "base.en")
    idx_small = w.get("device_index", 0)
    idx_large = w.get("device_index_large")
    if idx_large is None or model in _SMALL_MODELS:
        return idx_small
    return idx_large


def load_whisper(cfg: dict):
    """Load a WhisperModel and return it. Reuse across multiple transcribe_file() calls."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(
            "\nERROR: faster-whisper is not installed.\n"
            "Install it with:  pip install faster-whisper\n"
            "Also ensure ffmpeg is on your PATH:  winget install ffmpeg",
            file=sys.stderr,
        )
        sys.exit(1)

    import warnings
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")

    w = cfg["whisper"]
    device_index = _pick_device_index(cfg)
    print(f"  Loading Whisper '{w['model']}' (GPU {device_index})...", end=" ", flush=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = WhisperModel(
            w["model"],
            device=w["device"],
            device_index=device_index,
            compute_type=w["compute_type"],
            download_root=w.get("cache_dir") or None,
        )
    print("ready.")
    return model


def transcribe_file(audio_path: Path, model, cfg: dict) -> tuple[str, float]:
    """Transcribe a single audio file with an already-loaded WhisperModel."""
    w = cfg["whisper"]
    kwargs: dict = {"beam_size": 5, "condition_on_previous_text": False}
    if w.get("language"):
        kwargs["language"] = w["language"]

    print(f"  Transcribing {audio_path.name}...", end=" ", flush=True)
    t0 = time.time()
    segments, info = model.transcribe(str(audio_path), **kwargs)
    transcript = " ".join(seg.text.strip() for seg in segments)
    elapsed = time.time() - t0
    print(f"done. ({info.duration / 60:.1f} min audio, {elapsed:.0f}s, lang: {info.language})")
    return transcript, info.duration


def _transcribe_parallel(
    files: list[Path], cfg: dict, device_indices: list[int]
) -> list[tuple[Path, str]]:
    """Load one Whisper model per GPU; transcribe files concurrently."""
    import warnings
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("ERROR: faster-whisper not installed.", file=sys.stderr)
        sys.exit(1)

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    w = cfg["whisper"]
    n = len(device_indices)

    print(f"  Loading {n} Whisper models (GPU {device_indices})...", end=" ", flush=True)
    models: list[WhisperModel] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for idx in device_indices:
            models.append(WhisperModel(
                w["model"],
                device=w["device"],
                device_index=idx,
                compute_type=w["compute_type"],
                download_root=w.get("cache_dir") or None,
            ))
    print("ready.")

    kwargs: dict = {"beam_size": 5}
    if w.get("language"):
        kwargs["language"] = w["language"]

    def _run(slot: int, path: Path) -> tuple[Path, str]:
        t0 = time.time()
        segments, info = models[slot % n].transcribe(str(path), **kwargs)
        text = " ".join(seg.text.strip() for seg in segments)
        elapsed = time.time() - t0
        print(f"  ✓ {path.name} ({info.duration / 60:.1f} min, {elapsed:.0f}s, GPU {device_indices[slot % n]})")
        return path, text

    ordered: dict[Path, str] = {}
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {pool.submit(_run, i, f): f for i, f in enumerate(files)}
        for fut in as_completed(futures):
            path, text = fut.result()
            ordered[path] = text

    # preserve original file order
    return [(f, ordered[f]) for f in files if f in ordered]


# ─── summarisation ────────────────────────────────────────────────────────────

def summarize(transcript: str, meeting_type: str, date: str, cfg: dict) -> str:
    oc = cfg["ollama"]
    host, model = oc["host"], oc["model"]
    timeout = oc.get("timeout", 600)
    num_ctx = oc.get("num_ctx", 32768)

    prompt = build_prompt(meeting_type, date, transcript)
    est_tokens = len(transcript) // 4
    if est_tokens > num_ctx * 0.75:
        print(
            f"  ⚠ Transcript ~{est_tokens:,} tokens — consider raising num_ctx in config.yaml.",
            file=sys.stderr,
        )

    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "10m",
        "options": {"num_ctx": num_ctx},
    }).encode()

    print(f"  Summarising with {model}...", end=" ", flush=True)
    t0 = time.time()

    req = urllib.request.Request(
        f"{host}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
        print(f"done. ({time.time() - t0:.0f}s)")
        return result["response"].strip()
    except urllib.error.URLError as e:
        print(f"\nERROR: Cannot reach Ollama at {host}.\n  Is Ollama running?  {e}", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"\nERROR during summarisation: {e}", file=sys.stderr)
        return ""


# ─── output assembly ──────────────────────────────────────────────────────────

def assemble_output(summary: str, transcript: str, cfg: dict) -> str:
    parts = [summary]
    if cfg["output"]["include_transcript"]:
        if cfg["output"]["transcript_collapsible"]:
            parts.append(
                "\n\n---\n\n"
                "<details>\n<summary>📜 Full Transcript</summary>\n\n"
                f"{transcript}\n\n"
                "</details>"
            )
        else:
            parts.append(f"\n\n---\n\n## 📜 Full Transcript\n\n{transcript}")
    return "".join(parts)


def derive_output_path(summary: str, anchor: Path, date_str: str, output_dir: Path) -> Path:
    m = re.search(r"^#\s+(.{6,})", summary, re.MULTILINE)
    if m:
        title = m.group(1).strip()
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)[:100].rstrip(". ")
        base = f"{safe}.md"
    else:
        stem = anchor.stem
        for _, p in TYPE_PATTERNS.items():
            stem = p.sub("", stem).strip("_- ")
        base = f"{date_str} {stem}.md"

    path = output_dir / base
    if path.exists():
        for i in range(1, 100):
            candidate = output_dir / f"{base[:-3]} ({i}).md"
            if not candidate.exists():
                return candidate
    return path


# ─── processing pipelines ─────────────────────────────────────────────────────

def process_file(
    audio_path: Path,
    output_dir: Path,
    cfg: dict,
    meeting_type: str | None = None,
    transcript_only: bool = False,
    model=None,
) -> Path | None:
    print(f"\n{'─' * 60}")
    print(f"▶ {audio_path.name}")

    if not audio_path.exists():
        print("  ERROR: file not found", file=sys.stderr)
        return None

    date_str = (parse_filename_timestamp(audio_path.stem) or datetime.now()).strftime("%Y-%m-%d")

    transcript, _ = transcribe_file(audio_path, model or load_whisper(cfg), cfg)
    if not transcript.strip():
        print("  WARNING: empty transcript — check the audio file.", file=sys.stderr)
        return None

    mtype = resolve_meeting_type(audio_path, transcript, meeting_type, cfg)
    print(f"  Type: {mtype}")

    if transcript_only:
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / f"{date_str} {audio_path.stem}.transcript.txt"
        out.write_text(transcript, encoding="utf-8")
        print(f"  Transcript saved: {out}")
        return out

    summary = summarize(transcript, mtype, date_str, cfg)
    if not summary:
        output_dir.mkdir(parents=True, exist_ok=True)
        fallback = output_dir / f"{date_str} {audio_path.stem}.transcript.txt"
        fallback.write_text(transcript, encoding="utf-8")
        print(f"  Summarisation failed — transcript saved to {fallback}", file=sys.stderr)
        return fallback

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = derive_output_path(summary, audio_path, date_str, output_dir)
    out_path.write_text(assemble_output(summary, transcript, cfg), encoding="utf-8")
    print(f"  ✓ {out_path}")
    return out_path


def process_group(
    files: list[Path],
    output_dir: Path,
    cfg: dict,
    meeting_type: str | None = None,
    transcript_only: bool = False,
    llm_check: bool = False,
) -> Path | None:
    """Transcribe a group of related files, (optionally) verify, merge, then summarise."""
    if len(files) == 1:
        return process_file(files[0], output_dir, cfg, meeting_type, transcript_only)

    print(f"\n{'─' * 60}")
    print(f"▶ Merging {len(files)} files into one session:")
    for f in files:
        ts = parse_filename_timestamp(f.stem)
        ts_str = f"  {ts.strftime('%H:%M')}" if ts else ""
        print(f"    {f.name}{ts_str}")

    # Load model once, transcribe each file in turn
    model = load_whisper(cfg)
    raw_pairs: list[tuple[Path, str]] = []
    for f in files:
        print(f"\n  [{len(raw_pairs)+1}/{len(files)}] {f.name}")
        transcript, _ = transcribe_file(f, model, cfg)
        raw_pairs.append((f, transcript))

    pairs: list[tuple[Path, str]] = []
    for f, transcript in raw_pairs:
        if transcript.strip():
            pairs.append((f, transcript))
        else:
            print(f"  WARNING: empty transcript for {f.name}", file=sys.stderr)

    if not pairs:
        print("  ERROR: all transcripts empty.", file=sys.stderr)
        return None

    # Optional LLM relevance check on adjacent pairs — only meaningful for fallback path
    if llm_check and len(pairs) > 1:
        print("\n  Checking content relevance between segments...", flush=True)
        split_before: list[int] = []
        for i in range(len(pairs) - 1):
            _, ta = pairs[i]
            _, tb = pairs[i + 1]
            same = check_content_relevance(ta, tb, cfg)
            status = "✓ same" if same else "✗ different session"
            print(f"    segments {i+1}↔{i+2}: {status}")
            if not same:
                split_before.append(i + 1)

        if split_before:
            # Re-split into sub-groups and recurse
            boundaries = [0] + split_before + [len(pairs)]
            results = []
            for a, b in zip(boundaries, boundaries[1:]):
                subfiles = [p for p, _ in pairs[a:b]]
                results.append(
                    process_group(subfiles, output_dir, cfg, meeting_type, transcript_only, False)
                )
            return results[-1]

    merged = merge_transcripts(pairs)
    date_str = datetime.now().strftime("%Y-%m-%d")
    mtype = resolve_meeting_type(files[0], merged, meeting_type, cfg)

    # Use timestamp of first file as the session date if parseable
    first_ts = parse_filename_timestamp(files[0].stem)
    if first_ts:
        date_str = first_ts.strftime("%Y-%m-%d")

    print(f"\n  Type: {mtype} | Session date: {date_str}")

    if transcript_only:
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / f"{date_str} {files[0].stem} (merged).transcript.txt"
        out.write_text(merged, encoding="utf-8")
        print(f"  Transcript saved: {out}")
        return out

    summary = summarize(merged, mtype, date_str, cfg)
    if not summary:
        output_dir.mkdir(parents=True, exist_ok=True)
        fallback = output_dir / f"{date_str} {files[0].stem} (merged).transcript.txt"
        fallback.write_text(merged, encoding="utf-8")
        print(f"  Summarisation failed — transcript saved to {fallback}", file=sys.stderr)
        return fallback

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = derive_output_path(summary, files[0], date_str, output_dir)
    out_path.write_text(assemble_output(summary, merged, cfg), encoding="utf-8")
    print(f"  ✓ {out_path}")
    return out_path


# ─── summarize existing transcripts ──────────────────────────────────────────

def process_transcript_file(
    transcript_path: Path,
    output_dir: Path,
    cfg: dict,
    meeting_type: str | None = None,
) -> Path | None:
    """Re-summarize a .transcript.txt file that was previously saved."""
    print(f"\n{'─' * 60}")
    print(f"▶ {transcript_path.name}")

    transcript = transcript_path.read_text(encoding="utf-8").strip()
    if not transcript:
        print("  WARNING: empty transcript file.", file=sys.stderr)
        return None

    # Derive a plausible anchor path and date from the filename
    # Expected pattern: "YYYY-MM-DD <stem>.transcript.txt"
    m = re.match(r"(\d{4}-\d{2}-\d{2})\s+(.+?)(?:\s*\(merged\))?\.transcript\.txt$",
                 transcript_path.name, re.I)
    if m:
        date_str, stem = m.group(1), m.group(2)
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")
        stem = transcript_path.stem

    anchor = transcript_path.with_name(stem)

    mtype = meeting_type or detect_meeting_type(anchor)
    if mtype == "general":
        print("  Inferring type from transcript...", end=" ", flush=True)
        mtype = infer_meeting_type(transcript, cfg)
        print(mtype)
    print(f"  Type: {mtype}")

    summary = summarize(transcript, mtype, date_str, cfg)
    if not summary:
        print("  Summarisation failed — transcript file unchanged.", file=sys.stderr)
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = derive_output_path(summary, anchor, date_str, output_dir)
    out_path.write_text(assemble_output(summary, transcript, cfg), encoding="utf-8")
    transcript_path.unlink()  # remove the .transcript.txt now that we have the .md
    print(f"  ✓ {out_path}")
    return out_path


# ─── watch mode ───────────────────────────────────────────────────────────────

def watch_mode(
    input_dir: Path,
    output_dir: Path,
    cfg: dict,
    meeting_type: str | None,
    transcript_only: bool,
) -> None:
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("ERROR: watchdog not installed.  pip install watchdog", file=sys.stderr)
        sys.exit(1)

    in_flight: set[Path] = set()

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory:
                return
            path = Path(event.src_path).resolve()
            if path.suffix.lower() not in AUDIO_EXTS or path in in_flight:
                return
            in_flight.add(path)
            time.sleep(1.0)
            try:
                process_file(path, output_dir, cfg, meeting_type, transcript_only)
            except Exception as exc:
                print(f"  ERROR: {exc}", file=sys.stderr)
            finally:
                in_flight.discard(path)

    input_dir.mkdir(parents=True, exist_ok=True)
    print(f"Watching {input_dir}/ for audio files... (Ctrl+C to stop)")
    observer = Observer()
    observer.schedule(_Handler(), str(input_dir), recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="transmizer",
        description="Transcribe and summarize audio recordings locally",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=dedent("""\
            Examples:
              python transmizer.py                         # process all files in input/
              python transmizer.py meeting.mp3             # process a specific file
              python transmizer.py --type medical appt.m4a
              python transmizer.py --watch                 # watch input/ for new files
              python transmizer.py --transcript-only       # transcribe without summarising
              python transmizer.py --no-merge              # process each file separately
              python transmizer.py --merge-gap 30         # tighten grouping to 30 min
              python transmizer.py --merge-llm-check      # verify with LLM before merging

            Meeting type is auto-detected from the filename:
              1on1_*, oneone_*    → 1on1
              doctor_*, medical_* → medical
              meeting_*, sync_*   → work
              (anything else)     → general

            Add your own type by dropping a .md file in prompts/.
            Use {date} and {transcript} as placeholders.
        """),
    )
    parser.add_argument("files", nargs="*", type=Path, help="Audio file(s) to process")
    parser.add_argument(
        "--type", "-t",
        help="Override meeting type (must match a prompts/*.md stem)",
    )
    parser.add_argument("--watch", "-w", action="store_true", help="Watch input/ for new files")
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("output"),
        help="Output directory (default: ./output/)",
    )
    parser.add_argument(
        "--whisper-model",
        metavar="MODEL",
        help="Whisper model: tiny.en / base.en / small.en / medium.en / large-v3",
    )
    parser.add_argument(
        "--ollama-model",
        metavar="MODEL",
        help="Ollama model (e.g. qwen3-coder:30b, llava-llama3)",
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=Path("config.yaml"),
        help="Config file (default: ./config.yaml)",
    )
    parser.add_argument(
        "--transcript-only",
        action="store_true",
        help="Only transcribe; skip summarisation",
    )
    parser.add_argument(
        "--no-transcript",
        action="store_true",
        help="Exclude transcript from output markdown",
    )
    # merge flags
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Process each file independently instead of grouping by session",
    )
    parser.add_argument(
        "--merge-gap",
        type=float,
        metavar="MINS",
        help="Max gap in minutes between files in the same group (default from config, 60)",
    )
    parser.add_argument(
        "--merge-llm-check",
        action="store_true",
        help="Use LLM to verify content relevance before merging adjacent files",
    )
    parser.add_argument(
        "--summarize-transcripts",
        action="store_true",
        help="Re-summarize *.transcript.txt files in the output dir (no re-transcription)",
    )
    parser.add_argument(
        "--skip-processed",
        action="store_true",
        help="Skip input files whose stem already has output in the output dir",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.whisper_model:
        cfg["whisper"]["model"] = args.whisper_model
    if args.ollama_model:
        cfg["ollama"]["model"] = args.ollama_model
    if args.no_transcript:
        cfg["output"]["include_transcript"] = False
    if args.merge_gap is not None:
        cfg["merge"]["gap_minutes"] = args.merge_gap
    if args.merge_llm_check:
        cfg["merge"]["llm_check"] = True
    do_merge = not args.no_merge

    if args.watch:
        watch_mode(Path("input"), args.output, cfg, args.type, args.transcript_only)
        return

    # Re-summarize existing transcripts without re-transcribing
    if args.summarize_transcripts:
        transcript_files = sorted(args.output.glob("*.transcript.txt"))
        if not transcript_files:
            print(f"No *.transcript.txt files found in {args.output}/")
            sys.exit(0)
        print(f"Found {len(transcript_files)} transcript file(s) to summarise.")
        errors: list[tuple] = []
        for tf in transcript_files:
            try:
                process_transcript_file(tf, args.output, cfg, args.type)
            except Exception as exc:
                print(f"  ERROR on {tf.name}: {exc}", file=sys.stderr)
                errors.append((tf, exc))
        print(f"\n{'─' * 60}")
        print(f"Done. Output → {args.output}/")
        if errors:
            sys.exit(1)
        return

    # Collect files
    if args.files:
        audio_files = [f for f in args.files if f.suffix.lower() in AUDIO_EXTS]
        for f in args.files:
            if f.suffix.lower() not in AUDIO_EXTS:
                print(f"Skipping {f} — unsupported extension", file=sys.stderr)
    else:
        input_dir = Path("input")
        if not input_dir.exists():
            input_dir.mkdir()
            print("Created input/ — drop audio files there and re-run.")
            sys.exit(0)
        audio_files = sorted(
            f for f in input_dir.iterdir() if f.suffix.lower() in AUDIO_EXTS
        )

    audio_files = dedup_by_stem(audio_files)

    # Skip files whose stem already appears in any output filename
    if args.skip_processed and args.output.exists():
        existing = {f.name for f in args.output.iterdir()}
        def _already_processed(f: Path) -> bool:
            return any(f.stem in name for name in existing)
        before = len(audio_files)
        audio_files = [f for f in audio_files if not _already_processed(f)]
        skipped = before - len(audio_files)
        if skipped:
            print(f"Skipping {skipped} already-processed file(s).")

    if not audio_files:
        print("No audio files found.")
        sys.exit(0)

    errors: list[tuple] = []
    gap = cfg["merge"]["gap_minutes"]
    llm_check = cfg["merge"]["llm_check"]

    if do_merge and len(audio_files) > 1:
        groups = group_files(audio_files, gap)
        print(
            f"Found {len(audio_files)} file(s) → {len(groups)} session group(s)"
            f" (gap ≤ {gap:.0f} min). Use --no-merge to process files individually."
        )
        for i, group in enumerate(groups, 1):
            ts = parse_filename_timestamp(group[0].stem)
            label = ts.strftime("%Y-%m-%d %H:%M") if ts else group[0].name
            print(f"\nGroup {i}/{len(groups)}: {len(group)} file(s) starting {label}")
            try:
                process_group(group, args.output, cfg, args.type, args.transcript_only, llm_check)
            except Exception as exc:
                print(f"  ERROR: {exc}", file=sys.stderr)
                errors.append((group, exc))
    else:
        print(f"Processing {len(audio_files)} file(s) individually...")
        for af in audio_files:
            try:
                process_file(af, args.output, cfg, args.type, args.transcript_only)
            except Exception as exc:
                print(f"  ERROR on {af.name}: {exc}", file=sys.stderr)
                errors.append((af, exc))

    print(f"\n{'─' * 60}")
    print(f"Done. Output → {args.output}/")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
