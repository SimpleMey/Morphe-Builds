import json
import logging
import re
import os
import shutil
from sys import exit
from pathlib import Path
from os import getenv
import subprocess
from src import (
    r2,
    utils,
    release,
    downloader
)

def _should_retry_with_older_version(output: str | None) -> bool:
    """Detect common patterns that indicate the chosen app version is not
    actually compatible with the selected patches (fingerprint mismatch, etc.)."""
    if not output:
        return False
    t = output.lower()
    return (
        "failed to match the fingerprint" in t
        or "patch.patchexception" in t
        or ("fingerprint" in t and "failed" in t)
        or "patching aborted" in t
    )

# --- Reseam (git.reseam.app) ------------------------------------------------
# Reseam is a separate patching ecosystem: a native `reseam` CLI applies a signed
# `.reseam` bundle, with its own command, trust model and (pk8/der) signing. Kept
# fully self-contained so it never touches the morphe/revanced code path.

# Official Reseam patches signer, read from their signed bundle and pinned here so we
# only ever apply bundles actually signed by Reseam.
RESEAM_SIGNER = "556c1b22f4e03398212ba10fe6a31db252df49f599b938d24a9f2b6baec41a1d"

# Stock package each app's bundle patches.
_RESEAM_PACKAGE = {
    "youtube": "com.google.android.youtube",
    "youtube-music": "com.google.android.apps.youtube.music",
}

# Output filename stem per app (youtube -> yt-reseam-v<ver>.apk).
_RESEAM_NAME = {
    "youtube": "yt-reseam",
    "youtube-music": "ytmusic-reseam",
}

# Patch options per app. GmsCore support is on in the recommended preset; it renames
# the package (installs alongside the stock app) and signs in through the app.revanced
# GmsCore the manager already ships.
_RESEAM_OPTIONS = {
    "youtube": [
        "--option", "GmsCore support.gmsCoreVendorGroupId=app.revanced",
        "--option", "GmsCore support.packageName=app.reseam.android.youtube",
    ],
}


def _reseam_find_cli(files: "list[Path]"):
    for f in files:
        if f.name == "reseam-linux-x64" or (f.name.lower().startswith("reseam") and f.suffix == ""):
            return f
    return None


def _reseam_version_key(v: str) -> tuple:
    import re
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def _reseam_supported_versions(cli: "Path", bundle: "Path", package: str) -> "list[str]":
    """Versions the bundle supports for `package`, newest first (reseam bundle list --json)."""
    import json
    try:
        out = subprocess.run(
            [str(cli), "bundle", "list", str(bundle), "--json"],
            capture_output=True, text=True, check=True,
        ).stdout
        data = json.loads(out)
    except Exception as e:
        logging.warning(f"reseam bundle list failed: {e}")
        return []
    versions = set()
    for patch in data.get("patches", []) or []:
        compat = patch.get("compatibility") or {}
        for pkg in compat.get("packages", []) or []:
            if pkg.get("package") == package:
                for v in pkg.get("versions", []) or []:
                    if v:
                        versions.add(str(v))
    return sorted(versions, key=_reseam_version_key, reverse=True)


