"""Download YouTube videos and their manually created subtitles from an ID list.

Each non-empty, non-comment line of the input file is a YouTube video ID or URL.
A video is kept only if it has manual subtitles: subtitles are fetched first
(preferred languages, then any manual language) and the video is downloaded
only when subtitles were found.

Re-running with the same arguments resumes: IDs whose video and subtitles are
already on disk are skipped; failed or interrupted IDs are retried.

Example:
    python Dataset_Processor/yt_downloader.py \
        --input datasets/youtube-asl_youtube_asl_video_ids.txt \
        --video-dir datasets/Videos --subtitle-dir datasets/Subtitles \
        --percent 5
"""

import argparse
import json
import math
import os
import re
import shutil
import sys
import time
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from tqdm import tqdm

VIDEO_EXTS = ("mp4", "mkv", "webm")
SUB_EXTS = {".vtt", ".srt", ".ass", ".ttml", ".srv1", ".srv2", ".srv3", ".json3"}
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
VIDEO_FORMAT = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best"


# ---------------------------------------------------------
# 1. INPUT
# ---------------------------------------------------------

def stream_video_urls(file_path):
    """Yield each non-empty, non-comment line from a text file."""
    with open(file_path, "r", encoding="utf-8") as file:
        for line in file:
            item = line.strip()
            if item and not item.startswith("#"):
                yield item


def get_video_id(value):
    """Return the 11-character video ID from an ID or a YouTube URL, else None."""
    value = str(value).strip()
    video_id = None

    if "://" not in value and "/" not in value:
        video_id = value
    else:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()

        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
        elif host == "youtube.com" or host.endswith(".youtube.com"):
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [None])[0]
            else:
                parts = parsed.path.strip("/").split("/")
                if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live", "v"}:
                    video_id = parts[1]

    # Strict check: also makes the ID safe to use in file names and globs.
    if video_id and VIDEO_ID_RE.fullmatch(video_id):
        return video_id
    return None


# ---------------------------------------------------------
# 2. YOUTUBE DOWNLOADER
# ---------------------------------------------------------

class YtdlpLogger:
    """Route yt-dlp messages through tqdm so they don't break the progress bar."""

    def __init__(self, verbose):
        self.verbose = verbose

    def _write(self, msg):
        if self.verbose:
            tqdm.write(msg)

    def debug(self, msg):
        if not msg.startswith("[debug] "):
            self._write(msg)

    def info(self, msg):
        self._write(msg)

    def warning(self, msg):
        self._write(msg)

    def error(self, msg):
        # Errors are also raised, recorded in the report and printed as [FAILED].
        self._write(msg)


def make_ydl(args, **overrides):
    options = {
        "format": VIDEO_FORMAT,
        "merge_output_format": "mp4",
        # Relative templates + "paths" puts videos and subtitles in their own dirs.
        "outtmpl": {"default": "%(id)s.%(ext)s", "subtitle": "%(id)s.%(ext)s"},
        "paths": {"home": str(args.video_dir), "subtitle": str(args.subtitle_dir)},
        "writesubtitles": True,
        "writeautomaticsub": False,
        "subtitleslangs": args.sub_langs,
        "ffmpeg_location": args.ffmpeg,
        "cookiefile": str(args.cookies) if args.cookies else None,
        "ignoreerrors": False,
        "noplaylist": True,
        "quiet": not args.verbose,
        "no_warnings": not args.verbose,
        "noprogress": True,
        "logger": YtdlpLogger(args.verbose),
    }
    options.update(overrides)
    return yt_dlp.YoutubeDL(options)


# ---------------------------------------------------------
# 3. FILES ON DISK
# ---------------------------------------------------------

def video_exists(video_id, video_dir):
    # Exact final names only: ".part" files and ".f137.mp4" fragments never match.
    return any((video_dir / f"{video_id}.{ext}").is_file() for ext in VIDEO_EXTS)


def subs_on_disk(video_id, subtitle_dir):
    return any(
        p.suffix in SUB_EXTS and p.is_file()
        for p in subtitle_dir.glob(f"{video_id}.*")
    )


