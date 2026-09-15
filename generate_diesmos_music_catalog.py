#!/usr/bin/env python3
"""
Generate a browser-friendly music catalog by scanning the folder containing
this script (or a folder passed with --root).

Supported title/file-name formats include:
  01 - Song Title.mp3
  01–Song Title.mp3
  01 — Song Title.mp3
  01_ Song Title.mp3
  01. Song Title.mp3
  01) Song Title.mp3
  01: Song Title.mp3
  01 Song Title.mp3
  [01] Song Title.mp3
  (01) Song Title.mp3

The script does NOT contact GitHub. It scans local files and writes the
catalog next to the music files by default.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BRANCH = "main"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".wav", ".flac", ".aac", ".opus"}

# Deliberately conservative: only treat a leading 1–3 digit number as a track
# when it is followed by an obvious separator OR whitespace + a title.
TRACK_PATTERNS = (
    re.compile(r"^\s*\[(?P<number>\d{1,3})\]\s*(?P<title>.+?)\s*$"),
    re.compile(r"^\s*\((?P<number>\d{1,3})\)\s*(?P<title>.+?)\s*$"),
    re.compile(
        r"^\s*(?P<number>\d{1,3})\s*(?:[-_.:–—)]\s*|\s+)(?P<title>.+?)\s*$"
    ),
)


def clean_track_title(filename: str) -> tuple[int, str]:
    """Return (track_number, clean_title) for many common naming styles."""
    stem = Path(filename).stem.strip()

    for pattern in TRACK_PATTERNS:
        match = pattern.match(stem)
        if not match:
            continue

        number = int(match.group("number"))
        title = match.group("title").strip()

        # Avoid treating obvious years / long numeric prefixes as track numbers.
        if number == 0:
            return 9999, stem

        # Remove a small amount of accidental leftover punctuation.
        title = re.sub(r"^[-_.:–—]+\s*", "", title).strip()
        if title:
            return number, title

    return 9999, stem


def encode_path(path: Path, root: Path) -> str:
    return "/".join(
        urllib.parse.quote(part, safe="")
        for part in path.relative_to(root).parts
    )


def hosted_url(relative_path: str) -> str:
    return (
        "https://fastly.jsdelivr.net/gh/waycrosspublicmedia/diesmosmusic@main/"
        + "/".join(
            urllib.parse.quote(part, safe="")
            for part in relative_path.split("/")
        )
    )


def choose_cover(album_dir: Path, root: Path) -> str:
    """Prefer common cover-art filenames, then any image in the album folder."""
    preferred = {
        "cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "cover.gif",
        "folder.jpg", "folder.jpeg", "folder.png", "folder.webp", "folder.gif",
        "album.jpg", "album.jpeg", "album.png", "album.webp", "album.gif",
    }

    candidates = sorted(
        (p for p in album_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
        key=lambda p: p.name.casefold(),
    )

    for candidate in candidates:
        if candidate.name.casefold() in preferred:
            return hosted_url(candidate.relative_to(root).as_posix())

    if candidates:
        return hosted_url(candidates[0].relative_to(root).as_posix())

    return ""


def build_catalog(root: Path) -> dict[str, Any]:
    artists: dict[str, dict[str, Any]] = {}

    audio_files = sorted(
        (
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS
        ),
        key=lambda p: p.relative_to(root).as_posix().casefold(),
    )

    for path in audio_files:
        rel = path.relative_to(root)
        parts = rel.parts
        filename = parts[-1]
        track_number, title = clean_track_title(filename)

        if len(parts) >= 3:
            artist_name = parts[0]
            album_name = parts[1]
            album_dir = root.joinpath(*parts[:-1])
        elif len(parts) == 2:
            artist_name = parts[0]
            album_name = "Singles"
            album_dir = root / parts[0]
        else:
            artist_name = "Diesmos Music"
            album_name = "Singles"
            album_dir = root

        artist = artists.setdefault(
            artist_name,
            {"name": artist_name, "albums": {}},
        )
        album = artist["albums"].setdefault(
            album_name,
            {"name": album_name, "coverArtUrl": "", "songs": []},
        )

        if not album["coverArtUrl"] and album_dir.exists():
            album["coverArtUrl"] = choose_cover(album_dir, root)
            for existing_song in album["songs"]:
                existing_song["coverArtUrl"] = album["coverArtUrl"]

        album["songs"].append({
            "title": title,
            "track": track_number,
            "artist": artist_name,
            "album": album_name,
            "filename": filename,
            "path": rel.as_posix(),
            "url": hosted_url(rel.as_posix()),
            "coverArtUrl": album["coverArtUrl"],
        })

    artist_list = []
    for artist_name, artist in sorted(
        artists.items(), key=lambda item: item[0].casefold()
    ):
        albums = []
        for album_name, album in sorted(
            artist["albums"].items(), key=lambda item: item[0].casefold()
        ):
            album["songs"].sort(
                key=lambda song: (song["track"], song["title"].casefold())
            )
            albums.append(album)
        artist_list.append({"name": artist_name, "albums": albums})

    return {
        "schemaVersion": 2,
        "source": "waycrosspublicmedia/diesmosmusic",
        "branch": DEFAULT_BRANCH,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "artists": artist_list,
    }


def write_outputs(catalog: dict[str, Any], output_dir: Path, js_name: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pretty = json.dumps(catalog, ensure_ascii=False, indent=2)

    js_text = (
        "// Generated by generate_diesmos_music_catalog.py\n"
        "window.DIESMOS_MUSIC_CATALOG = " + pretty + ";\n"
    )

    (output_dir / js_name).write_text(js_text, encoding="utf-8")
    (output_dir / "diesmos-music-catalog.json").write_text(
        pretty + "\n", encoding="utf-8"
    )

    lines: list[str] = []
    for artist in catalog["artists"]:
        lines.append(artist["name"])
        for album in artist["albums"]:
            lines.append(f"  {album['name']}")
            for song in album["songs"]:
                track = (
                    f"{song['track']:02d}"
                    if song["track"] < 9999
                    else "--"
                )
                lines.append(f"    {track} - {song['title']}")
        lines.append("")

    (output_dir / "diesmos-music-catalog.txt").write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8"
    )


def main() -> int:
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description=(
            "Generate the Diesmos Music catalog by scanning the folder "
            "containing this script."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=script_dir,
        help="Music repository root. Defaults to the script's folder.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=script_dir,
        help="Where to write generated catalog files.",
    )
    parser.add_argument(
        "--js-name",
        default="diesmos-music-catalog.js",
        help="Generated JS filename.",
    )

    args = parser.parse_args()
    root = args.root.resolve()
    output_dir = args.output_dir.resolve()

    if not root.exists() or not root.is_dir():
        print(f"Error: music folder does not exist: {root}", file=sys.stderr)
        return 1

    catalog = build_catalog(root)
    write_outputs(catalog, output_dir, args.js_name)

    artists = len(catalog["artists"])
    albums = sum(len(a["albums"]) for a in catalog["artists"])
    songs = sum(
        len(album["songs"])
        for artist in catalog["artists"]
        for album in artist["albums"]
    )

    print(f"Scanned: {root}")
    print(f"Generated {artists} artists, {albums} albums, {songs} songs.")
    print(
        f"Wrote {args.js_name}, diesmos-music-catalog.json, "
        f"and diesmos-music-catalog.txt to {output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