def _reseam_signing_args() -> "list[str]":
    """Convert the RVX BKS keystore to pk8/der for `reseam patch --key/--cert`.

    Every YT Reseam build MUST be signed with this one fixed key so users update in
    place. This is mandatory and has no fallback: if the key is missing or cannot be
    converted, we raise and the build fails, rather than ever signing with reseam's
    own throwaway key (which would change the signature and break seamless updates)."""
    ks = getenv("RVX_KEYSTORE_PATH")
    if not ks or not Path(ks).exists():
        raise RuntimeError(
            "reseam signing: RVX keystore not available (set RVX_KEYSTORE_BASE64 / "
            "RVX_KEYSTORE_PATH). Refusing to build without the fixed signing key."
        )
    pwd = getenv("RVX_KEYSTORE_PASSWORD", "")
    alias = getenv("RVX_KEY_ALIAS", "")
    keypass = getenv("RVX_KEY_PASSWORD", "") or pwd

    bcprov = Path("bcprov.jar")
    if not bcprov.exists():
        downloader.download_resource(
            "https://repo1.maven.org/maven2/org/bouncycastle/bcprov-jdk18on/1.78.1/bcprov-jdk18on-1.78.1.jar",
            "bcprov.jar",
        )
    p12 = Path("rvx.p12")
    p12.unlink(missing_ok=True)
    # BKS -> PKCS12 (keytool needs the BouncyCastle provider to read BKS). No try/except:
    # any failure propagates and fails the build (no throwaway-key fallback).
    subprocess.run([
        "keytool", "-importkeystore", "-noprompt",
        "-srckeystore", ks, "-srcstoretype", "BKS",
        "-srcstorepass", pwd, "-srcalias", alias, "-srckeypass", keypass,
        "-destkeystore", str(p12), "-deststoretype", "PKCS12",
        "-deststorepass", pwd, "-destkeypass", pwd,
        "-provider", "org.bouncycastle.jce.provider.BouncyCastleProvider",
        "-providerpath", str(bcprov),
    ], check=True, capture_output=True, text=True)
    # PKCS12 -> pk8 (PKCS8 DER key) + der (DER cert).
    subprocess.run(["openssl", "pkcs12", "-in", str(p12), "-passin", f"pass:{pwd}",
                    "-nodes", "-nocerts", "-out", "rvx-key.pem"], check=True, capture_output=True, text=True)
    subprocess.run(["openssl", "pkcs12", "-in", str(p12), "-passin", f"pass:{pwd}",
                    "-nokeys", "-clcerts", "-out", "rvx-cert.pem"], check=True, capture_output=True, text=True)
    subprocess.run(["openssl", "pkcs8", "-topk8", "-nocrypt", "-in", "rvx-key.pem",
                    "-outform", "DER", "-out", "rvx.pk8"], check=True, capture_output=True, text=True)
    subprocess.run(["openssl", "x509", "-in", "rvx-cert.pem", "-outform", "DER",
                    "-out", "rvx.der"], check=True, capture_output=True, text=True)
    if not (Path("rvx.pk8").exists() and Path("rvx.der").exists()):
        raise RuntimeError("reseam signing: key conversion produced no pk8/der; refusing to sign.")
    logging.info("Converted RVX BKS keystore to pk8/der for reseam signing.")
    return ["--key", "rvx.pk8", "--cert", "rvx.der"]


def _build_reseam(app_name: str, arch: str, download_files: "list[Path]"):
    cli = _reseam_find_cli(download_files)
    bundle = utils.find_file(download_files, suffix=".reseam")
    if not cli or not bundle:
        raise RuntimeError(f"reseam: CLI or bundle missing after download "
                           f"(files: {[f.name for f in download_files]})")
    # Use an ABSOLUTE path to exec the CLI: a bare relative name ("reseam-linux-x64")
    # is looked up on PATH, not the cwd, so it would not be found when run.
    cli = cli.resolve()
    try:
        cli.chmod(0o755)
    except Exception:
        pass

    package = _RESEAM_PACKAGE.get(app_name)
    if not package:
        logging.error(f"❌ reseam: no package mapping for app '{app_name}'")
        return None

    # Resolve the one fixed signing key up front so we fail before the big APK download
    # if it is unavailable (mandatory, no fallback -> users always update in place).
    signing_args = _reseam_signing_args()

    versions = _reseam_supported_versions(cli, bundle, package)
    if not versions:
        raise RuntimeError(f"reseam: bundle lists no supported versions for {package} "
                           f"(reseam CLI/bundle engine mismatch, or bundle list parse failed)")
    logging.info(f"🔖 reseam supports {package}: {versions}")

    # Download the base APK at the newest supported version, falling back down the list.
    input_apk = None
    version = None
    for v in versions:
        input_apk, got_ver, _ = downloader.download_best(app_name, "", "", arch, override_version=v)
        if input_apk:
            version = got_ver or v
            break
    if not input_apk or not version:
        raise RuntimeError(f"reseam: could not download a supported base APK for {app_name} "
                           f"(tried versions {versions} across all providers)")

    stem = _RESEAM_NAME.get(app_name, f"{app_name}-reseam")
    output_apk = Path(f"{stem}-v{version}.apk")
    output_apk.unlink(missing_ok=True)

    cmd = [
        str(cli), "patch", str(input_apk),
        "--bundle", str(bundle),
        "--trust", RESEAM_SIGNER,
        "--preset", "recommended",
        *_RESEAM_OPTIONS.get(app_name, []),
        *signing_args,
        "--output", str(output_apk),
    ]
    logging.info("🔧 Using Reseam patching system...")
    utils.run_process(cmd, capture=True, stream=True)
    input_apk.unlink(missing_ok=True)

    if not output_apk.exists() or output_apk.stat().st_size == 0:
        raise RuntimeError("reseam: patch command produced no output APK")
    logging.info(f"✅ Built {output_apk.name}")
    return str(output_apk)


