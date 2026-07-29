"""Audit torrent file structures for popular anime releases.

Fetches torrent files from Nyaa for various anime, parses them with our
bdecode + order_episodes pipeline, and reports how each would be handled.
"""
from __future__ import annotations

import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from nekofetch.sources._torrent import torrent_files, order_episodes

ANIME_QUERIES = [
    # Modern popular
    ("Demon Slayer", "Kimetsu no Yaiba"),
    ("Attack on Titan", "Shingeki no Kyojin"),
    ("Jujutsu Kaisen", None),
    ("Spy x Family", None),
    ("Chainsaw Man", None),
    # Classic
    ("Cowboy Bebop", None),
    ("Steins;Gate", "Steins Gate"),
    ("Death Note", None),
    # Short series
    ("Bocchi the Rock", None),
    ("Takopi's Original Sin", "Takopii no Genzai"),
    # Movies
    ("Suzume", None),
    ("Your Name", "Kimi no Na wa"),
]

HEADERS = {"User-Agent": "Mozilla/5.0"}


def search_nyaa(session: requests.Session, query: str) -> list[dict]:
    url = f"https://nyaa.si/?page=rss&q={query}&c=1_2&f=0&s=seeders&o=desc"
    try:
        resp = session.get(url, timeout=30)
    except Exception:
        return []
    if resp.status_code != 200:
        return []
    root = ET.fromstring(resp.text)
    results = []
    ns = {"nyaa": "https://nyaa.si/xmlns/nyaa"}
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        seeders_el = item.find("nyaa:seeders", ns)
        if title_el is None or link_el is None:
            continue
        title = title_el.text or ""
        seeders = int(seeders_el.text) if seeders_el is not None and seeders_el.text else 0
        low = title.lower()
        if any(k in low for k in ("batch", "complete", "bd", "blu-ray", "bdrip")):
            link_text = link_el.text or ""
            if "/download/" in link_text:
                torrent_link = link_text
            else:
                torrent_link = link_text.replace("/view/", "/download/") + ".torrent"
            results.append({"title": title, "torrent_url": torrent_link, "seeders": seeders})
    return results[:3]


def fetch_and_parse(session: requests.Session, torrent_url: str) -> tuple[str, list[dict]] | None:
    try:
        resp = session.get(torrent_url, timeout=30, allow_redirects=True)
        if resp.status_code != 200:
            return None
        name, files = torrent_files(resp.content)
        ordered = order_episodes(files)
        return name, ordered
    except Exception:
        return None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    with requests.Session() as session:
        session.headers.update(HEADERS)
        for anime_name, alt_query in ANIME_QUERIES:
            print(f"\n{'='*80}")
            print(f"ANIME: {anime_name}")
            print(f"{'='*80}")

            results = search_nyaa(session, alt_query or anime_name)
            if not results:
                results = search_nyaa(session, anime_name)
            if not results:
                print("  No batch torrents found on Nyaa.")
                continue

            # Pick top-seeded result
            best = results[0]
            print(f"  Release: {best['title']}")
            print(f"  Seeders: {best['seeders']}")

            parsed = fetch_and_parse(session, best["torrent_url"])
            if parsed is None:
                print("  FAILED to fetch/parse torrent")
                continue

            name, ordered = parsed
            print(f"  Torrent name: {name}")
            print(f"  Total video files: {len(ordered)}")

            main_eps = [e for e in ordered if e["kind"] == "episode"]
            movies = [e for e in ordered if e["kind"] == "movie"]
            specials = [e for e in ordered if e["kind"] in ("special", "ova")]
            extras = [e for e in ordered if e["kind"] == "extra"]

            print(f"  Main episodes: {len(main_eps)}")
            print(f"  Movies: {len(movies)}")
            print(f"  Specials/OVAs: {len(specials)}")
            print(f"  Extras (NCOP/NCED/etc): {len(extras)}")

            if main_eps:
                eps = [e.get("episode") for e in main_eps]
                print(f"  Episode numbers detected: {eps}")
                seasons = sorted(set(e["season"] for e in main_eps))
                print(f"  Seasons detected: {seasons}")

            # Check for potential issues
            issues = []
            if any(e.get("episode") is None for e in main_eps):
                bad = [e["name"] for e in main_eps if e.get("episode") is None]
                issues.append(f"Episode number NOT detected for: {bad[:3]}")

            if main_eps:
                ep_nums = [e["episode"] for e in main_eps if e["episode"] is not None]
                if ep_nums and ep_nums != sorted(ep_nums):
                    issues.append(f"Episodes NOT in order: {ep_nums[:10]}")

            # Check filename patterns
            if ordered:
                print(f"\n  Sample files:")
                for e in ordered[:5]:
                    print(f"    [{e['kind']:>8}] S{e['season']:02d} "
                          f"{'E'+str(e['episode']):>5} "
                          f"{'['+e['resolution']+']' if e.get('resolution') else '[?????]':>8} "
                          f"{e['name'][:70]}")
                if len(ordered) > 5:
                    print(f"    ... and {len(ordered) - 5} more files")

                # Show extras separately
                if extras:
                    print(f"\n  Extras that would be SKIPPED (with new filter):")
                    for e in extras:
                        print(f"    [{e['kind']:>8}] {e['name'][:70]}")

            if issues:
                print(f"\n  ⚠️ ISSUES:")
                for iss in issues:
                    print(f"    - {iss}")
            else:
                print(f"\n  ✅ No issues detected")

            time.sleep(1)  # be nice to Nyaa


if __name__ == "__main__":
    main()
