#!/usr/bin/env python3
"""Generate catalog.json, one CDN-hosted file that bundles the latest-release metadata for
every app VMPro shows.

VMPro used to call the GitHub REST API live on each launch (Morphe-Builds + NewTube + Flow +
GmsCore + MicroG RE + j-hc modules + SmartTube + TizenTube + its own update check). Unauthenticated
that is capped at 60 requests/hour per IP, so users on shared/CGNAT IPs hit HTTP 403.

This script runs server-side with the Actions GITHUB_TOKEN (5000 req/h), a few times a day, and
publishes catalog.json as an asset on the Morphe-Builds `latest` release. The app then resolves
its whole catalog from that single CDN file, with zero GitHub API calls in the normal case. The
app keeps the live API only as a fallback for when catalog.json is unavailable.

The per-repo release objects are stored in GitHub's own shape (a subset of fields), so the app
feeds them straight into its existing release parser and arch/variant resolution unchanged.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Run as `python scripts/build_catalog.py`: add the repo root so `src` is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import utils

logging.basicConfig(level=logging.INFO, format="%(message)s")

# "owner/repo" -> how many releases to include. Matches the app's own per_page so the list the
# app slices from the catalog is identical to what it would have fetched from the API.
REPOS = {
    "SimpleMey/Morphe-Builds": 1,          # the single `latest` release (all Morphe APK assets)
    "aleixrodriala/newtube": 6,            # NewTube (Apps tab)
    "Flow-Tube/Flow": 6,                   # Flow (Apps tab)
    "ReVanced/GmsCore": 15,                # GmsCore (MicroG tab)
    "MorpheApp/MicroG-RE": 15,             # MicroG RE (MicroG tab)
    "j-hc/revanced-magisk-module": 10,     # Magisk/KernelSU modules (Modules tab)
    "yuliskov/SmartTube": 6,               # SmartTube (TV tab)
    "reisxd/TizenTubeCobalt": 6,           # TizenTube Cobalt (TV tab)
    "SimpleMey/VMPro": 5,                  # VMPro itself (in-app update check)
}

# Release notes can be large; the app only needs them to parse a module's patch version, which
# sits near the top. Cap each body so catalog.json stays small.
BODY_CAP = 6000


def _trim_release(r: dict) -> dict:
    return {
        "tag_name": r.get("tag_name", ""),
        "name": r.get("name", ""),
        "published_at": r.get("published_at", ""),
        "html_url": r.get("html_url", ""),
        "prerelease": bool(r.get("prerelease", False)),
        "body": (r.get("body") or "")[:BODY_CAP],
        "assets": [
            {
                "name": a.get("name", ""),
                "size": a.get("size", 0),
                "browser_download_url": a.get("browser_download_url", ""),
                "download_count": a.get("download_count", 0),
            }
            for a in (r.get("assets") or [])
        ],
    }


def _fetch_repo_releases(owner_repo: str, per_page: int) -> list:
    try:
        data = utils.gh_api_request(f"repos/{owner_repo}/releases?per_page={per_page}")
        if isinstance(data, list):
            return [_trim_release(r) for r in data]
        logging.warning(f"catalog: unexpected response shape for {owner_repo}")
    except Exception as e:
        logging.warning(f"catalog: failed to fetch releases for {owner_repo}: {e}")
    return []


def _load_manifest() -> dict:
    """Prefer a manifest.json in the workspace (same job as a build); otherwise read the one
    published on the latest release (this script usually runs in its own job)."""
    local = Path("manifest.json")
    if local.exists():
        try:
            return json.loads(local.read_text())
        except Exception as e:
            logging.warning(f"catalog: local manifest.json unreadable: {e}")
    try:
        return utils.fetch_json(
            "https://github.com/SimpleMey/Morphe-Builds/releases/latest/download/manifest.json"
        )
    except Exception as e:
        logging.warning(f"catalog: manifest.json fallback failed: {e}")
    return {}


def main():
    repos = {}
    for owner_repo, per_page in REPOS.items():
        releases = _fetch_repo_releases(owner_repo, per_page)
        if releases:
            repos[owner_repo] = releases
            logging.info(f"catalog: {owner_repo}: {len(releases)} release(s)")
        else:
            logging.warning(f"catalog: {owner_repo}: no releases (app will fall back to the API)")

    catalog = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "morphe_manifest": _load_manifest(),
        "repos": repos,
    }

    out = Path("catalog.json")
    out.write_text(json.dumps(catalog, separators=(",", ":")))
    logging.info(
        f"catalog: wrote {out.name} ({out.stat().st_size} bytes, {len(repos)} repo(s))"
    )


if __name__ == "__main__":
    main()