def run_build(app_name: str, source: str, arch: str = "universal") -> str:
    """Build APK for specific architecture"""
    download_files, name = downloader.download_required(source)

    # Log downloaded files for debugging
    logging.info(f"📦 Downloaded {len(download_files)} files for {source}:")
    for file in download_files:
        logging.info(f"  - {file.name} ({file.stat().st_size} bytes)")

    # Reseam is its own toolchain (native CLI + signed .reseam bundle), not morphe/revanced.
    if name == "reseam":
        return _build_reseam(app_name, arch, download_files)

    # DETECT SOURCE TYPE BASED ON DOWNLOADED FILES
    is_morphe = False
    is_revanced = False

    # Check file contents to determine source type
    for file in download_files:
        if "morphe-cli" in file.name.lower():
            is_morphe = True
            break
        elif "revanced-cli" in file.name.lower():
            is_revanced = True
            break

    # If not detected by CLI name, check patch file extension
    if not is_morphe and not is_revanced:
        for file in download_files:
            if file.suffix == ".mpp":
                is_morphe = True
                break
            elif file.suffix in [".rvp", ".jar"] and "patches" in file.name.lower():
                is_revanced = True
                break

    # If still not detected, fallback to source name
    if not is_morphe and not is_revanced:
        is_morphe = "morphe" in source.lower() or "custom" in source.lower()
        is_revanced = not is_morphe  # Default to ReVanced if not Morphe

    logging.info(f"🔍 Detected: {'Morphe' if is_morphe else 'ReVanced'} source type")

    # FIND FILES BASED ON DETECTED TYPE
    if is_morphe:
        # Find Morphe files - prefer non-dev version
        cli = utils.find_file(download_files, contains="morphe-cli", suffix=".jar", exclude=["dev"])
        if not cli:
            # Fallback to any Morphe CLI
            cli = utils.find_file(download_files, contains="morphe", suffix=".jar")
        
        if not cli:
            cli = utils.find_file(download_files, suffix=".jar")
        patches = utils.find_file(download_files, contains="patches", suffix=".mpp")
        if not patches:
            # Fallback to any .mpp file
            patches = utils.find_file(download_files, suffix=".mpp")
    else:
        # Find ReVanced files
        cli = utils.find_file(download_files, contains="revanced-cli", suffix=".jar")
        patches = utils.find_file(download_files, contains="patches", suffix=".rvp")
        
        if not patches:
            # Try .jar extension for patches
            patches = utils.find_file(download_files, contains="patches", suffix=".jar")

    # Validate tools
    if not cli:
        logging.error(f"❌ CLI not found for source: {source}")
        logging.error(f"Available files: {[f.name for f in download_files]}")
        return None
    if not patches:
        logging.error(f"❌ Patches not found for source: {source}")
        logging.error(f"Available files: {[f.name for f in download_files]}")
        return None

    logging.info(f"✅ Using CLI: {cli.name}")
    logging.info(f"✅ Using patches: {patches.name}")

    # Download the APK, preferring the newest supported version across ALL mirrors
    # before downgrading. A mirror missing the newest version must not silently
    # drop the build to an older one (see downloader.download_best).
    input_apk, version, candidates = downloader.download_best(
        app_name, str(cli), str(patches), arch
    )

    if input_apk is None or not version:
        logging.error(f"❌ Failed to download APK for {app_name}")
        logging.error("All download sources failed. Skipping this app.")
        return None

    # Try the downloaded version first, then (if available) older compatible
    # versions from the patch set. This prevents a single bad/overstated
    # compatibility entry from breaking the whole build.
    versions_to_try: list[str] = [version]
    if candidates and version in candidates:
        versions_to_try += [v for v in candidates if v != version]

    # morphe-cli grammar: options (-O) are bound to the patch enabled right after them,
    # i.e. "-O key=value ... -e <PatchName>". So we collect each "+ PatchName" together
    # with its following "@key=value" options, emitting the options before the -e.
    disable_args = []
    enable_args = []
    _enable_name = None
    _enable_opts = []

    patches_path = Path("patches") / f"{app_name}-{source}.txt"
    if patches_path.exists():
        with patches_path.open('r') as patches_file:
            for line in patches_file:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('+'):
                    if _enable_name is not None:
                        enable_args.extend(_enable_opts)
                        enable_args.extend(["-e", _enable_name])
                    _enable_name = line[1:].strip()
                    _enable_opts = []
                elif line.startswith('@'):
                    _enable_opts.extend(["-O", line[1:].strip()])
                elif line.startswith('-'):
                    disable_args.extend(["-d", line[1:].strip()])
            if _enable_name is not None:
                enable_args.extend(_enable_opts)
                enable_args.extend(["-e", _enable_name])

    # RVX (anddea) apps can be signed with the user's own keystore (from repo secrets) so that
    # existing RVX users get seamless in-place updates. ReVanced-based managers export BKS
    # keystores, which apksigner cannot read but morphe-cli (BouncyCastle) can, so for RVX we
    # sign via morphe-cli and skip the apksigner re-sign.
    rvx_custom_ks = bool(name == "anddea" and getenv("RVX_KEYSTORE_PATH"))
    rvx_ks_path = getenv("RVX_KEYSTORE_PATH", "")
    rvx_ks_pass = getenv("RVX_KEYSTORE_PASSWORD", "")
    rvx_ks_alias = getenv("RVX_KEY_ALIAS", "")
    rvx_key_pass = getenv("RVX_KEY_PASSWORD", "") or rvx_ks_pass

    for attempt_idx, ver in enumerate(versions_to_try):
        if attempt_idx > 0:
            logging.warning(
                f"Retrying {app_name}/{source}/{arch} with older version {ver} due to patch failure..."
            )
            # Cleanup any previous attempt artifacts.
            try:
                input_apk.unlink(missing_ok=True)
            except Exception:
                pass

            input_apk, version, _ = downloader.download_best(app_name, str(cli), str(patches), arch, override_version=ver)
            if input_apk is None:
                continue
            version = ver

        # --- Normalize/merge input into .apk when needed ---
        if input_apk.suffix != ".apk":
            # Check if it is a split bundle (contains multiple .apk files or is .apkm/.xapk/.apks)
            is_bundle = False
            try:
                import zipfile
                if zipfile.is_zipfile(input_apk):
                    with zipfile.ZipFile(input_apk, "r") as z:
                        namelist = z.namelist()
                        has_split_apks = any(n.endswith(".apk") for n in namelist)
                        is_bundle = has_split_apks or input_apk.suffix.lower() in [".apkm", ".xapk", ".apks", ".zip"]
            except Exception as e:
                logging.debug(f"Zip inspection failed for {input_apk}: {e}")

            target_apk = input_apk.with_name(f"{input_apk.stem}.apk" if not input_apk.name.endswith(".apk") else input_apk.name)

            if is_bundle:
                logging.info(f"Input file is a bundle ({input_apk.name}), using APKEditor to merge")
                apk_editor = downloader.download_apkeditor()
                merged_apk = input_apk.with_suffix(".apk")
                merged_apk.unlink(missing_ok=True)

                try:
                    utils.run_process([
                        "java", "-jar", str(apk_editor), "m",
                        "-f",
                        "-i", str(input_apk),
                        "-o", str(merged_apk)
                    ], silent=True, check=True)
                    input_apk.unlink(missing_ok=True)
                    input_apk = merged_apk
                except Exception as e:
                    logging.warning(f"APKEditor merge failed ({e}); checking if file can be used as standalone APK")
                    if input_apk.exists():
                        target_apk.unlink(missing_ok=True)
                        os.replace(input_apk, target_apk)
                        input_apk = target_apk
            else:
                logging.info(f"Normalizing standalone APK filename to {target_apk.name}")
                if input_apk != target_apk:
                    target_apk.unlink(missing_ok=True)
                    os.replace(input_apk, target_apk)
                    input_apk = target_apk

            if not input_apk.exists():
                logging.error("Processed APK file not found")
                raise RuntimeError("Processed APK file not found")

            # Clean up filename: remove build number like (1575420) and -1575420.
            # Only strip 6+ digit build-number tokens so legitimate short version
            # segments (e.g. "app-2_0") are not mangled.
            clean_name = re.sub(r'\(\d+\)', '', input_apk.name)  # Remove (1575420)
            clean_name = re.sub(r'-\d{6,}_', '_', clean_name)  # Remove -1575420_ -> _
            if clean_name != input_apk.name:
                clean_apk = input_apk.with_name(clean_name)
                clean_apk.unlink(missing_ok=True)
                os.replace(input_apk, clean_apk)
                input_apk = clean_apk

            logging.info(f"Normalized APK file: {input_apk}")

        # --- ARCHITECTURE-SPECIFIC PROCESSING ---
        if arch != "universal":
            logging.info(f"Processing APK for {arch} architecture...")
            if arch == "arm64-v8a":
                utils.strip_zip_entries(input_apk, ["lib/x86/*", "lib/x86_64/*", "lib/armeabi-v7a/*"])
            elif arch == "armeabi-v7a":
                utils.strip_zip_entries(input_apk, ["lib/x86/*", "lib/x86_64/*", "lib/arm64-v8a/*"])
        else:
            utils.strip_zip_entries(input_apk, ["lib/x86/*", "lib/x86_64/*"])

        # Validate APK integrity
        logging.info("Checking APK integrity...")
        if not utils.check_apk_integrity(input_apk):
            logging.warning("APK integrity check failed; attempting repair with zip -FF if available")
            if shutil.which("zip"):
                fixed_apk = Path(f"{app_name}-fixed-v{version}.apk")
                subprocess.run([
                    "zip", "-FF", str(input_apk), "--out", str(fixed_apk)
                ], check=False, capture_output=True)

                if fixed_apk.exists() and fixed_apk.stat().st_size > 0:
                    input_apk.unlink(missing_ok=True)
                    fixed_apk.rename(input_apk)
                    logging.info("APK fixed successfully")
                else:
                    logging.warning("Repair produced no usable file; keeping original APK")
            else:
                logging.warning("zip command not available for repair; proceeding with current APK")
        else:
            logging.info("APK integrity OK; no repair needed")

        # Include architecture in output filename
        output_apk = Path(f"{app_name}-{arch}-patch-v{version}.apk")

        try:
            # USE DIFFERENT COMMANDS BASED ON SOURCE TYPE
            if is_morphe:
                logging.info("🔧 Using Morphe patching system...")
                morphe_cmd = [
                    "java", "-jar", str(cli),
                    "patch", "--patches", str(patches),
                    "--out", str(output_apk), str(input_apk),
                    *enable_args, *disable_args
                ]
                if rvx_custom_ks:
                    # Sign with the user's keystore (morphe-cli reads BKS natively).
                    morphe_cmd += [
                        "--keystore", rvx_ks_path,
                        "--keystore-password", rvx_ks_pass,
                        "--keystore-entry-alias", rvx_ks_alias,
                        "--keystore-entry-password", rvx_key_pass,
                    ]
                utils.run_process(morphe_cmd, capture=True, stream=True)
            else:
                logging.info("🔧 Using ReVanced patching system...")
                cli_name = Path(cli).name.lower()
                is_revanced_v6_or_newer = (
                    'revanced-cli-6' in cli_name or 'revanced-cli-7' in cli_name or 'revanced-cli-8' in cli_name
                )

                if is_revanced_v6_or_newer:
                    utils.run_process([
                        "java", "-jar", str(cli),
                        "patch", "-p", str(patches), "-b",
                        "--out", str(output_apk), str(input_apk),
                        *enable_args, *disable_args
                    ], capture=True, stream=True)
                else:
                    utils.run_process([
                        "java", "-jar", str(cli),
                        "patch", "--patches", str(patches),
                        "--out", str(output_apk), str(input_apk),
                        *enable_args, *disable_args
                    ], capture=True, stream=True)

        except subprocess.CalledProcessError as e:
            # Remove temp input apk; we'll re-download if retrying.
            input_apk.unlink(missing_ok=True)
            output_apk.unlink(missing_ok=True)

            if attempt_idx < len(versions_to_try) - 1 and _should_retry_with_older_version(getattr(e, "output", None)):
                continue
            raise

        # Patch succeeded -> cleanup input and sign.
        input_apk.unlink(missing_ok=True)

        if name == "anddea":
            # RVX builds get their own filenames, e.g. rvx-app-anddea-v<ver>.apk and
            # rvx-music-anddea-arm64-v8a-v<ver>.apk (universal arch is omitted).
            rvx_name = {"youtube": "rvx-app", "youtube-music": "rvx-music"}.get(app_name, app_name)
            arch_suffix = "" if arch == "universal" else f"-{arch}"
            signed_apk = Path(f"{rvx_name}-{name}{arch_suffix}-v{version}.apk")
        else:
            signed_apk = Path(f"{app_name}-{arch}-{name}-v{version}.apk")

        if rvx_custom_ks:
            # morphe-cli already aligned and signed the APK with the user's keystore, so just
            # rename it to the final name (apksigner cannot load the BKS keystore).
            logging.info("RVX build signed by morphe-cli with the custom keystore.")
            output_apk.rename(signed_apk)
        else:
            apksigner = utils.find_apksigner()
            if not apksigner:
                raise RuntimeError("apksigner not found")
            try:
                utils.run_process([
                    str(apksigner), "sign", "--verbose",
                    "--ks", "keystore/public.jks",
                    "--ks-pass", "pass:public",
                    "--key-pass", "pass:public",
                    "--ks-key-alias", "public",
                    "--in", str(output_apk), "--out", str(signed_apk)
                ], capture=True, stream=True)
            except Exception as e:
                logging.warning(f"Standard signing failed: {e}")
                logging.info("Trying alternative signing method...")

                utils.run_process([
                    str(apksigner), "sign", "--verbose",
                    "--min-sdk-version", "21",
                    "--ks", "keystore/public.jks",
                    "--ks-pass", "pass:public",
                    "--key-pass", "pass:public",
                    "--ks-key-alias", "public",
                    "--in", str(output_apk), "--out", str(signed_apk)
                ], capture=True, stream=True)

        output_apk.unlink(missing_ok=True)
        print(f"✅ APK built: {signed_apk.name}")
        return str(signed_apk)

    # If we got here, every candidate version failed.
    return None

