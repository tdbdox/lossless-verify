#!/usr/bin/env python3
"""
lossless-verify — fake-lossless detector + quality sorter.

For every audio file under a directory (or a single file):
  1. Spectral check: decode to mono PCM via ffmpeg, FFT loud frames only,
     find the high-frequency cutoff relative to the 11-14 kHz reference band.
     Verdicts are driven by CLIFF STEEPNESS -- absolute and relative to the
     local slope -- not cutoff location: lossy codecs leave a near-vertical
     lowpass wall, while genuine masters (flat modern, vintage tape slope,
     dark mixes) roll off gradually. A low cutoff with no cliff is content,
     not a transcode fingerprint.
       FAIL    any 1 kHz step drops > 40 dB (unambiguous brickwall)
       SUSPECT a step drops > 30 dB, or a step > 3x the local median slope
               with > 12 dB absolute below 24 kHz, or cutoff <= 18 kHz
       PASS    otherwise
       UNKNOWN file could not be decoded / had no audible frames
  2. Sort every file into an output folder by verdict:
       <output>/PASS/<relative path>
       <output>/SUSPECT/<tier>/<relative path>
       <output>/FAIL/<tier>/<relative path>
     Tiers sub-sort SUSPECT / FAIL by likely usability, driven by cutoff:
       NEAR_CD     >= 19 kHz: near-CD quality, worth auditioning
       BORDERLINE  >= 16 kHz: YouTube-grade or better, borderline
       LOW_QUALITY < 16 kHz: very low ceiling
     Each sorted file gets a .verdict.json sidecar with the analysis.
     By default files are COPIED (non-destructive). Pass --move to move
     them instead of copying.
  3. Optionally tag the comment field with an idempotent stamp
     (off by default -- pass --tag to enable):
       Lossless-Verify: PASS | cutoff 22.0kHz | 2026-10-01
     Re-running replaces the old stamp instead of stacking duplicates.
     FLAC uses Vorbis COMMENT, MP3 uses ID3 COMM, M4A uses (c)cmt,
     other formats use a generic comment where supported.
     In copy mode the sorted copy is tagged (source untouched); in
     --move mode the file is tagged after moving.

Usage:
  python3 lossless_verify.py <path> [--output-dir ./lossless-verify-output]
  python3 lossless_verify.py ./incoming --move --tag --json

Supported extensions (default): .flac .wav .aiff .aif .mp3 .m4a .ogg .opus
Requires: Python 3.9+, ffmpeg + ffprobe on PATH, numpy, mutagen.
See requirements.txt and README.md.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

import numpy as np

# ---------------- analysis constants (calibrated, do not tune blindly) ----------------
N_FFT = 4096
HOP = 2048
REF_BANDS = (11, 12, 13)    # kHz reference bands
CUTOFF_MIN_KHZ = 15
FAIL_DROP_DB = 40
SUSPECT_DROP_DB = 30
CUTOFF_DROP_DB = 25
CLIFF_RATIO = 3.0           # steepest step vs. median step
CLIFF_MIN_DB = 12.0         # absolute floor for the relative-slope rule
CLIFF_MAX_KHZ = 24          # relative-slope rule only applies below 24 kHz
SUSPECT_CUTOFF_HZ = 18000

# Salvage tiering cutoffs (Hz) — tiers are driven by cutoff only.
# A FAIL file with a 20 kHz brickwall is more salvageable than a FAIL file
# with a 14 kHz brickwall, so drop size does NOT force a tier.
TIER_NEAR_CD_CUTOFF = 19000
TIER_BORDERLINE_CUTOFF = 16000
# Below BORDERLINE -> LOW_QUALITY

TAG_PREFIX = "Lossless-Verify"
DEFAULT_EXTS = {".flac", ".wav", ".aiff", ".aif", ".mp3", ".m4a", ".ogg", ".opus"}
# -------------------------------------------------------------------------------------


def log(msg, log_file=None, quiet=False, sink=None):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    if sink is not None:
        sink.append(line)
        return
    if quiet:
        return
    print(line, flush=True)
    if log_file:
        with open(log_file, "a") as f:
            f.write(line + "\n")


def load_allowlist(path):
    if not path or not os.path.exists(path):
        return []
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def allowlisted(path, frags):
    return any(frag in path for frag in frags)


def find_tool(name, explicit=None):
    if explicit:
        return explicit
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(
            "%s not found on PATH. Install ffmpeg (which provides %s and %s)."
            % (name, "ffmpeg", "ffprobe")
        )
    return found


def decode_mono(path, ffmpeg, ffprobe):
    """Decode any ffmpeg-readable audio file to mono float32 PCM."""
    probe = subprocess.run(
        [ffprobe, "-v", "error",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    m = re.search(r"\d+", probe.stdout.strip().split("\n")[0] if probe.stdout.strip() else "")
    if not m:
        raise ValueError("could not read sample rate for %s" % path)
    sr = int(m.group(0))
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-i", path, "-ac", "1",
         "-f", "f32le", "-acodec", "pcm_f32le", "-"],
        capture_output=True)
    pcm = np.frombuffer(proc.stdout, dtype=np.float32)
    return pcm, sr


def analyze(path, ffmpeg, ffprobe):
    pcm, sr = decode_mono(path, ffmpeg, ffprobe)
    nyquist = sr / 2.0
    win = np.hanning(N_FFT)

    n_frames = 1 + (len(pcm) - N_FFT) // HOP
    if n_frames <= 0:
        return {"verdict": "UNKNOWN", "cutoff_hz": 0, "max_drop_db": 0, "sr": sr}
    frame_e = np.empty(n_frames)
    for i in range(n_frames):
        fr = pcm[i * HOP:i * HOP + N_FFT] * win
        frame_e[i] = np.sum(fr ** 2)
    if frame_e.max() <= 0:
        return {"verdict": "UNKNOWN", "cutoff_hz": 0, "max_drop_db": 0, "sr": sr}
    thresh = np.percentile(frame_e, 70)
    loud_idx = np.where(frame_e >= thresh)[0]
    if len(loud_idx) == 0:
        loud_idx = np.array([np.argmax(frame_e)])

    nbands = int(nyquist // 1000)
    # Low sample rates (e.g. voice memos at <= 22.05 kHz) have no content
    # above ~11 kHz, so the 11-14 kHz reference band is empty and the
    # cliff cannot be measured. Return UNKNOWN instead of NaN.
    if nbands <= CUTOFF_MIN_KHZ:
        return {"verdict": "UNKNOWN", "cutoff_hz": 0, "max_drop_db": 0,
                "sr": sr,
                "reason": "sample rate too low to measure high-frequency cliff"}
    band_e = np.zeros(nbands)
    # Hoist frequency bins + band masks outside the frame loop (they are
    # identical for every frame; recomputing them per frame was ~20x work).
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    band_masks = [(freqs >= b * 1000) & (freqs < (b + 1) * 1000)
                  for b in range(nbands)]
    for i in loud_idx:
        fr = pcm[i * HOP:i * HOP + N_FFT] * win
        spec = np.abs(np.fft.rfft(fr)) ** 2
        for b in range(nbands):
            band_e[b] += spec[band_masks[b]].sum()
    band_e /= len(loud_idx)
    band_db = 10 * np.log10(band_e + 1e-12)

    ref = np.mean([band_db[b] for b in REF_BANDS if b < nbands])
    cutoff_khz = None
    for b in range(CUTOFF_MIN_KHZ, nbands):
        if ref - band_db[b] > CUTOFF_DROP_DB:
            cutoff_khz = b
            break
    if cutoff_khz is None:
        cutoff_khz = nbands  # extends to Nyquist

    drops = [band_db[b] - band_db[b + 1]
             for b in range(CUTOFF_MIN_KHZ, nbands - 1)]
    max_drop = max(drops) if drops else 0.0
    baseline = float(np.median(drops)) if drops else 0.0
    cliff_ratio = max_drop / max(baseline, 1.0)
    max_drop_khz = (CUTOFF_MIN_KHZ + drops.index(max_drop)) if drops else 0

    cutoff_hz = cutoff_khz * 1000.0
    if max_drop > FAIL_DROP_DB:
        verdict = "FAIL"
    elif (max_drop > SUSPECT_DROP_DB or cutoff_hz <= SUSPECT_CUTOFF_HZ
          or (cliff_ratio > CLIFF_RATIO and max_drop > CLIFF_MIN_DB
              and max_drop_khz < CLIFF_MAX_KHZ)):
        verdict = "SUSPECT"
    else:
        verdict = "PASS"
    # band_db is included so plots can be made later without re-analyzing
    # (see scripts/plot_spectrum.py). Rounded to keep .verdict.json small.
    return {"verdict": verdict, "cutoff_hz": cutoff_hz,
            "max_drop_db": float(max_drop), "sr": sr,
            "cliff_ratio": round(cliff_ratio, 2),
            "band_db": [round(float(x), 1) for x in band_db]}


STAMP_RE = re.compile(
    # Matches current "Lossless-Verify:" stamps and legacy "FLAC-Verify:"
    # stamps from before the rename, so re-tagging replaces old stamps
    # instead of stacking duplicates.
    r"(?:Lossless-Verify|FLAC-Verify):\s*[A-Z]+\s*\|\s*cutoff\s*[\d.]+kHz\s*\|\s*\d{4}-\d{2}-\d{2}")


def tag_file(path, verdict, cutoff_hz):
    """Stamp the file's comment field. Idempotent. Returns the stamp."""
    stamp = "%s: %s | cutoff %.1fkHz | %s" % (
        TAG_PREFIX, verdict, cutoff_hz / 1000.0,
        datetime.now().strftime("%Y-%m-%d"))
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        from mutagen.flac import FLAC
        audio = FLAC(path)
        existing = " ".join(audio.get("comment", []))
        cleaned = re.sub(r"\s+", " ", STAMP_RE.sub("", existing)).strip()
        cleaned = re.sub(r"\s*\|\s*", " | ", cleaned).strip(" |")
        new_comment = (cleaned + " | " + stamp) if cleaned else stamp
        audio["comment"] = [new_comment]
        audio.save()
    elif ext == ".mp3":
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, COMM
        audio = MP3(path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        existing = " ".join(c.text[0] for c in audio.tags.getall("COMM") if c.text)
        cleaned = STAMP_RE.sub("", existing)
        new = (cleaned + " | " + stamp) if cleaned.strip(" |") else stamp
        audio.tags.delall("COMM")
        audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=new))
        audio.save()
    elif ext == ".m4a":
        from mutagen.mp4 import MP4
        audio = MP4(path)
        existing = " ".join(audio.get("\xa9cmt", []))
        cleaned = STAMP_RE.sub("", existing)
        new = (cleaned + " | " + stamp) if cleaned.strip(" |") else stamp
        audio["\xa9cmt"] = [new]
        audio.save()
    else:  # wav / aiff / ogg / opus: generic comment where supported
        import mutagen
        audio = mutagen.File(path)
        if audio is not None and hasattr(audio, "tags") and audio.tags is not None:
            existing = " ".join(audio.get("comment", []))
            cleaned = STAMP_RE.sub("", existing)
            new = (cleaned + " | " + stamp) if cleaned.strip(" |") else stamp
            audio["comment"] = new
            audio.save()
    return stamp


