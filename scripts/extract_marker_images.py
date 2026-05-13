#!/usr/bin/env python3
"""Extract Delaware marker image URLs from marker page URLs."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

HREF_RE = re.compile(r'href\s*=\s*"([^"]+)"', re.IGNORECASE)
ANCHOR_TEXT_RE = re.compile(r">([^<]+)<")
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
ATTR_RE = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

USER_AGENT = "visit-de-history-marker-image-extractor/1.0 (+https://archives.delaware.gov)"

SKIP_SUBSTRINGS = [
    "archives-menu-branding",
    "archives-interior-logo",
    "marker-page-header",
    "marker-program",
    "marker_photo_gallery",
    "city_",
    "/wp-content/themes/",
    "googleusercontent",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Path to input JSON file")
    parser.add_argument(
        "--output",
        default="marker-images.csv",
        help="CSV output path (default: marker-images.csv)",
    )
    parser.add_argument(
        "--download-dir",
        default="",
        help="Optional directory to download image files",
    )
    parser.add_argument("--limit", type=int, default=0, help="Process only first N rows")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.15,
        help="Delay between requests in seconds (default: 0.15)",
    )
    return parser.parse_args()


def parse_marker_url(marker_number_field: str) -> tuple[str, str]:
    marker_number_field = marker_number_field or ""
    href_match = HREF_RE.search(marker_number_field)
    text_match = ANCHOR_TEXT_RE.search(marker_number_field)

    url = html.unescape(href_match.group(1).strip()) if href_match else ""
    marker_number = html.unescape(text_match.group(1).strip()) if text_match else ""

    return marker_number, url


def parse_img_src(img_tag: str) -> str:
    attrs = {}
    for key, _, value in ATTR_RE.findall(img_tag):
        attrs[key.lower()] = html.unescape(value.strip())

    for key in ("src", "data-src", "data-lazy-src", "data-orig-file"):
        if attrs.get(key):
            return attrs[key]
    return ""


def choose_marker_image(page_html: str) -> tuple[str, str]:
    img_tags = IMG_TAG_RE.findall(page_html)

    candidates = []
    for tag in img_tags:
        src = parse_img_src(tag)
        if not src:
            continue
        lower_src = src.lower()
        if "/wp-content/uploads/" not in lower_src:
            continue
        if any(skip in lower_src for skip in SKIP_SUBSTRINGS):
            continue
        candidates.append(src)

    if candidates:
        return candidates[0], "ok"

    og = OG_IMAGE_RE.search(page_html)
    if og:
        return html.unescape(og.group(1)), "fallback_og_image"

    return "", "no_image_found"


def safe_filename(prefix: str, url: str) -> str:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path) or "image.jpg"
    clean_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "-", prefix).strip("-") or "marker"
    return f"{clean_prefix}__{name}"


def fetch_text(url: str, timeout: int = 30) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        encoding = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(encoding, errors="replace")


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def maybe_download_image(image_url: str, out_dir: Path, file_prefix: str) -> tuple[str, str]:
    if not image_url:
        return "", ""

    out_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(file_prefix, image_url)
    out_path = out_dir / filename

    try:
        data = fetch_bytes(image_url, timeout=30)
        out_path.write_bytes(data)
        return str(out_path), ""
    except Exception as exc:  # noqa: BLE001
        return "", f"download_error: {exc}"


def load_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload, list):
        return payload
    raise ValueError("Expected JSON array or object with a 'data' array")


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    download_dir = Path(args.download_dir) if args.download_dir else None

    rows = load_rows(input_path)
    if args.limit > 0:
        rows = rows[: args.limit]

    results: list[dict[str, str]] = []
    for idx, row in enumerate(rows, start=1):
        marker_number_field = str(row.get("Marker Number", ""))
        marker_name = str(row.get("Marker Name", "")).strip()
        city = str(row.get("City/Town", "")).strip()

        marker_number, marker_url = parse_marker_url(marker_number_field)

        status = "ok"
        note = ""
        image_url = ""
        local_image_path = ""

        if not marker_url:
            status = "no_marker_url"
            note = "Could not parse href from Marker Number field"
        else:
            try:
                page_html = fetch_text(marker_url, timeout=30)
                image_url, status = choose_marker_image(page_html)
                if not image_url:
                    note = "No candidate image found"
                if download_dir and image_url:
                    local_image_path, dl_note = maybe_download_image(
                        image_url,
                        download_dir,
                        marker_number or marker_name or f"row-{idx}",
                    )
                    if dl_note:
                        note = dl_note
                        status = "download_error"
            except Exception as exc:  # noqa: BLE001
                status = "fetch_error"
                note = str(exc)

        results.append(
            {
                "row": str(idx),
                "marker_number": marker_number,
                "marker_name": marker_name,
                "city": city,
                "marker_url": marker_url,
                "image_url": image_url,
                "local_image_path": local_image_path,
                "status": status,
                "note": note,
            }
        )

        time.sleep(max(0.0, args.delay))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "row",
                "marker_number",
                "marker_name",
                "city",
                "marker_url",
                "image_url",
                "local_image_path",
                "status",
                "note",
            ],
        )
        writer.writeheader()
        writer.writerows(results)

    ok_count = sum(1 for r in results if r["status"] in {"ok", "fallback_og_image"})
    print(f"Wrote {len(results)} rows to {output_path} ({ok_count} with image URLs)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
