import json
import logging
import time
from pathlib import Path
from src import (
    utils,
    apkpure,
    session,
    uptodown,
    aptoide,
    apkmirror,
    github,
    apkcombo,
)

def download_resource(url: str, name: str = None) -> Path:
    res = session.get(url, stream=True)
    res.raise_for_status()
    final_url = res.url

    if not name:
        name = utils.extract_filename(res, fallback_url=final_url)

    filepath = Path(name)
    total_size = int(res.headers.get('content-length', 0))
    downloaded_size = 0

    with filepath.open("wb") as file:
        for chunk in res.iter_content(chunk_size=8192):
            if chunk:
                file.write(chunk)
                downloaded_size += len(chunk)

    logging.info(
        f"URL: {final_url} [{downloaded_size}/{total_size}] -> \"{filepath}\" [1]"
    )

    return filepath

def download_required(source: str) -> tuple[list[Path], str]:
    source_path = Path("sources") / f"{source}.json"
    with source_path.open() as json_file:
        repos_info = json.load(json_file)

    # Handle bundle format
    if isinstance(repos_info, dict) and "bundle_url" in repos_info:
        return download_from_bundle(repos_info)
    
    # Handle old list format
    name = repos_info[0]["name"]
    downloaded_files = []

    for repo_info in repos_info[1:]:
        release = utils.detect_release(repo_info)
        entry_name = (
            repo_info.get("repo")
            or repo_info.get("project")
            or repo_info.get("name")
            or ""
        ).lower()

        for asset in release["assets"]:
            asset_name = asset["name"]
            asset_url = asset["browser_download_url"]
            if asset_name.endswith(".asc"):
                continue

            # Keep the existing Morphe-specific asset filtering.
            if "morphe-patches" in entry_name or "morphe-cli" in entry_name:
                if asset_name.endswith(".mpp") or (
                    asset_name.lower().endswith(".jar")
                ):
                    downloaded_files.append(download_resource(asset_url))
            else:
                downloaded_files.append(download_resource(asset_url))

    return downloaded_files, name

def download_from_bundle(bundle_info: dict) -> tuple[list[Path], str]:
    """Download resources from a bundle URL"""
    bundle_url = bundle_info["bundle_url"]
    name = bundle_info.get("name", "bundle-patches")
    
    logging.info(f"Downloading bundle from {bundle_url}")
    
    # Download the bundle JSON
    with session.get(bundle_url) as res:
        res.raise_for_status()
        bundle_data = res.json()
    
    downloaded_files = []
    
    # Check API version and structure
    if "patches" in bundle_data:
        # API v4 format
        patches = bundle_data.get("patches", [])
        integrations = bundle_data.get("integrations", [])
        
        # Download patches (JAR files)
        for patch in patches:
            if "url" in patch:
                filepath = download_resource(patch["url"])
                downloaded_files.append(filepath)
                logging.info(f"Downloaded patch: {patch.get('name', 'unknown')}")
        
        # Download integrations (APK files)
        for integration in integrations:
            if "url" in integration:
                filepath = download_resource(integration["url"])
                downloaded_files.append(filepath)
                logging.info(f"Downloaded integration: {integration.get('name', 'unknown')}")
    
    # Also download CLI (still needed) - try ReVanced CLI first
    try:
        cli_release = utils.detect_github_release("revanced", "revanced-cli", "latest")
        for asset in cli_release["assets"]:
            if asset["name"].endswith(".asc"):
                continue
            if asset["name"].endswith(".jar") and "cli" in asset["name"].lower():
                filepath = download_resource(asset["browser_download_url"])
                downloaded_files.append(filepath)
                logging.info("Downloaded ReVanced CLI")
                break
    except Exception as e:
        logging.warning(f"Could not download ReVanced CLI: {e}")
    
    return downloaded_files, name

def download_platform(
    app_name: str,
    platform: str,
    cli: str,
    patches: str,
    arch: str = None,
    override_version: str = None,
) -> tuple[Path | None, str | None, list[str]]:
    try:
        config_path = Path("apps") / platform / f"{app_name}.json"
        config = None
        if config_path.exists():
            with config_path.open() as json_file:
                config = json.load(json_file)
        else:
            # Fallback: search other platform config directories for this app
            for other_platform in ["apkmirror", "uptodown", "apkpure", "aptoide", "github", "apkcombo"]:
                if other_platform == platform:
                    continue
                other_path = Path("apps") / other_platform / f"{app_name}.json"
                if other_path.exists():
                    try:
                        with other_path.open() as json_file:
                            other_cfg = json.load(json_file)
                        if other_cfg.get("package"):
                            config = {
                                "name": other_cfg.get("name", app_name),
                                "package": other_cfg["package"],
                                "version": other_cfg.get("version", ""),
                                "arch": other_cfg.get("arch", "universal"),
                                "type": other_cfg.get("type", "APK"),
                                "dpi": other_cfg.get("dpi", "nodpi"),
                                "org": other_cfg.get("org", app_name)
                            }
                            logging.info(f"Synthesized {platform} config for {app_name} from {other_platform}")
                            break
                    except Exception:
                        continue

        if not config or not config.get("package"):
            raise FileNotFoundError(f"Config file not found for {app_name} on {platform}")
        
        # Override arch only if explicitly specified non-universal, or if config has no arch set
        if arch and arch != "universal":
            config['arch'] = arch
        elif 'arch' not in config or not config['arch']:
            config['arch'] = arch or "universal"

        platform_module = globals()[platform]

        # Candidate versions (highest -> lowest) for universal robustness:
        # - If config pins a version: only try that.
        # - Else if override provided (retry path): try only that.
        # - Else ask the patching CLI for compatible versions and try those.
        # - If none returned: fall back to latest available from the store.
        pinned = (config.get("version") or "").strip()
        if override_version:
            candidates = [override_version]
        elif pinned:
            candidates = [pinned]
        else:
            candidates = utils.get_supported_versions(config["package"], cli, patches)
            try:
                latest = platform_module.get_latest_version(app_name, config)
                if latest and latest not in candidates:
                    candidates.append(latest)
            except Exception as e:
                logging.debug(f"Could not get latest version for {app_name} on {platform}: {e}")

        last_error: Exception | None = None
        for version in candidates:
            if not version:
                continue
            download_link = platform_module.get_download_link(version, app_name, config)
            if not download_link:
                last_error = ValueError(f"No download link found for {app_name} version {version}")
                continue
            try:
                filepath = download_resource(download_link)
                return filepath, version, candidates
            except Exception as e:
                last_error = e
                continue

        raise last_error or ValueError(f"No downloadable versions found for {app_name} on {platform}")

    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return None, None, []

