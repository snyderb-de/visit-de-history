#!/usr/bin/env python3
from __future__ import annotations

import csv
import html
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

USER_AGENT = "visit-de-history-non-marker-photo-fetcher/1.0"

META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
SCRIPT_JSONLD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
ATTR_RE = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
CSS_URL_RE = re.compile(r"url\((['\"]?)([^)'\"]+)\1\)", re.IGNORECASE)
IMAGE_VAR_RE = re.compile(r"(?:const\s+imageUrl|imageUrl)\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)

SKIP_PATTERNS = [
    "logo",
    "favicon",
    "icon",
    "sprite",
    "avatar",
    "seal",
    "badge",
    "tracking",
    "analytics",
    "pixel",
    "doubleclick",
    "gravatar",
    "blavatar",
    "wpcom-gray-white",
    "delaware_global_",
    "pixel.png",
]

GOOD_HINTS = [
    "house",
    "hall",
    "church",
    "museum",
    "historic",
    "lighthouse",
    "courthouse",
    "fort",
    "mansion",
    "district",
    "building",
    "camp",
]

EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass
class Candidate:
    url: str
    source: str
    score: float


def slug(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def parse_attrs(tag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, _, v in ATTR_RE.findall(tag):
        out[k.lower()] = html.unescape(v.strip())
    return out


def fetch_text(url: str, timeout: int = 30) -> tuple[str, str]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        final = resp.geturl()
        body = resp.read().decode(charset, errors="replace")
        return final, body


def fetch_bytes(url: str, timeout: int = 30) -> tuple[bytes, str, str]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"})
    with urlopen(req, timeout=timeout) as resp:
        final = resp.geturl()
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return resp.read(), ctype, final


def norm_url(base: str, raw: str) -> str:
    raw = html.unescape((raw or "").strip())
    if not raw:
        return ""
    if raw.startswith("data:"):
        return ""
    return urljoin(base, raw)


def iter_jsonld_images(blob: str) -> Iterable[str]:
    blob = blob.strip()
    if not blob:
        return []
    try:
        doc = json.loads(blob)
    except Exception:
        return []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                lk = str(k).lower()
                if lk == "image":
                    if isinstance(v, str):
                        yield v
                    elif isinstance(v, dict):
                        for kk in ("url", "contenturl", "thumbnailurl"):
                            vv = v.get(kk) or v.get(kk.capitalize())
                            if isinstance(vv, str):
                                yield vv
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, str):
                                yield item
                            elif isinstance(item, dict):
                                for kk in ("url", "contentUrl", "thumbnailUrl"):
                                    vv = item.get(kk)
                                    if isinstance(vv, str):
                                        yield vv
                yield from walk(v)
        elif isinstance(x, list):
            for i in x:
                yield from walk(i)

    return walk(doc)


def site_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    stop = {"the", "and", "of", "at", "in", "for", "a", "an", "or", "county", "community", "center"}
    return {w for w in words if len(w) > 2 and w not in stop}


def score_candidate(img_url: str, source_url: str, site_name: str, origin: str) -> float:
    u = img_url.lower()
    src_domain = (urlparse(source_url).netloc or "").lower()
    img_domain = (urlparse(img_url).netloc or "").lower()

    score = 0.0
    if origin == "og":
        score += 48
    elif origin == "twitter":
        score += 44
    elif origin == "jsonld":
        score += 40
    else:
        score += 28

    if src_domain and img_domain == src_domain:
        score += 8

    if any(p in u for p in SKIP_PATTERNS):
        score -= 50

    if u.endswith(".svg"):
        score -= 35

    toks = site_tokens(site_name)
    tok_hits = sum(1 for t in toks if t in u)
    score += tok_hits * 6

    hint_hits = sum(1 for h in GOOD_HINTS if h in u)
    score += hint_hits * 2

    if "uploads" in u:
        score += 4

    if any(x in u for x in ["hero", "header", "banner"]):
        score -= 6

    return score


def parse_dim(val: str) -> int:
    v = (val or "").strip().lower()
    if not v:
        return 0
    m = re.search(r"\d+(?:\.\d+)?", v)
    if not m:
        return 0
    try:
        return int(float(m.group(0)))
    except Exception:
        return 0


def extract_candidates(final_url: str, html_text: str, site_name: str) -> list[Candidate]:
    found: list[Candidate] = []

    for tag in META_RE.findall(html_text):
        attrs = parse_attrs(tag)
        prop = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content", "")
        if prop in {"og:image", "og:image:url"}:
            u = norm_url(final_url, content)
            if u:
                found.append(Candidate(u, "og", score_candidate(u, final_url, site_name, "og")))
        elif prop == "twitter:image":
            u = norm_url(final_url, content)
            if u:
                found.append(Candidate(u, "twitter", score_candidate(u, final_url, site_name, "twitter")))

    for js in SCRIPT_JSONLD_RE.findall(html_text):
        for raw in iter_jsonld_images(js):
            u = norm_url(final_url, raw)
            if u:
                found.append(Candidate(u, "jsonld", score_candidate(u, final_url, site_name, "jsonld")))

    for tag in IMG_RE.findall(html_text):
        attrs = parse_attrs(tag)
        raw = attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src") or ""
        u = norm_url(final_url, raw)
        if not u:
            continue
        w = parse_dim(attrs.get("width", ""))
        h = parse_dim(attrs.get("height", ""))
        s = score_candidate(u, final_url, site_name, "img")
        if w and h and (w < 240 or h < 180):
            s -= 25
        found.append(Candidate(u, "img", s))

    for _, raw in CSS_URL_RE.findall(html_text):
        u = norm_url(final_url, raw)
        if u:
            found.append(Candidate(u, "css", score_candidate(u, final_url, site_name, "img") - 2))

    for raw in IMAGE_VAR_RE.findall(html_text):
        u = norm_url(final_url, raw)
        if u:
            found.append(Candidate(u, "var", score_candidate(u, final_url, site_name, "img") + 8))

    # Deduplicate by URL keeping max score
    best: dict[str, Candidate] = {}
    for c in found:
        prev = best.get(c.url)
        if prev is None or c.score > prev.score:
            best[c.url] = c

    ranked = sorted(best.values(), key=lambda c: c.score, reverse=True)
    return ranked[:20]


def identify_size(path: Path) -> tuple[int, int]:
    try:
        out = subprocess.check_output(["magick", "identify", "-format", "%w %h", str(path)], text=True).strip()
        w, h = out.split()
        return int(w), int(h)
    except Exception:
        return 0, 0


def choose_extension(image_url: str, content_type: str) -> str:
    ext = os.path.splitext(urlparse(image_url).path)[1].lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if ext == ".jpeg" else ext
    return EXT_BY_TYPE.get(content_type, ".jpg")


def main() -> int:
    target_csv = Path("non_marker_targets.csv")
    out_dir = Path("non-marker-images")
    out_dir.mkdir(exist_ok=True)

    rows = list(csv.DictReader(target_csv.open(encoding="utf-8")))
    manifest = []

    for r in rows:
        sid = int(r["site_id"])
        site_name = r["site_name"]
        source_url = r["source_url"]
        domain_slug = slug(urlparse(source_url).netloc.replace("www.", "")) or "source"

        status = "error"
        note = ""
        chosen_url = ""
        saved_path = ""
        width = 0
        height = 0

        try:
            final_url, page = fetch_text(source_url)
            cands = extract_candidates(final_url, page, site_name)
            if not cands:
                status = "no_candidates"
                note = "No image candidates found"
            else:
                # Try top candidates until one passes size sanity
                for c in cands[:8]:
                    try:
                        data, ctype, final_img_url = fetch_bytes(c.url)
                    except Exception:
                        continue

                    ext = choose_extension(final_img_url, ctype)
                    fname = f"{sid:02d}_{slug(site_name)}__{domain_slug}{ext}"
                    path = out_dir / fname
                    path.write_bytes(data)
                    w, h = identify_size(path)
                    if w >= 450 and h >= 220:
                        chosen_url = final_img_url
                        saved_path = str(path)
                        width, height = w, h
                        status = "ok"
                        note = f"picked_{c.source}_score_{c.score:.1f}"
                        break
                    # keep as fallback only if nothing better emerges
                    if not chosen_url:
                        chosen_url = final_img_url
                        saved_path = str(path)
                        width, height = w, h
                        status = "small_image"
                        note = f"picked_{c.source}_score_{c.score:.1f}_small"

                if not chosen_url:
                    status = "download_failed"
                    note = "Could not download any candidate"

        except Exception as exc:  # noqa: BLE001
            status = "fetch_failed"
            note = str(exc)

        manifest.append(
            {
                "site_id": sid,
                "site_name": site_name,
                "city": r["city"],
                "source_url": source_url,
                "status": status,
                "selected_image_url": chosen_url,
                "saved_path": saved_path,
                "width": width,
                "height": height,
                "note": note,
            }
        )
        print(f"{sid:02d} {site_name[:40]:40} -> {status}")

    manifest_path = Path("non-marker-images-manifest.csv")
    with manifest_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(manifest[0].keys()))
        w.writeheader()
        w.writerows(manifest)

    ok = sum(1 for x in manifest if x["status"] in {"ok", "small_image"})
    print(f"wrote {manifest_path} | usable={ok}/{len(manifest)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
