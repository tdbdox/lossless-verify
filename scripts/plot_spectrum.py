#!/usr/bin/env python3
"""
plot_spectrum.py — render a mean-spectrum plot from lossless-verify analysis.

The plot shows per-kHz band energy (dB, relative to the 11-14 kHz reference
band) vs. frequency, with the measured cutoff and the steepest 1 kHz step
marked. A genuine lossless master rolls off gradually; a lossy transcode
shows a near-vertical cliff (the "brickwall").

Usage:
  # From an audio file (requires ffmpeg + lossless_verify.py):
  python3 scripts/plot_spectrum.py track.flac --output spectrum.png

  # From a sanitized spectrum JSON (auto-detected by .json extension):
  python3 scripts/plot_spectrum.py spectrum.json --output spectrum.png

  # Side-by-side comparison for README (two JSONs or two audio files):
  python3 scripts/plot_spectrum.py --compare genuine.json transcode.json \
      --output assets/spectrum-compare.png \
      --titles "Genuine lossless — PASS" "Transcoded — SUSPECT"

  JSON inputs are auto-detected by the .json extension, so --from-json is only
  needed for JSON files with a different extension. Because --compare takes its
  own two files, --titles can appear before or after the inputs.

The JSON format is the same shape as lossless_verify.py's .verdict.json sidecars
(verdict, cutoff_hz, max_drop_db, sr, cliff_ratio, band_db), minus the
"original" path. Strip paths/filenames before sharing JSONs.

Requires: matplotlib (optional dependency — not in requirements.txt).
  pip install matplotlib
"""

import argparse
import json
import os
import sys

import numpy as np


def spectrum_from_audio(path, ffmpeg=None, ffprobe=None):
    """Run lossless_verify.analyze() and return a sanitized spectrum dict."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import lossless_verify
    ffmpeg = ffmpeg or lossless_verify.find_tool("ffmpeg", None)
    ffprobe = ffprobe or lossless_verify.find_tool("ffprobe", None)
    res = lossless_verify.analyze(path, ffmpeg, ffprobe)
    if "band_db" not in res:
        raise ValueError("no band data for %s (verdict %s)" % (path, res.get("verdict")))
    return {k: res[k] for k in ("verdict", "cutoff_hz", "max_drop_db", "sr",
                                "cliff_ratio", "band_db") if k in res}


def spectrum_from_json(path):
    with open(path) as f:
        data = json.load(f)
    for key in ("band_db", "cutoff_hz", "max_drop_db", "sr"):
        if key not in data:
            raise ValueError("%s is missing %r" % (path, key))
    return data


def plot_one(ax, spec, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: F401  (backend set above)

    band_db = np.array(spec["band_db"], dtype=float)
    nbands = len(band_db)
    sr = spec["sr"]
    # Relative to the 11-14 kHz reference band, same as analyze().
    ref_bands = [b for b in (11, 12, 13) if b < nbands]
    ref = float(np.mean([band_db[b] for b in ref_bands])) if ref_bands else float(band_db[0])
    rel = band_db - ref
    freqs = np.arange(nbands)  # kHz, band b covers [b, b+1) kHz
    cutoff_khz = spec["cutoff_hz"] / 1000.0
    # Steepest 1 kHz step at/above 15 kHz (same range as analyze()).
    drops = [rel[b] - rel[b + 1] for b in range(15, nbands - 1)] if nbands > 16 else []
    max_drop = max(drops) if drops else 0.0
    max_khz = 15 + drops.index(max_drop) if drops else 0

    ax.plot(freqs, rel, linewidth=1.5)
    ax.fill_between(freqs, rel, -60, alpha=0.12)
    ax.axvline(cutoff_khz, color="red", linestyle="--", linewidth=1,
               label="cutoff %.0f kHz" % cutoff_khz)
    if drops:
        # Label goes left of the cliff: a cliff at/above 15 kHz would push a
        # right-side label past the axes edge (xlim tops out at sr/2).
        # Clamp y so the label stays inside the (-60, 30) view.
        text_x = max_khz - 5.5 if max_khz >= 15 else max_khz + 2
        text_y = min(max(rel[max_khz] + 8, -55), 22)
        ax.annotate("steepest step\n%.1f dB @ %d kHz" % (max_drop, max_khz),
                    xy=(max_khz, rel[max_khz]), xytext=(text_x, text_y),
                    fontsize=8, arrowprops={"arrowstyle": "->", "lw": 0.8})
    ax.set_xlim(0, sr / 2000.0)
    ax.set_ylim(-60, 30)
    ax.set_xlabel("Frequency (kHz)")
    ax.set_ylabel("Relative energy (dB, vs 11–14 kHz)")
    ax.set_title(title)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, loc="lower left")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Plot lossless-verify mean spectra (genuine vs transcode).")
    p.add_argument("inputs", nargs="*", help="audio file(s) or spectrum JSON(s)")
    p.add_argument("--from-json", action="store_true",
                   help="treat inputs as spectrum JSON files (default: audio)")
    p.add_argument("--compare", nargs=2, metavar=("FILE1", "FILE2"),
                   help="side-by-side comparison of exactly two inputs")
    p.add_argument("--titles", nargs="*", default=None,
                   help="titles for each panel (default: verdict + numbers)")
    p.add_argument("--ffmpeg", default=None, help="path to ffmpeg")
    p.add_argument("--ffprobe", default=None, help="path to ffprobe")
    p.add_argument("--output", "-o", required=True, help="output PNG path")
    args = p.parse_args(argv)

    inputs = list(args.compare) if args.compare else list(args.inputs)
    if not inputs:
        p.error("pass one input, or --compare FILE1 FILE2 for a side-by-side")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is required for plotting: pip install matplotlib",
              file=sys.stderr)
        return 2

    specs = []
    for inp in inputs:
        if args.from_json or inp.lower().endswith(".json"):
            specs.append(spectrum_from_json(inp))
        else:
            specs.append(spectrum_from_audio(inp, args.ffmpeg, args.ffprobe))

    titles = []
    for i, spec in enumerate(specs):
        if args.titles and i < len(args.titles):
            titles.append(args.titles[i])
        else:
            titles.append("%s — cutoff %.0f kHz, max drop %.1f dB"
                          % (spec.get("verdict", "?"),
                             spec["cutoff_hz"] / 1000.0, spec["max_drop_db"]))

    if args.compare:
        fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), sharey=True)
        for ax, spec, title in zip(axes, specs, titles):
            plot_one(ax, spec, title)
        fig.suptitle("Mean spectrum: genuine vs transcode", fontsize=12)
        fig.tight_layout()
    else:
        fig, ax = plt.subplots(figsize=(8, 4.5))
        plot_one(ax, specs[0], titles[0])
        fig.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    fig.savefig(args.output, dpi=150)
    print("wrote %s" % args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
