#!/usr/bin/env python3
"""
Generate a browser-friendly music catalog by scanning the folder containing
this script (or a folder passed with --root).

On the FIRST run, every file and folder inside the music root is renamed to
a deterministic, hash-based gibberish name. The original -> obfuscated
mapping is written to ``obfuscation-map.txt`` next to the generated catalog.

Subsequent runs reuse that map (and only obfuscate any *new* files that were
added since the last run), so the on-disk tree stays obfuscated while the
generated catalog still shows the original artist, album and song titles.

In addition, the generated JS/JSON catalog stores the ``title``, ``artist``
and ``album`` strings *reversed* (case preserved). The site is expected to
reverse them again before display. The plain-text catalog is written with the
original, human-readable names.

Pass ``--no-obfuscate`` to skip renaming entirely and only (re)generate the
catalog. Delete ``obfuscation-map.txt`` to force a fresh obfuscation pass
(note: this will re-hash the already-obfuscated names, producing different
gibberish).

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
import hashlib
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

MAP_FILENAME = "obfuscation-map.txt"
CATALOG_JSON_NAME = "diesmos-music-catalog.json"
CATALOG_TXT_NAME = "diesmos-music-catalog.txt"

# Names that must never be renamed or scanned into the catalog.
EXCLUDED_DIR_NAMES = {".git"}
EXCLUDED_FILE_NAMES = {"blankcd.png"}

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


def reverse_text(text: str) -> str:
    """Reverse a string while keeping character case intact.

    Python's slice reversal already preserves case character-for-character,
    so "Hello World" -> "dlroW olleH". The helper exists mainly to make the
    intent explicit and to give us a single place to change the transform
    later if needed.
    """
    return text[::-1]


def is_excluded_name(name: str) -> bool:
    """True if this name must never be renamed / scanned into the catalog."""
    return name in EXCLUDED_DIR_NAMES or name in EXCLUDED_FILE_NAMES


# ---------------------------------------------------------------------------
# Obfuscation helpers
# ---------------------------------------------------------------------------

def gibberish_name(original_name: str, is_dir: bool, extra: str = "") -> str:
    """Return a deterministic gibberish name for a file or folder.

    The file extension is preserved so browsers can still play audio / show
    images. ``extra`` is only used to resolve the (extremely unlikely) hash
    collision inside the same parent folder.
    """
    salt = "d:" if is_dir else "f:"
    digest = hashlib.sha256(
        (salt + original_name + extra).encode("utf-8")
    ).hexdigest()
    stem = digest[:16]
    if is_dir:
        return stem
    return stem + Path(original_name).suffix


def load_obfuscation_map(map_path: Path) -> dict[str, str]:
    """Load the map file.

    Returns a *reverse* mapping: {obfuscated_name: original_name}. The same
    original basename always maps to the same obfuscated name, so this is
    enough to restore the human-readable names for the catalog.
    """
    reverse: dict[str, str] = {}
    if not map_path.exists():
        return reverse

    for raw_line in map_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        obfuscated, original = parts[0].strip(), parts[1]
        if obfuscated and original:
            reverse[obfuscated] = original
    return reverse


def save_obfuscation_map(map_path: Path, reverse: dict[str, str]) -> None:
    lines = [
        "# Diesmos Music obfuscation map",
        "#",
        "# Format (tab separated): <obfuscated name>\t<original name>",
        "#",
        "# Every file and folder under the music root was renamed once to the",
        "# obfuscated name shown on the left. The generated catalog still",
        "# shows the original artist / album / song names, so this file is",
        "# what keeps the catalog readable. Delete it only if you want a",
        "# fresh obfuscation pass.",
        "#",
    ]
    for obfuscated, original in sorted(
        reverse.items(), key=lambda kv: kv[1].casefold()
    ):
        lines.append(f"{obfuscated}\t{original}")
    map_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def obfuscate_tree(
    root: Path,
    output_dir: Path,
    excluded_files: set[Path],
    reverse: dict[str, str],
) -> None:
    """Rename every not-yet-obfuscated file/folder under ``root`` in place.

    Updates ``reverse`` with any new mappings. Items whose current name is
    already a known obfuscated name are left alone (so this is idempotent).
    The ``.git`` directory and any ``blankcd.png`` are never touched.
    """
    root_res = root.resolve()
    out_res = output_dir.resolve()
    skip_output_subtree = out_res != root_res

    def is_excluded(p: Path) -> bool:
        # Hard exclusions by name (case-insensitive for safety).
        if p.name.casefold() in EXCLUDED_DIR_NAMES:
            return True
        if p.name.casefold() in EXCLUDED_FILE_NAMES:
            return True

        try:
            rp = p.resolve()
        except OSError:
            return False
        if rp in excluded_files:
            return True
        if skip_output_subtree:
            try:
                rp.relative_to(out_res)
                return True
            except ValueError:
                pass
        return False

    def recurse(dir_path: Path) -> None:
        # Walk children sorted for deterministic naming.
        for child in sorted(dir_path.iterdir(), key=lambda p: p.name.casefold()):
            if is_excluded(child):
                continue

            is_dir = child.is_dir()

            # Already obfuscated? Recurse into dirs to catch new files,
            # then leave the name alone.
            if child.name in reverse:
                if is_dir:
                    recurse(child)
                continue

            # Recurse *before* renaming the directory so that children are
            # renamed while the parent still has its original path.
            if is_dir:
                recurse(child)

            gibberish = gibberish_name(child.name, is_dir)
            target = child.parent / gibberish
            counter = 0
            while target.exists() and target != child:
                counter += 1
                gibberish = gibberish_name(
                    child.name, is_dir, extra=f":{counter}"
                )
                target = child.parent / gibberish

            child.rename(target)
            reverse[gibberish] = child.name

    recurse(root_res)


# ---------------------------------------------------------------------------
# Catalog building
# ---------------------------------------------------------------------------

def choose_cover(
    album_dir: Path,
    root: Path,
    reverse: dict[str, str],
) -> str:
    """Prefer common cover-art filenames, then any image in the album folder."""
    preferred = {
        "cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "cover.gif",
        "folder.jpg", "folder.jpeg", "folder.png", "folder.webp", "folder.gif",
        "album.jpg", "album.jpeg", "album.png", "album.webp", "album.gif",
    }

    candidates = sorted(
        (
            p for p in album_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() in IMAGE_EXTS
            and p.name.casefold() not in EXCLUDED_FILE_NAMES
        ),
        key=lambda p: p.name.casefold(),
    )

    for candidate in candidates:
        # The file on disk is obfuscated - compare its *original* name.
        display_name = reverse.get(candidate.name, candidate.name).casefold()
        if display_name in preferred:
            return hosted_url(candidate.relative_to(root).as_posix())

    if candidates:
        return hosted_url(candidates[0].relative_to(root).as_posix())

    return ""


def build_catalog(root: Path, reverse: dict[str, str]) -> dict[str, Any]:
    """Build the *display* catalog (human-readable names).

    The JSON/JS writer will reverse the title/artist/album strings before
    serialising them; this function keeps the originals so the TXT output
    and any internal logic stays sane.
    """
    artists: dict[str, dict[str, Any]] = {}

    def display(name: str) -> str:
        return reverse.get(name, name)

    def is_under_excluded_dir(p: Path) -> bool:
        try:
            rel = p.relative_to(root)
        except ValueError:
            return False
        return any(
            part.casefold() in EXCLUDED_DIR_NAMES for part in rel.parts[:-1]
        )

    audio_files = sorted(
        (
            p for p in root.rglob("*")
            if p.is_file()
            and p.suffix.lower() in AUDIO_EXTS
            and p.name.casefold() not in EXCLUDED_FILE_NAMES
            and not is_under_excluded_dir(p)
        ),
        key=lambda p: p.relative_to(root).as_posix().casefold(),
    )

    for path in audio_files:
        rel = path.relative_to(root)
        parts = rel.parts
        # Restore the human-readable names for display purposes.
        display_parts = [display(part) for part in parts]
        filename = display_parts[-1]
        track_number, title = clean_track_title(filename)

        if len(parts) >= 3:
            artist_name = display_parts[0]
            album_name = display_parts[1]
            album_dir = root.joinpath(*parts[:-1])
        elif len(parts) == 2:
            artist_name = display_parts[0]
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
            album["coverArtUrl"] = choose_cover(album_dir, root, reverse)
            for existing_song in album["songs"]:
                existing_song["coverArtUrl"] = album["coverArtUrl"]

        album["songs"].append({
            "title": title,
            "track": track_number,
            "artist": artist_name,
            "album": album_name,
            # Actual on-disk (obfuscated) filename + path, so the player can
            # still find the file.
            "filename": parts[-1],
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


def reverse_catalog_strings(catalog: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``catalog`` with title/artist/album strings reversed.

    Track numbers, paths, filenames and URLs are left untouched - only the
    three human-readable display strings are transformed.
    """
    reversed_catalog: dict[str, Any] = {
        "schemaVersion": catalog["schemaVersion"],
        "source": catalog["source"],
        "branch": catalog["branch"],
        "generatedAt": catalog["generatedAt"],
        "artists": [],
    }

    for artist in catalog["artists"]:
        new_artist = {
            "name": reverse_text(artist["name"]),
            "albums": [],
        }
        for album in artist["albums"]:
            new_album = {
                "name": reverse_text(album["name"]),
                "coverArtUrl": album["coverArtUrl"],
                "songs": [],
            }
            for song in album["songs"]:
                new_song = dict(song)
                new_song["title"] = reverse_text(song["title"])
                new_song["artist"] = reverse_text(song["artist"])
                new_song["album"] = reverse_text(song["album"])
                new_album["songs"].append(new_song)
            new_artist["albums"].append(new_album)
        reversed_catalog["artists"].append(new_artist)

    return reversed_catalog


