import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from espsync import updater


@pytest.fixture
def server(monkeypatch):
    files: dict[str, bytes] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = files.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setenv(updater.URL_OVERRIDE_ENV, base + "/latest")
    yield base, files
    httpd.shutdown()


def release(base, payload=b"installer", tag="v9.1.0", digest=None, **extra):
    digest = digest if digest is not None else "sha256:" + hashlib.sha256(payload).hexdigest()
    data = {
        "tag_name": tag,
        "html_url": "https://github.com/Alli1223/EspSync/releases/tag/" + tag,
        "body": "notes",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": "source.zip", "browser_download_url": base + "/src", "size": 1},
            {"name": f"EspSync-Setup-{tag[1:]}.exe", "browser_download_url": base + "/setup.exe",
             "size": len(payload), "digest": digest},
        ],
    }
    data.update(extra)
    return json.dumps(data).encode()


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [
        ("v0.2.0", "0.1.0", True),
        ("0.1.1", "0.1.0.57", True),  # a release beats any dev build of the previous version
        ("v0.1.0", "0.1.0.57", False),  # dev build from main is newer than the release it follows
        ("v0.1.0", "0.1.0", False),
        ("v1.0.0-beta2", "0.9.9", True),
        ("garbage", "0.1.0", False),
    ],
)
def test_is_newer(candidate, current, newer):
    assert updater.is_newer(candidate, current) is newer


def test_parse_release_picks_installer_and_digest():
    info = updater.parse_release(json.loads(release("http://x")))
    assert info.version == "9.1.0" and info.asset_name == "EspSync-Setup-9.1.0.exe"
    assert info.sha256 == hashlib.sha256(b"installer").hexdigest()
    assert updater.parse_release(json.loads(release("http://x", prerelease=True))) is None
    assert updater.parse_release(json.loads(release("http://x", draft=True))) is None


def test_check_against_server(server):
    base, files = server
    files["/latest"] = release(base)
    assert updater.check("0.1.0").version == "9.1.0"
    assert updater.check("9.1.0") is None


def test_download_verifies_hash(server, tmp_path):
    base, files = server
    payload = b"x" * 300_000
    files["/latest"] = release(base, payload)
    files["/setup.exe"] = payload
    seen = []
    path = updater.download(updater.check("0.1.0"), tmp_path, progress=lambda d, t: seen.append((d, t)))
    assert path.read_bytes() == payload
    assert seen[-1] == (len(payload), len(payload))


def test_download_rejects_corrupt_file(server, tmp_path):
    base, files = server
    files["/latest"] = release(base, b"good", digest="sha256:" + "0" * 64)
    files["/setup.exe"] = b"good"
    with pytest.raises(updater.UpdateError, match="SHA-256"):
        updater.download(updater.check("0.1.0"), tmp_path)
    assert list(tmp_path.iterdir()) == []  # no partial or bad file left behind


def test_download_refuses_untrusted_hosts(monkeypatch, tmp_path):
    monkeypatch.delenv(updater.URL_OVERRIDE_ENV, raising=False)
    info = updater.UpdateInfo("9.0.0", "v9.0.0", "", "", "EspSync-Setup-9.0.0.exe", "https://evil.example/x.exe", 1)
    with pytest.raises(updater.UpdateError, match="refusing"):
        updater.download(info, tmp_path)
    info.asset_url = "http://github.com/x.exe"
    with pytest.raises(updater.UpdateError, match="refusing"):
        updater.download(info, tmp_path)


def test_installer_args_and_scope(monkeypatch, tmp_path):
    args = updater.installer_args(Path("setup.exe"), "currentuser")
    assert "/SILENT" in args and "/CURRENTUSER" in args and "/relaunch=1" in args
    assert "/ALLUSERS" in updater.installer_args(Path("s.exe"), "allusers", relaunch=False)
    pf = tmp_path / "Program Files"
    (pf / "EspSync").mkdir(parents=True)
    monkeypatch.setenv("ProgramFiles", str(pf))
    assert updater.install_scope(pf / "EspSync" / "EspSync.exe") == "allusers"
    assert updater.install_scope(tmp_path / "Local" / "EspSync.exe") == "currentuser"


def test_source_runs_are_not_installed_builds():
    assert updater.is_installed_build() is False