def main():
    app_name = getenv("APP_NAME")
    source = getenv("SOURCE")

    if not app_name or not source:
        logging.error("APP_NAME and SOURCE environment variables must be set")
        exit(1)

    # Read arch-config.json
    arch_config_path = Path("arch-config.json")
    if arch_config_path.exists():
        with open(arch_config_path) as f:
            arch_config = json.load(f)
        
        # Find arches for this app
        arches = [(getenv("ARCH") or "universal").strip()]
        for config in arch_config:
            if not getenv("ARCH") and config["app_name"] == app_name and config["source"] == source:
                arches = config["arches"]
                break
        
        # Build for each architecture
        built_apks = []
        for arch in arches:
            logging.info(f"🔨 Building {app_name} for {arch} architecture...")
            apk_path = run_build(app_name, source, arch)
            if apk_path:
                built_apks.append(apk_path)
                print(f"✅ Built {arch} version: {Path(apk_path).name}")
        
        # Summary
        print(f"\n🎯 Built {len(built_apks)} APK(s) for {app_name}:")
        for apk in built_apks:
            print(f"  📱 {Path(apk).name}")
        
    else:
        # Fallback to single universal build
        logging.warning("arch-config.json not found, building universal only")
        apk_path = run_build(app_name, source, "universal")
        if apk_path:
            print(f"🎯 Final APK path: {apk_path}")

if __name__ == "__main__":
    main()