def write_outputs(catalog: dict[str, Any], output_dir: Path, js_name: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON/JS get the reversed display strings; the site reverses them back.
    json_catalog = reverse_catalog_strings(catalog)
    pretty_json = json.dumps(json_catalog, ensure_ascii=False, indent=2)
    pretty_original = json.dumps(catalog, ensure_ascii=False, indent=2)

    js_text = (
        "// Generated by generate_diesmos_music_catalog.py\n"
        "// title/artist/album are stored reversed; reverse them again\n"
        "// (preserving case) before display.\n"
        "window.DIESMOS_MUSIC_CATALOG = " + pretty_json + ";\n"
    )

    (output_dir / js_name).write_text(js_text, encoding="utf-8")
    (output_dir / CATALOG_JSON_NAME).write_text(
        pretty_json + "\n", encoding="utf-8"
    )

    # The TXT stays fully human-readable (original, unreversed strings).
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

    (output_dir / CATALOG_TXT_NAME).write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8"
    )

    # Keep the internal reference around in case callers want it.
    return pretty_original


def main() -> int:
    script_path = Path(__file__).resolve()
    script_dir = script_path.parent

    parser = argparse.ArgumentParser(
        description=(
            "Generate the Diesmos Music catalog by scanning the folder "
            "containing this script. Files and folders are obfuscated on the "
            f"first run; original names are recorded in {MAP_FILENAME} so the "
            "catalog stays readable. The JS/JSON catalog stores title/artist/"
            "album reversed; the site reverses them back for display. "
            ".git and blankcd.png are always excluded."
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
        help="Where to write generated catalog files (and the obfuscation map).",
    )
    parser.add_argument(
        "--js-name",
        default="diesmos-music-catalog.js",
        help="Generated JS filename.",
    )
    parser.add_argument(
        "--no-obfuscate",
        action="store_true",
        help="Skip renaming files; only (re)generate the catalog.",
    )

    args = parser.parse_args()
    root = args.root.resolve()
    output_dir = args.output_dir.resolve()

    if not root.exists() or not root.is_dir():
        print(f"Error: music folder does not exist: {root}", file=sys.stderr)
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    map_path = output_dir / MAP_FILENAME

    # Load any existing map. If it already has entries, we know we've
    # obfuscated before and can reuse the same names.
    reverse = load_obfuscation_map(map_path)
    reused_existing_map = bool(reverse)

    if not args.no_obfuscate:
        excluded_files = {
            script_path,
            map_path.resolve(),
            (output_dir / CATALOG_JSON_NAME).resolve(),
            (output_dir / CATALOG_TXT_NAME).resolve(),
            (output_dir / args.js_name).resolve(),
        }
        obfuscate_tree(root, output_dir, excluded_files, reverse)
        save_obfuscation_map(map_path, reverse)

    catalog = build_catalog(root, reverse)
    write_outputs(catalog, output_dir, args.js_name)

    artists = len(catalog["artists"])
    albums = sum(len(a["albums"]) for a in catalog["artists"])
    songs = sum(
        len(album["songs"])
        for artist in catalog["artists"]
        for album in artist["albums"]
    )

    if args.no_obfuscate:
        print(f"Scanned: {root} (obfuscation skipped)")
    elif reused_existing_map:
        print(f"Scanned: {root} (loaded existing {MAP_FILENAME})")
    else:
        print(f"Scanned: {root} (files obfuscated, map saved)")

    print(f"Generated {artists} artists, {albums} albums, {songs} songs.")
    print(
        f"Wrote {args.js_name}, {CATALOG_JSON_NAME}, "
        f"and {CATALOG_TXT_NAME} to {output_dir}"
    )
    if not args.no_obfuscate:
        print(f"Obfuscation map: {map_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())