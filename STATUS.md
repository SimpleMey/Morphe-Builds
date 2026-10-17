# Morphe-Builds status

## Auto-build schedule
- Workflow: "Auto Build and Release Morphe" (`.github/workflows/patch.yml`).
- Runs daily at **03:17 UTC = 08:47 IST**. Cron: `17 3 * * *`.

### Why this time
- GitHub delays scheduled workflows most at the top of the hour, so we use an off-peak minute (`:17`), not `:00`.
- GitHub Actions congestion follows developer and CI activity, which is concentrated in US and European working hours, not world population. From about 01:00 to 07:00 UTC both the US and EU are off-hours, so the runner queue is short and the job starts close to on time. 03:17 UTC sits in that window.
- Scheduled runs are still best-effort and can be delayed during high GitHub load. This timing only minimizes the delay, it does not guarantee an exact start.
- The old schedule (`0 6 * * *`, 06:00 UTC) used `:00` and a busy slot, which is why runs kept drifting to around 12:30 UTC.

## How builds work (incremental)
- Each run first checks for updates (`scripts/check_app_updates.py`). An app is rebuilt only when its patch source changes (a new release, a changed release asset, or a new default-branch commit) or its app version changes.
- If nothing upstream changed, the run still succeeds but builds nothing and publishes no new assets. So there is not a new release every day, only when an upstream app or patch set actually updates.
- To force a full rebuild: Actions tab, run the workflow with `force_full_rebuild = true`.

## Version selection
- App versions are not pinned. The builder asks the patches which versions they support and downloads the newest supported version it can fetch, trying every mirror for that version before considering a lower one. A single mirror missing the newest version no longer downgrades the build.

## Release output
- All APKs plus `manifest.json` are published to the single `latest` release. Filenames keep the version, e.g. `youtube-arm64-v8a-morphe-v2.5.0.apk`.
- Cleanup keeps the newest 2 versions of each app and architecture (current + previous); older ones are deleted. The cleanup runs after the upload, so when an app updates, its previous version stays until the NEXT update replaces it.
- Why keep 2: a download link already published elsewhere (VMPro, or the WordPress/Sinker feed) keeps working across a rebuild instead of 404ing. The link to the previous version survives one full update cycle, which is far longer than the feed takes to refresh. The `manifest.json` always points at the newest version, so VMPro and the changelog show the newest; the retained older APK is just an extra download for the overlap window.
- Only an app that updates twice before the feed refreshes could expose a stale link; raise `--keep-versions` in the workflow if more overlap is ever needed.

## Apps and sources
- Morphe: YouTube, YouTube Music, Reddit (`MorpheApp/morphe-patches`).
- Anddea (RVX): RVX App (YouTube), RVX Music (YT Music) (`anddea/revanced-patches`, dev tag).
- Piko: X / Twitter (`crimera/piko-newx`).
- Reseam: YT Reseam (YouTube), from the Reseam toolchain on Gitea (`git.reseam.app/reseam/patches` bundle + `reseam/reseam` CLI, downloaded per build). It is its own patcher (native `reseam` CLI + signed `.reseam` bundle), not morphe/revanced: own patch path in `src/__main__.py`, output `yt-reseam-v<ver>.apk`, package `app.reseam.android.youtube`, GmsCore vendor `app.revanced`, signed with the one fixed RVX key (BKS converted to pk8/der at build time); signing is mandatory with no fallback, so the build fails rather than ever using a throwaway key, keeping updates seamless. Trust is pinned to Reseam's bundle signer.