def salvage_tier(res):
    """Sub-sort SUSPECT / FAIL by likely usability, driven by cutoff only.

    NEAR_CD     >= 19 kHz cutoff: near-CD quality, worth auditioning.
    BORDERLINE  >= 16 kHz cutoff: YouTube-grade or better, borderline.
    LOW_QUALITY < 16 kHz cutoff: very low ceiling.
    """
    cutoff = res.get("cutoff_hz", 0)
    if cutoff >= TIER_NEAR_CD_CUTOFF:
        return "NEAR_CD"
    if cutoff >= TIER_BORDERLINE_CUTOFF:
        return "BORDERLINE"
    return "LOW_QUALITY"


def sort_file(src, rel, res, output_dir, move, log_file, quiet, sink=None):
    verdict = res["verdict"]
    if verdict in ("SUSPECT", "FAIL"):
        tier = salvage_tier(res)
        res["tier"] = tier
        dest = os.path.join(output_dir, verdict, tier, rel)
    elif verdict == "PASS":
        dest = os.path.join(output_dir, "PASS", rel)
    else:  # UNKNOWN
        dest = os.path.join(output_dir, "UNKNOWN", rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    base, ext = os.path.splitext(dest)
    i = 1
    while os.path.exists(dest):  # never clobber
        dest = "%s-%d%s" % (base, i, ext)
        i += 1
    if move:
        shutil.move(src, dest)
        action = "moved"
    else:
        shutil.copy2(src, dest)
        action = "copied"
    with open(dest + ".verdict.json", "w") as f:
        json.dump({"original": src, "action": action,
                   "sorted": datetime.now().isoformat(), **res}, f, indent=2)
    log("%s verdict=%s from=%s to=%s" % (action, verdict, src, dest),
        log_file, quiet, sink=sink)
    return dest


def handle_file(path, scan_root, args, ffmpeg, ffprobe, allow_frags, sink=None):
    rel = os.path.relpath(path, scan_root)
    if allowlisted(path, allow_frags):
        log("skipped allowlisted rel=%s" % rel, args.log_file, args.quiet, sink=sink)
        return "SKIP"
    try:
        res = analyze(path, ffmpeg, ffprobe)
    except Exception as e:
        log("analyze_error rel=%s err=%r" % (rel, e), args.log_file, args.quiet, sink=sink)
        return "ERROR"
    v = res["verdict"]
    detail = ("verdict=%s cutoff=%.0fHz maxdrop=%.1fdB sr=%d rel=%s"
              % (v, res["cutoff_hz"], res["max_drop_db"], res["sr"], rel))
    # --no-sort-suspect: SUSPECT files stay in place, untagged unless
    # tagging was requested (then the source is tagged in place).
    should_sort = v in ("PASS", "SUSPECT", "FAIL", "UNKNOWN") and not (
        v == "SUSPECT" and not args.sort_suspect)
    dest = None
    if should_sort:
        dest = sort_file(path, rel, res, args.output_dir, args.move,
                         args.log_file, args.quiet, sink=sink)
    if args.tag and v in ("PASS", "SUSPECT", "FAIL"):
        # Tag the sorted copy in copy mode (source untouched); tag the
        # moved file after moving in --move mode; tag the source in place
        # when the file was not sorted (--no-sort-suspect).
        target = dest if dest is not None else path
        try:
            stamp = tag_file(target, v, res["cutoff_hz"])
            log("tagged rel=%s target=%s comment=%r" % (rel, target, stamp),
                args.log_file, args.quiet, sink=sink)
        except Exception as e:
            log("tag_error rel=%s err=%r" % (rel, e),
                args.log_file, args.quiet, sink=sink)
    log(detail, args.log_file, args.quiet, sink=sink)
    return v


def _process_one(task):
    """Worker for --workers > 1. Returns (verdict, log_lines)."""
    path, scan_root, args, ffmpeg, ffprobe, allow_frags = task
    sink = []
    try:
        v = handle_file(path, scan_root, args, ffmpeg, ffprobe, allow_frags,
                        sink=sink)
    except Exception as e:  # never let one file kill a parallel scan
        sink.append("%s worker_error path=%s err=%r"
                    % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path, e))
        v = "ERROR"
    return v, sink


