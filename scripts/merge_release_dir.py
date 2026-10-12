#!/usr/bin/env python3
"""Carry forward unchanged APKs from the previous release into the new release dir.

Why
---
Builds are incremental: a run only rebuilds the apps whose patch source or version
changed. Each GitHub release, however, is an independent immutable snapshot (we keep
the two newest and delete older ones), so every release must contain the COMPLETE set
of current APKs - not just the ones rebuilt this run. Otherwise the newest release
(which VMPro and the WordPress/Sinker feed both read) would be missing every app that
did not change this run.

What
----
For each APK in --prev-dir (downloaded from the previous release), copy it into
--new-dir unless --new-dir already holds an APK with the same {app}-{arch} identity
(a freshly built, newer version), which always wins. Identity matching is shared with
cleanup_old_apks.identity_prefix so versioned filenames like
``youtube-arm64-v8a-morphe-v2.5.0.apk`` collapse to ``youtube-arm64-v8a-morphe``.
"""
import argparse
import shutil
from pathlib import Path

from cleanup_old_apks import identity_prefix  # single source of identity logic


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--new-dir", required=True, help="freshly built APKs (winners)")
    ap.add_argument("--prev-dir", required=True, help="APKs from the previous release")
    args = ap.parse_args()

    new_dir = Path(args.new_dir)
    prev_dir = Path(args.prev_dir)
    new_dir.mkdir(parents=True, exist_ok=True)

    if not prev_dir.exists():
        print(f"No previous dir {prev_dir}; nothing to carry over.")
        return 0

    new_ids = {identity_prefix(p.name) for p in new_dir.glob("*.apk")}
    carried = 0
    for apk in sorted(prev_dir.glob("*.apk")):
        ident = identity_prefix(apk.name)
        if ident in new_ids:
            print(f"  skip {apk.name}: rebuilt this run ({ident})")
            continue
        dest = new_dir / apk.name
        if dest.exists():
            continue
        shutil.copy2(apk, dest)
        carried += 1
        print(f"  carry {apk.name}")

    print(f"Carried over {carried} unchanged APK(s) from the previous release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
