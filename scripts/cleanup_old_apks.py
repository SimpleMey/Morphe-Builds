#!/usr/bin/env python3
"""Delete superseded (older-version) APK assets from the 'latest' release.

Problem this solves
-------------------
APK filenames embed the app version, e.g.::

    youtube-arm64-v8a-morphe-v2.5.0.apk

When a new version is built and uploaded, the *old* APK
(``youtube-arm64-v8a-morphe-v2.4.0.apk``) was never removed, because the previous
workflow step only deleted assets whose filename matched a *new* APK exactly
(i.e. it could only overwrite identical names). Old versions therefore
accumulated indefinitely.

Strategy
--------
Group assets by *identity prefix* ``{app}-{arch}-`` (everything before
``-v{version}.apk``) and, within each group, keep only the newest ``--keep-versions``
versions (default 2 = current + previous); delete the rest. This is identity-based,
so e.g. v2.6 and v2.5 are kept while v2.4 is pruned.

Run this AFTER the build uploads the new APK, so the fresh version is counted and
the PREVIOUS version is retained until the NEXT update supersedes it. That overlap
keeps an already-published download link (VMPro, or the WordPress/Sinker feed)
working across a rebuild instead of 404ing. The ``manifest.json`` still points at the
newest version, so the retained older APK is just an orphan download for the window.

Inputs
------
- ``--keep-file``: a newline-delimited file of APK basenames to PRESERVE (this run's
  freshly built APKs). Anything listed here is never deleted and counts as newest.
- ``--keep-versions``: how many newest versions to keep per identity (default 2).
- ``--release``: release tag to clean (default ``latest``).

The current APK asset names are read via ``gh release view --json assets`` so we
never need to download the heavy APK files.

Safety
------
- Only ``.apk`` assets are ever considered for deletion.
- Anything in the keep-set is always preserved.
- Every deletion is best-effort (logged + non-fatal).
- A ``--dry-run`` flag prints what would be deleted without deleting.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Set

# APK names look like: {app}-{arch}-{name}-v{version}.apk. The identity is
# everything before the trailing "-v{version}.apk". The version can contain
# letters (e.g. piko's "-v12.30.0-prod.01"), so the version part allows word
# characters; the greedy ".*" prefix binds the match to the LAST "-v<digit>"
# token, which keeps arch tokens like "v8a"/"v7a" inside the identity (there is a
# later "-v<version>" after them) instead of truncating there.
VERSION_MARKER = re.compile(r"^(?P<id>.*)-v(?P<ver>\d[\w.+()\-]*)\.apk$", re.IGNORECASE)


def gh_release_assets(release: str) -> List[dict]:
    """Return asset dicts (with 'name' + 'id') currently attached to the release.
    Only APK assets are returned."""
    try:
        result = subprocess.run(
            ["gh", "release", "view", release, "--json", "assets"],
            capture_output=True, text=True, check=True,
        )
        assets = json.loads(result.stdout or "{}").get("assets", []) or []
        return [a for a in assets if isinstance(a, dict)
                and str(a.get("name", "")).endswith(".apk")]
    except Exception as e:
        print(f"⚠️  could not list release assets: {e}", file=sys.stderr)
        return []


def identity_prefix(apk_name: str) -> str:
    """Return the identity prefix of an APK filename (everything before the
    version marker). E.g. ``youtube-arm64-v8a-morphe-v2.5.0.apk`` ->
    ``youtube-arm64-v8a-morphe``. Falls back to the stem if no marker matches."""
    m = VERSION_MARKER.match(apk_name)
    return (m.group("id") if m else Path(apk_name).stem).lower()


def version_key(apk_name: str) -> tuple:
    """Sortable version key for an APK (higher is newer). Uses the integer groups
    of the version token, e.g. "12.30.0-prod.01" -> (12, 30, 0, 1). Names with no
    recognisable version sort lowest."""
    m = VERSION_MARKER.match(apk_name)
    if not m:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", m.group("ver")))


def load_keep_set(keep_file: Path) -> Set[str]:
    if not keep_file or not keep_file.exists():
        return set()
    names = set()
    for line in keep_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            names.add(line)
    return names


def delete_asset_by_name(release: str, name: str) -> tuple:
    """Delete a single asset by name using `gh release delete-asset`.
    NOTE: `gh release delete <tag> <asset>` deletes the WHOLE release, not the
    asset — that was the original bug. The correct subcommand is `delete-asset`.
    Returns (ok, message)."""
    try:
        result = subprocess.run(
            ["gh", "release", "delete-asset", release, name, "--yes"],
            capture_output=True, text=True, check=True,
        )
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or e.stdout or str(e)).strip()
    except Exception as e:
        return False, str(e)


def delete_asset_by_id(name: str, asset_id: str) -> tuple:
    """Fallback: delete an asset by its numeric ID via the REST API.
    Used when `gh release delete-asset` is unavailable (older gh) or fails.
    Returns (ok, message)."""
    repo = _repo_slug()
    if not repo:
        return False, "GITHUB_REPOSITORY not set for API fallback"
    try:
        result = subprocess.run(
            ["gh", "api", "-X", "DELETE",
             f"repos/{repo}/releases/assets/{asset_id}"],
            capture_output=True, text=True, check=True,
        )
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or e.stdout or str(e)).strip()
    except Exception as e:
        return False, str(e)


def _repo_slug() -> str:
    """Return 'owner/name' from the GITHUB_REPOSITORY env var, or ''."""
    import os
    return (os.environ.get("GITHUB_REPOSITORY") or "").strip()


def delete_asset(release: str, name: str, asset_id: str = "") -> bool:
    """Delete a single release asset. Tries `gh release delete-asset` first,
    falls back to the REST API (by asset id) on failure. Returns True on
    success. Every failure is logged to stderr but never fatal."""
    ok, msg = delete_asset_by_name(release, name)
    if ok:
        return True
    print(f"  ⚠️  delete-asset failed for {name}: {msg[:200]}", file=sys.stderr)

    if asset_id:
        ok2, msg2 = delete_asset_by_id(name, asset_id)
        if ok2:
            return True
        print(f"  ⚠️  API fallback failed for {name} (id={asset_id}): {msg2[:200]}",
              file=sys.stderr)
    else:
        print(f"  ⚠️  no asset id available for {name}; API fallback skipped",
              file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete superseded APK assets from a release.")
    parser.add_argument("--release", default="latest", help="release tag (default: latest)")
    parser.add_argument("--keep-file", required=True,
                        help="newline-delimited file of APK basenames to preserve")
    parser.add_argument("--retire", action="append", default=[],
                        help="regex (case-insensitive) matched against asset names; any APK "
                             "matching a retire pattern and not in the keep-set is deleted, "
                             "regardless of its identity prefix. Use this for permanently "
                             "renamed/retired naming schemes (e.g. 'revanced-anddea' after it "
                             "was renamed to 'rvx-app'/'rvx-music'). Repeatable.")
    parser.add_argument("--keep-versions", type=int, default=2,
                        help="number of newest versions to keep per app/arch identity "
                             "(default: 2 -> current + previous, so an already-published "
                             "download link survives one update cycle before it is pruned).")
    parser.add_argument("--dry-run", action="store_true", help="list deletions without performing them")
    args = parser.parse_args()

    keep = load_keep_set(Path(args.keep_file))
    assets = gh_release_assets(args.release)  # list of {name, id, ...} dicts
    keep_versions = max(1, args.keep_versions)

    if not assets:
        print("No existing APK assets to clean up.")
        return 0

    # Compile retire patterns (permanently-retired naming schemes).
    retire_patterns = []
    for pat in args.retire:
        try:
            retire_patterns.append(re.compile(pat, re.IGNORECASE))
        except re.error as e:
            print(f"⚠️  ignoring invalid --retire pattern {pat!r}: {e}", file=sys.stderr)

    to_delete = []  # list of asset dicts
    already: set = set()

    # 1. Retire pass: permanently-renamed/retired naming schemes are always removed
    #    (unless explicitly kept), regardless of version.
    for asset in assets:
        name = str(asset.get("name", ""))
        if not name or name in keep or name in already:
            continue
        if any(p.search(name) for p in retire_patterns):
            to_delete.append(asset)
            already.add(name)

    # 2. Keep the newest `keep_versions` versions of each app/arch identity; delete
    #    older ones. This runs AFTER the build's upload, so the freshly built version
    #    is counted and the PREVIOUS version is retained until the NEXT update
    #    supersedes it. That overlap keeps already-published download links (VMPro,
    #    the WordPress/Sinker feed) alive across a rebuild instead of 404ing. The
    #    manifest still points at the newest version, so the older APK is just an
    #    orphan download for the overlap window. Keep-set members (this run's APKs)
    #    are always preserved and counted toward the kept versions.
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for asset in assets:
        name = str(asset.get("name", ""))
        if name:
            groups[identity_prefix(name)].append(asset)
    for ident, grp in groups.items():
        # Newest first; keep-set members sort ahead of everything so they survive.
        ordered = sorted(
            grp,
            key=lambda a: (str(a.get("name", "")) in keep, version_key(str(a.get("name", "")))),
            reverse=True,
        )
        survivors = 0
        for asset in ordered:
            name = str(asset.get("name", ""))
            if name in already:
                continue  # already retired above
            if survivors < keep_versions or name in keep:
                survivors += 1
                continue
            to_delete.append(asset)
            already.add(name)
            print(f"  prune {name}: keeping newest {keep_versions} of {ident}")

    if not to_delete:
        print("No superseded APK assets found.")
        return 0

    print(f"Found {len(to_delete)} superseded APK asset(s) to remove:")
    deleted = 0
    for asset in to_delete:
        name = str(asset.get("name", ""))
        asset_id = str(asset.get("id") or asset.get("apiUri", "")).strip()
        # If gh gave us a URL instead of a bare id, extract the trailing number.
        if asset_id and "/" in asset_id:
            asset_id = asset_id.rstrip("/").rsplit("/", 1)[-1]
        if args.dry_run:
            print(f"  [dry-run] would delete: {name}")
        else:
            if delete_asset(args.release, name, asset_id):
                print(f"  🗑️  deleted: {name}")
                deleted += 1

    action = "would delete" if args.dry_run else "deleted"
    print(f"Done. {action} {len(to_delete) if args.dry_run else deleted} superseded asset(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
