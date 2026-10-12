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

## Release output (rolling 2-release window)
- Each build publishes its own immutable release, tagged `build-YYYYMMDD-HHMMSS` (UTC), marked as "Latest".
- Every release is a COMPLETE set: the apps rebuilt this run, plus the unchanged apps carried over from the previous release (`scripts/merge_release_dir.py`), plus `manifest.json`.
- Filenames keep the version, e.g. `youtube-arm64-v8a-morphe-v2.5.0.apk`, so the version is visible on the download.
- Only the 2 newest releases are kept. When a 3rd build is published, the oldest release (and its tag and assets) is deleted automatically.
- Why: a build replaces nothing in place, so download links already published elsewhere (VMPro, or the WordPress/Sinker feed) keep working. A link stops working only after a newer build has been live for a full update cycle, which is far longer than the feed takes to refresh. This removes the window where the feed's links 404 during a rebuild.
- Consumers should read the newest release (GitHub `releases/latest`, or the first release that carries `manifest.json`); VMPro already does this, so dated tags do not break it.

## Apps and sources
- Morphe: YouTube, YouTube Music, Reddit (`MorpheApp/morphe-patches`).
- Anddea (RVX): RVX App (YouTube), RVX Music (YT Music) (`anddea/revanced-patches`, dev tag).
- Piko: X / Twitter (`crimera/piko-newx`).