# Update the specific download functions
def download_apkmirror(
    app_name: str,
    cli: str,
    patches: str,
    arch: str = None,
    override_version: str = None,
) -> tuple[Path | None, str | None, list[str]]:
    return download_platform(app_name, "apkmirror", cli, patches, arch, override_version)

def download_github(
    app_name: str,
    cli: str,
    patches: str,
    arch: str = None,
    override_version: str = None,
) -> tuple[Path | None, str | None, list[str]]:
    return download_platform(app_name, "github", cli, patches, arch, override_version)

def download_apkpure(
    app_name: str,
    cli: str,
    patches: str,
    arch: str = None,
    override_version: str = None,
) -> tuple[Path | None, str | None, list[str]]:
    return download_platform(app_name, "apkpure", cli, patches, arch, override_version)

def download_aptoide(
    app_name: str,
    cli: str,
    patches: str,
    arch: str = None,
    override_version: str = None,
) -> tuple[Path | None, str | None, list[str]]:
    return download_platform(app_name, "aptoide", cli, patches, arch, override_version)

def download_uptodown(
    app_name: str,
    cli: str,
    patches: str,
    arch: str = None,
    override_version: str = None,
) -> tuple[Path | None, str | None, list[str]]:
    return download_platform(app_name, "uptodown", cli, patches, arch, override_version)

def download_apkcombo(
    app_name: str,
    cli: str,
    patches: str,
    arch: str = None,
    override_version: str = None,
) -> tuple[Path | None, str | None, list[str]]:
    return download_platform(app_name, "apkcombo", cli, patches, arch, override_version)

# Provider order, highest-priority first. Each is tried for the preferred version
# before any lower version is attempted.
_PROVIDER_ORDER = ["apkmirror", "aptoide", "github", "uptodown", "apkpure", "apkcombo"]


def _first_app_config(app_name: str) -> dict | None:
    """First available apps/<platform>/<app>.json config (for package + version)."""
    for platform in _PROVIDER_ORDER:
        p = Path("apps") / platform / f"{app_name}.json"
        if p.exists():
            try:
                with p.open() as f:
                    return json.load(f)
            except Exception:
                continue
    return None


def download_best(
    app_name: str,
    cli: str,
    patches: str,
    arch: str = None,
    override_version: str = None,
) -> tuple[Path | None, str | None, list[str]]:
    """Download the APK for (app, patches), preferring the newest supported version
    across ALL providers before downgrading.

    This iterates versions as the OUTER loop and providers as the inner loop, so a
    mirror that is missing the preferred version cannot silently downgrade the
    build: every provider is tried for a given version first, and only if none of
    them has it do we move on to the next (lower) supported version. If nothing in
    the supported list is downloadable anywhere, it falls back to the per-provider
    path (which also considers each store's own "latest").
    """
    cfg = _first_app_config(app_name)
    package = (cfg or {}).get("package") or ""
    pinned = ((cfg or {}).get("version") or "").strip()

    if override_version:
        versions = [override_version]
    elif pinned:
        versions = [pinned]
    elif package:
        versions = utils.get_supported_versions(package, cli, patches)
    else:
        versions = []

    for version in versions:
        if not version:
            continue
        for platform in _PROVIDER_ORDER:
            input_apk, got_ver, _ = download_platform(
                app_name, platform, cli, patches, arch, override_version=version
            )
            if input_apk:
                logging.info(f"✅ Got {app_name} {got_ver} from {platform}")
                return input_apk, got_ver, versions

    # Nothing from the supported list downloaded anywhere: fall back to the
    # per-provider path (each provider also tries its store's latest version).
    for platform in _PROVIDER_ORDER:
        input_apk, got_ver, cands = download_platform(
            app_name, platform, cli, patches, arch, override_version=override_version
        )
        if input_apk:
            logging.info(f"✅ Got {app_name} {got_ver} from {platform} (fallback)")
            return input_apk, got_ver, (versions or cands)

    return None, None, versions


def download_apkeditor() -> Path:
    max_retries = 3
    for attempt in range(max_retries):
        try:
            release = utils.detect_github_release("REAndroid", "APKEditor", "latest")

            for asset in release["assets"]:
                if asset["name"].startswith("APKEditor") and asset["name"].endswith(".jar"):
                    return download_resource(asset["browser_download_url"])

            raise RuntimeError("APKEditor .jar file not found in the latest release")
        except Exception as e:
            if attempt == max_retries - 1:
                raise RuntimeError(f"Failed to download APKEditor after {max_retries} attempts: {e}")
            logging.warning(f"APKEditor download attempt {attempt + 1} failed: {e}. Retrying...")
            time.sleep(2)  # Wait 2 seconds before retry