def scan_subtitle_ids(subtitle_dir):
    """One directory scan at startup instead of a glob per ID."""
    ids = set()
    with os.scandir(subtitle_dir) as entries:
        for entry in entries:
            if os.path.splitext(entry.name)[1] in SUB_EXTS and entry.is_file():
                ids.add(entry.name.split(".", 1)[0])
    return ids


def remove_video_files(video_id, video_dir):
    removed = 0
    for path in video_dir.glob(f"{video_id}.*"):
        if path.is_file():
            path.unlink()
            removed += 1
    return removed


def free_gb(path):
    return shutil.disk_usage(path).free / 1024**3


# ---------------------------------------------------------
# 4. ONE VIDEO
# ---------------------------------------------------------

def fetch(video_id, ydls, args, subtitle_ids):
    """Download missing subtitles, then the missing video. Returns (status, error).

    yt-dlp errors (unavailable, private, sign-in, network) propagate to the caller.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    if video_id not in subtitle_ids:
        for ydl in (ydls["subs_pref"], ydls["subs_all"]):
            ydl.extract_info(url, download=True)
            if subs_on_disk(video_id, args.subtitle_dir):
                subtitle_ids.add(video_id)
                break
        else:
            removed = remove_video_files(video_id, args.video_dir)
            msg = "No manually created subtitles available"
            return "no_subs", msg + (" (existing video removed)" if removed else "")

    if not video_exists(video_id, args.video_dir):
        ydls["video"].extract_info(url, download=True)
        if not video_exists(video_id, args.video_dir):
            raise RuntimeError("Download finished but no video file was found")

    return "downloaded", None


# ---------------------------------------------------------
# 5. MAIN LOOP
# ---------------------------------------------------------

def run(args):
    total = sum(1 for _ in stream_video_urls(args.input))
    limit = min(total, max(1, math.ceil(round(total * args.percent / 100, 9)))) if total else 0

    stats = Counter()
    stop_reason = None
    started = time.monotonic()
    subtitle_ids = scan_subtitle_ids(args.subtitle_dir)

    try:
        with ExitStack() as stack:
            ydls = {
                "subs_pref": make_ydl(args, skip_download=True,
                                      writeautomaticsub=args.auto_subs),
                "subs_all": make_ydl(args, skip_download=True,
                                     subtitleslangs=["all", "-live_chat"]),
                "video": make_ydl(args, writesubtitles=False),
            }
            for ydl in ydls.values():
                stack.enter_context(ydl)

            report = stack.enter_context(open(args.report, "a", encoding="utf-8"))
            progress = stack.enter_context(tqdm(
                total=limit,
                desc="Downloading",
                unit="video",
                dynamic_ncols=True,
                mininterval=1 if sys.stderr.isatty() else 30,
            ))

            for line in islice(stream_video_urls(args.input), limit):
                if free_gb(args.video_dir) < args.min_free_gb:
                    stop_reason = f"free disk space below {args.min_free_gb:g} GB"
                    break

                video_id = get_video_id(line)
                status, error, used_network = "failed", None, False

                try:
                    if not video_id:
                        raise ValueError("Invalid YouTube URL or video ID")

                    if video_id in subtitle_ids and video_exists(video_id, args.video_dir):
                        status = "skipped"
                    else:
                        used_network = True
                        status, error = fetch(video_id, ydls, args, subtitle_ids)

                except Exception as exc:
                    status, error = "failed", " ".join(str(exc).split())

                stats[status] += 1
                report.write(json.dumps({
                    "id": video_id or line[:200],
                    "status": status,
                    "error": error,
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }) + "\n")
                report.flush()

                if status == "failed":
                    progress.write(f"[FAILED] {video_id or line[:200]}: {error}")
                elif status == "no_subs" and args.verbose:
                    progress.write(f"[NO SUBS] {video_id}")

                progress.set_postfix(
                    ok=stats["downloaded"], skip=stats["skipped"],
                    no_subs=stats["no_subs"], fail=stats["failed"], refresh=False,
                )
                progress.update(1)

                if used_network and args.sleep:
                    time.sleep(args.sleep)

    except KeyboardInterrupt:
        stop_reason = "interrupted by user"
        return 130
    finally:
        print_summary(args, stats, total, limit, stop_reason, time.monotonic() - started)

    return 0


def print_summary(args, stats, total, limit, stop_reason, elapsed):
    print("\nDownload Summary")
    print("----------------")
    print(f"Selected:         {limit} / {total} IDs ({args.percent:g}%)")
    print(f"Processed:        {sum(stats.values())}")
    print(f"  Downloaded:     {stats['downloaded']}")
    print(f"  Already there:  {stats['skipped']}")
    print(f"  No subtitles:   {stats['no_subs']}")
    print(f"  Failed:         {stats['failed']}")
    if stop_reason:
        print(f"Stopped early:    {stop_reason}")
    print(f"Elapsed:          {timedelta(seconds=round(elapsed))}")
    print(f"Report:           {args.report}")


# ---------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------

def percent_type(value):
    pct = float(value)
    if not 0 < pct <= 100:
        raise argparse.ArgumentTypeError("must be in the range (0, 100]")
    return pct


def non_negative(value):
    num = float(value)
    if num < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return num


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Download YouTube videos that have manual subtitles, from an ID list.")
    parser.add_argument("--input", required=True, type=Path,
                        help="Text file with one YouTube video ID or URL per line")
    parser.add_argument("--video-dir", type=Path, default=Path("datasets/Videos"))
    parser.add_argument("--subtitle-dir", type=Path, default=Path("datasets/Subtitles"))
    parser.add_argument("--report", type=Path, default=None,
                        help="JSONL report, appended per ID (default: <video-dir>/../download_report.jsonl)")
    parser.add_argument("--percent", type=percent_type, default=100.0,
                        help="Download only the first N%% of the IDs in file order (default: 100)")
    parser.add_argument("--sub-langs", default="en.*,eng.*,english.*",
                        help="Comma-separated preferred subtitle languages (regex allowed)")
    parser.add_argument("--auto-subs", action="store_true",
                        help="Also accept auto-generated subtitles in the preferred languages")
    parser.add_argument("--cookies", type=Path, default=None,
                        help="Netscape cookies.txt for YouTube (helps with sign-in / bot checks)")
    parser.add_argument("--sleep", type=non_negative, default=2.0,
                        help="Seconds to wait after each ID that hit the network (default: 2)")
    parser.add_argument("--min-free-gb", type=non_negative, default=20.0,
                        help="Stop cleanly when free space in --video-dir drops below this (default: 20)")
    parser.add_argument("--ffmpeg", default=None, help="Path to ffmpeg (default: found on PATH)")
    parser.add_argument("--verbose", action="store_true", help="Show yt-dlp output")
    args = parser.parse_args(argv)

    args.input = args.input.expanduser().resolve()
    if not args.input.is_file():
        parser.error(f"input file not found: {args.input}")

    args.ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    if not args.ffmpeg or not Path(args.ffmpeg).exists():
        parser.error("ffmpeg not found; install it or pass --ffmpeg")

    if args.cookies:
        args.cookies = args.cookies.expanduser().resolve()
        if not args.cookies.is_file():
            parser.error(f"cookies file not found: {args.cookies}")

    args.sub_langs = [lang.strip() for lang in args.sub_langs.split(",") if lang.strip()]
    if not args.sub_langs:
        parser.error("--sub-langs must contain at least one language")

    args.video_dir = args.video_dir.expanduser().resolve()
    args.subtitle_dir = args.subtitle_dir.expanduser().resolve()
    args.report = (args.report or args.video_dir.parent / "download_report.jsonl").expanduser().resolve()
    return args


def main(argv=None):
    # Keep [FAILED] lines in order with the bar when output is redirected to a log.
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args(argv)
    args.video_dir.mkdir(parents=True, exist_ok=True)
    args.subtitle_dir.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
