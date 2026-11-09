"""Self-update from GitHub Releases.

The installed Windows app checks the repo's latest (non-prerelease) release.
When it is newer, it downloads the `EspSync-Setup-*.exe` asset, verifies its
SHA-256 against the digest GitHub publishes for the asset, and runs the
installer silently. The installer upgrades in place and starts EspSync again
(see `ShouldRelaunch` in packaging/installer.iss).

Running from source (not the installer build) only reports that an update
exists, with a link to the release page.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import __version__

log = logging.getLogger(__name__)

REPO = "Alli1223/EspSync"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
ASSET_RE = re.compile(r"^EspSync-Setup-.+\.exe$", re.IGNORECASE)
TRUSTED_HOSTS = {"github.com", "api.github.com"}
# For testing against a local server: point the updater at another
# "latest release" JSON. Any download URL is then accepted.
URL_OVERRIDE_ENV = "ESPSYNC_UPDATE_URL"


class UpdateError(RuntimeError):
    pass


@dataclass
class UpdateInfo:
    version: str
    tag: str
    notes: str
    page_url: str
    asset_name: str = ""
    asset_url: str = ""
    asset_size: int = 0
    sha256: str = ""

    @property
    def installable(self) -> bool:
        return bool(self.asset_url)


# --------------------------------------------------------------------- versions


def parse_version(text: str) -> tuple[int, int, int, int]:
    """'v0.2.0' -> (0, 2, 0, 0); '0.1.0.57' -> (0, 1, 0, 57). Suffixes are ignored."""
    m = re.match(r"\s*[vV]?(\d+(?:\.\d+){0,3})", text or "")
    if not m:
        return (0, 0, 0, 0)
    nums = [int(n) for n in m.group(1).split(".")]
    return tuple(nums + [0] * (4 - len(nums)))  # type: ignore[return-value]


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def current_version() -> str:
    return __version__


# ------------------------------------------------------------- install context


def is_installed_build() -> bool:
    """True for the app installed by EspSync-Setup.exe (the only kind we can update)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    return Path(sys.executable).with_name("unins000.exe").exists()


def install_scope(exe: Path | None = None) -> str:
    """'allusers' if installed under Program Files, else 'currentuser'."""
    exe = (exe or Path(sys.executable)).resolve()
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        root = os.environ.get(var)
        if root and str(exe).lower().startswith(str(Path(root).resolve()).lower() + os.sep):
            return "allusers"
    return "currentuser"


# ----------------------------------------------------------------------- check


def _request(url: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": f"EspSync/{current_version()} (+https://github.com/{REPO})",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def fetch_latest(url: str | None = None, timeout: float = 10.0) -> dict:
    url = url or os.environ.get(URL_OVERRIDE_ENV) or LATEST_URL
    with urllib.request.urlopen(_request(url, "application/vnd.github+json"), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_release(data: dict) -> UpdateInfo | None:
    if data.get("draft") or data.get("prerelease") or not data.get("tag_name"):
        return None
    tag = str(data["tag_name"])
    info = UpdateInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        notes=str(data.get("body") or ""),
        page_url=str(data.get("html_url") or RELEASES_PAGE),
    )
    asset = next((a for a in data.get("assets") or [] if ASSET_RE.match(str(a.get("name", "")))), None)
    if asset:
        info.asset_name = str(asset["name"])
        info.asset_url = str(asset.get("browser_download_url") or "")
        info.asset_size = int(asset.get("size") or 0)
        digest = str(asset.get("digest") or "")
        if digest.lower().startswith("sha256:"):
            info.sha256 = digest.split(":", 1)[1].lower()
    return info


def check(current: str | None = None, url: str | None = None) -> UpdateInfo | None:
    """The latest release if it is newer than `current`, else None."""
    info = parse_release(fetch_latest(url))
    current = current or current_version()
    if info is None or not is_newer(info.version, current):
        return None
    return info


# -------------------------------------------------------------------- download


def _check_url(url: str) -> None:
    if os.environ.get(URL_OVERRIDE_ENV):
        return
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in TRUSTED_HOSTS:
        raise UpdateError(f"refusing to download from {url}")


def download(
    info: UpdateInfo,
    dest_dir: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Download the installer and verify its size and SHA-256."""
    if not info.installable:
        raise UpdateError("this release has no Windows installer")
    _check_url(info.asset_url)
    dest_dir = dest_dir or download_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / Path(info.asset_name).name
    partial = final.with_suffix(final.suffix + ".part")
    sha = hashlib.sha256()
    done = 0
    try:
        with urllib.request.urlopen(_request(info.asset_url, "application/octet-stream"), timeout=30) as resp, \
                partial.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or info.asset_size or 0)
            while chunk := resp.read(256 * 1024):
                if cancelled and cancelled():
                    raise UpdateError("download cancelled")
                out.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if info.asset_size and done != info.asset_size:
            raise UpdateError(f"download incomplete ({done} of {info.asset_size} bytes)")
        if info.sha256:
            if sha.hexdigest() != info.sha256:
                raise UpdateError("download is corrupt (SHA-256 does not match the release)")
        else:
            log.warning("release asset has no published digest; only the size was checked")
        os.replace(partial, final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    log.info("downloaded %s (%d bytes, sha256 ok=%s)", final, done, bool(info.sha256))
    return final


def download_dir() -> Path:
    return Path(tempfile.gettempdir()) / "EspSync-update"


def cleanup_downloads() -> None:
    """Remove installers left over from earlier updates (ignores files still in use)."""
    folder = download_dir()
    if not folder.is_dir():
        return
    for item in folder.iterdir():
        try:
            item.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------- install


def installer_args(installer: Path, scope: str, relaunch: bool = True) -> list[str]:
    args = [
        str(installer),
        "/SILENT",  # progress window only, no questions
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        "/ALLUSERS" if scope == "allusers" else "/CURRENTUSER",
    ]
    if relaunch:
        args.append("/relaunch=1")
    return args


def launch_installer(installer: Path, scope: str | None = None, relaunch: bool = True) -> None:
    """Start the installer detached; the caller should exit straight after."""
    args = installer_args(installer, scope or install_scope(), relaunch)
    log.info("launching installer: %s", " ".join(args))
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(args, creationflags=flags, close_fds=True)  # noqa: S603 - verified download
