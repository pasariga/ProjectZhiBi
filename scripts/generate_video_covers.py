"""Generate poster images from local microcourse videos.

The app serves the repository root as static files, so videos under
``videos/`` are available as ``/videos/...`` and generated covers under
``covers/`` are available as ``/covers/...``.

Preferred backend: ffmpeg, when it is installed and on PATH.
Fallback backend: installed Chrome/Edge headless screenshot of a seeked video.
"""
from __future__ import annotations

import argparse
import html
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIDEO_DIR = ROOT / "videos"
COVER_DIR = ROOT / "covers"
DEFAULT_TIMESTAMP = 1.0
VIEWPORT = (960, 540)


def find_browser() -> str | None:
    candidates = [
        os.environ.get("CHROME_PATH"),
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def generate_with_ffmpeg(video_path: Path, cover_path: Path, timestamp: float, overwrite: bool) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    if cover_path.exists() and not overwrite:
        return True
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y" if overwrite else "-n",
        "-ss",
        f"{timestamp:.2f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={VIEWPORT[0]}:{VIEWPORT[1]}:force_original_aspect_ratio=increase,crop={VIEWPORT[0]}:{VIEWPORT[1]}",
        str(cover_path),
    ]
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return completed.returncode == 0 and cover_path.exists() and cover_path.stat().st_size > 0


def generate_with_browser(video_path: Path, cover_path: Path, timestamp: float, overwrite: bool) -> bool:
    browser = find_browser()
    if not browser:
        return False
    if cover_path.exists() and not overwrite:
        return True
    cover_path.parent.mkdir(parents=True, exist_ok=True)

    video_url = video_path.resolve().as_uri()
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_html = Path(tmp_dir) / "capture.html"
        tmp_html.write_text(
            f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    html, body {{
      width: {VIEWPORT[0]}px;
      height: {VIEWPORT[1]}px;
      margin: 0;
      overflow: hidden;
      background: #eef6ff;
    }}
    video {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
      background: #eef6ff;
    }}
  </style>
</head>
<body>
  <video id="video" muted playsinline preload="auto" src="{html.escape(video_url)}"></video>
  <script>
    const video = document.getElementById('video');
    const targetTime = {timestamp:.2f};
    video.addEventListener('loadedmetadata', () => {{
      const duration = Number.isFinite(video.duration) ? video.duration : targetTime;
      video.currentTime = Math.max(0.1, Math.min(targetTime, Math.max(0.1, duration - 0.15)));
    }});
    video.addEventListener('seeked', () => {{
      document.body.setAttribute('data-ready', '1');
    }});
  </script>
</body>
</html>
""",
            encoding="utf-8",
        )
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--mute-audio",
            "--autoplay-policy=no-user-gesture-required",
            f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}",
            "--virtual-time-budget=5000",
            f"--screenshot={cover_path}",
            tmp_html.resolve().as_uri(),
        ]
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return completed.returncode == 0 and cover_path.exists() and cover_path.stat().st_size > 0


def generate_covers(video_dir: Path, cover_dir: Path, timestamp: float, overwrite: bool) -> list[Path]:
    videos = sorted(video_dir.glob("*.mp4"))
    generated: list[Path] = []
    for video_path in videos:
        cover_path = cover_dir / f"{video_path.stem}.png"
        ok = generate_with_ffmpeg(video_path, cover_path, timestamp, overwrite)
        if not ok:
            ok = generate_with_browser(video_path, cover_path, timestamp, overwrite)
        if not ok:
            raise RuntimeError(f"Could not generate cover for {video_path.name}. Install ffmpeg or Chrome/Edge.")
        generated.append(cover_path)
    return generated


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate static cover images from local videos.")
    parser.add_argument("--videos", type=Path, default=VIDEO_DIR, help="Directory containing mp4 files.")
    parser.add_argument("--covers", type=Path, default=COVER_DIR, help="Directory for generated cover images.")
    parser.add_argument("--time", type=float, default=DEFAULT_TIMESTAMP, help="Capture timestamp in seconds.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing covers.")
    args = parser.parse_args()

    generated = generate_covers(args.videos, args.covers, args.time, args.overwrite)
    for cover in generated:
        print(cover.relative_to(ROOT))


if __name__ == "__main__":
    main()
