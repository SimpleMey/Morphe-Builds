#!/usr/bin/env python3
"""Generate a per-app "Changelog" section for the release notes.

For every app in the release (read from manifest.json), this resolves the patch
source that built it (sources/<source>.json), fetches that patch repository's
release notes from GitHub, and renders one collapsible <details> block per app:

    ## Changelog

    <details>
    <summary><b>RVX App (YouTube)</b>: v21.13.164</summary>

    Patches: anddea/revanced-patches (dev-v...)
    Full changelog: https://github.com/anddea/revanced-patches/releases/tag/...

    <release notes body>

    </details>

The patch notes are shared across apps built from the same source (for example
YouTube, YouTube Music and Reddit on Morphe), so each of those apps shows the
same Morphe patch notes. That is intentional: each app's dropdown is
self-contained.

Writes the section to --out (default: stdout). Exits non-zero if manifest.json is
missing or has no usable entries, so the workflow can fall back to a plain list.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


# Friendly display name per (app_name, source). Falls back to a generated name.
APP_DISPLAY = {
    ("youtube", "morphe"): "YouTube (Morphe)",
    ("youtube-music", "morphe"): "YouTube Music (Morphe)",
    ("reddit", "morphe"): "Reddit (Morphe)",
    ("x-new", "piko-newx"): "X / Twitter (Piko)",
    ("youtube", "revanced-anddea"): "RVX App (YouTube)",
    ("youtube-music", "revanced-anddea"): "RVX Music (YouTube Music)",
}

# Order apps appear in the notes. Anything not listed is appended, sorted.
DISPLAY_ORDER = [
    ("youtube", "revanced-anddea"),
    ("youtube-music", "revanced-anddea"),
    ("youtube", "morphe"),
    ("youtube-music", "morphe"),
    ("reddit", "morphe"),
    ("x-new", "piko-newx"),
]

# CLI repos are not the patch bundle; skip them when picking the patches repo.
_CLI_REPOS = {"morphe-cli", "revanced-cli"}

BODY_LIMIT = 6000  # max characters of upstream notes to embed per app


def gh_api(path: str):
    """Call `gh api <path>` and return parsed JSON, or None on failure."""
    try:
        res = subprocess.run(
            ["gh", "api", path, "-H", "Accept: application/vnd.github+json"],
            capture_output=True, text=True, check=True,
        )
        return json.loads(res.stdout or "null")
    except Exception as e:
        msg = getattr(e, "stderr", "") or str(e)
        print(f"  gh api {path} failed: {str(msg)[:200]}", file=sys.stderr)
        return None


def fetch_release(user: str, repo: str, tag: str):
    """Resolve a source tag to a release object (with body/html_url/tag_name).

    Mirrors src/utils.detect_github_release: 'latest' -> releases/latest,
    'dev'/'prerelease'/'' -> newest matching release, otherwise a tagged release.
    """
    if tag == "latest":
        return gh_api(f"repos/{user}/{repo}/releases/latest")
    if tag in ("", "dev", "prerelease"):
        rels = gh_api(f"repos/{user}/{repo}/releases") or []
        if not isinstance(rels, list) or not rels:
            return None
        if tag == "dev":
            cand = [r for r in rels if "dev" in str(r.get("tag_name", "")).lower()]
        elif tag == "prerelease":
            cand = [r for r in rels if r.get("prerelease")]
        else:
            cand = rels
        if not cand:
            return None
        return max(cand, key=lambda r: r.get("created_at", ""))
    return gh_api(f"repos/{user}/{repo}/releases/tags/{tag}")


def resolve_patch_repo(source: str):
    """Return (user, repo, tag) of the patch bundle for a source, or None."""
    src_path = Path("sources") / f"{source}.json"
    if not src_path.exists():
        return None
    try:
        info = json.loads(src_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(info, list):
        return None
    # Entries after the name header; pick the last non-CLI GitHub repo.
    picked = None
    for entry in info[1:]:
        if not isinstance(entry, dict):
            continue
        repo = (entry.get("repo") or "").strip()
        user = (entry.get("user") or "").strip()
        if not repo or not user:
            continue
        if repo.lower() in _CLI_REPOS:
            continue
        picked = (user, repo, (entry.get("tag") or "latest").strip())
    return picked


def display_name(app: str, source: str) -> str:
    if (app, source) in APP_DISPLAY:
        return APP_DISPLAY[(app, source)]
    pretty_app = app.replace("-", " ").title()
    return f"{pretty_app} ({source})"


def load_apps(manifest_path: Path):
    """Return a dict {(app, source): version} from the manifest."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = data.get("entries", {}) if isinstance(data, dict) else {}
    apps = {}
    for entry in entries.values():
        app = entry.get("app_name", "")
        source = entry.get("source", "")
        if not app or not source:
            continue
        ver = (entry.get("built_version") or entry.get("config_version") or "").strip()
        # Keep the first non-empty version seen for this app/source.
        if (app, source) not in apps or (not apps[(app, source)] and ver):
            apps[(app, source)] = ver
    return apps


def trim_body(body: str, url: str) -> str:
    body = (body or "").strip()
    if not body:
        return "_No changelog provided for this release._"
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT].rstrip()
        if url:
            body += f"\n\n_Changelog truncated. Read the full notes at {url}_"
        else:
            body += "\n\n_Changelog truncated._"
    return body


def render(apps: dict) -> str:
    # Order: known order first, then any extras sorted.
    ordered = [k for k in DISPLAY_ORDER if k in apps]
    ordered += sorted(k for k in apps if k not in DISPLAY_ORDER)

    release_cache = {}
    out = ["## Changelog", ""]
    for (app, source) in ordered:
        version = apps[(app, source)]
        name = display_name(app, source)
        summary_ver = f": v{version}" if version else ""

        repo_info = resolve_patch_repo(source)
        if repo_info:
            user, repo, tag = repo_info
            if (user, repo, tag) not in release_cache:
                release_cache[(user, repo, tag)] = fetch_release(user, repo, tag)
            rel = release_cache[(user, repo, tag)]
        else:
            user = repo = tag = ""
            rel = None

        out.append("<details>")
        out.append(f"<summary><b>{name}</b>{summary_ver}</summary>")
        out.append("")
        if repo_info:
            rel_tag = (rel or {}).get("tag_name", tag)
            html_url = (rel or {}).get("html_url", "")
            out.append(f"Patches: `{user}/{repo}` ({rel_tag})")
            out.append("")
            out.append(trim_body((rel or {}).get("body", ""), html_url))
            if html_url:
                out.append("")
                out.append(f"Full changelog: {html_url}")
        else:
            out.append("_Patch source not found for this app._")
        out.append("")
        out.append("</details>")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the release-notes Changelog section.")
    ap.add_argument("--manifest", default="manifest.json", help="path to manifest.json")
    ap.add_argument("--out", default="-", help="output file, or '-' for stdout")
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    try:
        apps = load_apps(manifest_path)
    except Exception as e:
        print(f"failed to read manifest: {e}", file=sys.stderr)
        return 2

    if not apps:
        print("no apps found in manifest", file=sys.stderr)
        return 2

    section = render(apps)

    if args.out == "-":
        sys.stdout.write(section)
    else:
        Path(args.out).write_text(section, encoding="utf-8")
        print(f"wrote {args.out} ({len(section)} chars) for {len(apps)} app(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