def collect_files(input_path, exts, output_dir=None):
    if os.path.isfile(input_path):
        return [input_path], os.path.dirname(os.path.abspath(input_path))
    scan_root = os.path.abspath(input_path)
    out_abs = os.path.abspath(output_dir) if output_dir else None
    files = []
    for root, dirs, fns in os.walk(input_path):
        # Never scan our own output: if output_dir lives inside the input
        # (e.g. input "." with default ./lossless-verify-output), a second run
        # would otherwise re-scan sorted copies into nested folders.
        if out_abs:
            root_abs = os.path.abspath(root)
            if root_abs == out_abs or root_abs.startswith(out_abs + os.sep):
                dirs[:] = []
                continue
            # Prune the output dir so walk does not descend into it.
            dirs[:] = [d for d in dirs
                       if os.path.abspath(os.path.join(root, d)) != out_abs]
        for fn in sorted(fns):
            if os.path.splitext(fn)[1].lower() in exts:
                files.append(os.path.join(root, fn))
    return files, scan_root


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Detect fake-lossless / lossy-sourced audio and sort "
                    "files by spectral verification result.")
    p.add_argument("path", help="audio file or directory to scan")
    p.add_argument("--output-dir", default="./lossless-verify-output",
                   help="where sorted files go (default: ./lossless-verify-output)")
    p.add_argument("--move", action="store_true",
                   help="move files into output folders (default: copy)")
    p.add_argument("--tag", action="store_true",
                   help="stamp the comment field with the verdict (default: off)")
    p.add_argument("--sort-suspect", dest="sort_suspect", action="store_true",
                   default=True,
                   help="sort SUSPECT files too (default: on)")
    p.add_argument("--no-sort-suspect", dest="sort_suspect",
                   action="store_false",
                   help="leave SUSPECT files in place (only sort PASS/FAIL)")
    p.add_argument("--extensions", default=",".join(sorted(DEFAULT_EXTS)),
                   help="comma-separated extensions to scan "
                        "(default: %s)" % ",".join(sorted(DEFAULT_EXTS)))
    p.add_argument("--allowlist", default=None,
                   help="text file of substrings; matching paths are skipped")
    p.add_argument("--ffmpeg", default=None,
                   help="path to ffmpeg (default: ffmpeg on PATH)")
    p.add_argument("--ffprobe", default=None,
                   help="path to ffprobe (default: ffprobe next to --ffmpeg, "
                        "else ffprobe on PATH)")
    p.add_argument("--workers", type=int, default=1,
                   help="parallel workers for multi-file scans "
                        "(default: 1, sequential)")
    p.add_argument("--log-file", default=None,
                   help="also append log lines to this file")
    p.add_argument("--json", action="store_true",
                   help="print the summary counts as JSON")
    p.add_argument("--quiet", action="store_true",
                   help="suppress per-file log lines")
    args = p.parse_args(argv)

    if not os.path.exists(args.path):
        print("input not found: %s" % args.path, file=sys.stderr)
        return 2
    ffmpeg = find_tool("ffmpeg", args.ffmpeg)
    if args.ffprobe:
        ffprobe = find_tool("ffprobe", args.ffprobe)
    elif args.ffmpeg:
        # ffprobe next to --ffmpeg if it exists there, else ffprobe on PATH.
        # (find_tool returns an explicit path unchecked, so check existence
        # here — a bare "--ffmpeg ffmpeg" also needs abspath, since
        # dirname("ffmpeg") is "" and would bypass shutil.which.)
        candidate = os.path.join(
            os.path.dirname(os.path.abspath(args.ffmpeg)), "ffprobe")
        ffprobe = (candidate if os.path.exists(candidate)
                   else find_tool("ffprobe", None))
    else:
        ffprobe = find_tool("ffprobe", None)
    exts = {e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in args.extensions.split(",") if e.strip()}
    allow_frags = load_allowlist(args.allowlist)
    os.makedirs(args.output_dir, exist_ok=True)

    files, scan_root = collect_files(args.path, exts, args.output_dir)
    log("scan start path=%s files=%d output=%s mode=%s workers=%d"
        % (args.path, len(files), args.output_dir,
           "move" if args.move else "copy", args.workers),
        args.log_file, args.quiet)
    counts = {}
    if args.workers and args.workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        tasks = [(f, scan_root, args, ffmpeg, ffprobe, allow_frags)
                 for f in files]
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            for v, lines in ex.map(_process_one, tasks):
                # Logs are collected in workers and replayed here so
                # --log-file writes stay sequential and lines stay whole.
                for line in lines:
                    if not args.quiet:
                        print(line, flush=True)
                    if args.log_file:
                        with open(args.log_file, "a") as fh:
                            fh.write(line + "\n")
                counts[v] = counts.get(v, 0) + 1
    else:
        for f in files:
            v = handle_file(f, scan_root, args, ffmpeg, ffprobe, allow_frags)
            counts[v] = counts.get(v, 0) + 1
    summary = {"input": args.path, "output": args.output_dir,
               "mode": "move" if args.move else "copy",
               "counts": counts}
    with open(os.path.join(args.output_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    log("scan done counts=%s" % json.dumps(counts),
        args.log_file, args.quiet)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("counts=%s" % counts)
    return 0


def cli():
    """Console-script entry point (see pyproject.toml)."""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
