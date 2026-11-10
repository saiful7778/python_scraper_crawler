import os
from typing import Final

CANDIDATES = {
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ],
    "Linux": [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "brave-browser",
    ],
    "Windows": [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles%\Chromium\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
}

#: Prefix ``tempfile.mkdtemp`` uses for Chrome profiles. Kept in one place
#: so :func:`prune_stale_profiles` matches what :class:`Browser` creates.
PROFILE_PREFIX: Final[str] = "python_google_maps_scraper-chrome-"
KIT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME_PATH = os.path.join(
    KIT_PATH, ".engine"
)  # everything the engine keeps: scrapes, results, logs, its Chrome

ARCH_ALIASES: Final[dict[str, str]] = {
    "x86_64": "x64",
    "amd64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
}

CFT_PLATFORMS: Final[dict[tuple[str, str], str]] = {
    ("Darwin", "arm64"): "mac-arm64",
    ("Darwin", "x64"): "mac-x64",
    ("Linux", "x64"): "linux64",
    ("Linux", "arm64"): "linux-arm64",
    ("Windows", "x64"): "win64",
    ("Windows", "arm64"): "win64",
}
# Chrome for Testing's headless shell (https://googlechromelabs.github.io/chrome-for-testing/),
# Stable channel. Each zip's SHA-256 was computed from the download on 2026-10-31. A download that
# does not match is deleted and never run. Windows on Arm runs the x64 build under emulation.
CFT_VERSION = "156.0.8078.80"
CFT_URL = "https://storage.googleapis.com/chrome-for-testing-public/{v}/{p}/chrome-headless-shell-{p}.zip"
CFT_SHA256 = {
    "mac-arm64": "ea615baa2e9caf0d02d1898a6562b5f8fa9c35239707dae0f592178df5b986ac",
    "mac-x64": "535d417d9034cbd11e7dacd1a71aa3d6e1b525e6d5dcd2671eb2532f01481228",
    "linux64": "547dcd374e02961bf998f6046a267644f8502225c36bb78ca307dab32a0b3eb4",
    "linux-arm64": "5bb6a00a4285c13303d3f72c5afdb70f144ea7a93eed462365a52b69631c9607",
    "win64": "10a77466005e7f64e00f1a75fd1ea81f4a38a60093c65fea6895843b26e60a9a",
}
# What Chrome needs on a bare Debian or Ubuntu (a server, a container). apt picks the t64 variants
# (libasound2t64 and friends) on Ubuntu 24.04 by itself.
APT_LINE = (
    "sudo apt-get install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 "
    "libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 "
    "libcairo2 libasound2"
)

PY = "py" if os.name == "nt" else "python3"  # what the user types in the messages below
