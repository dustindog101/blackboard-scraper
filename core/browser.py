"""Shared browser selection. Launching never downloads a browser."""

import ntpath
import os
import shutil
import sys
from pathlib import Path

NAMES = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "brave": "Brave Browser"}
BROWSER_INSTALL_COMMAND = "python -m playwright install chromium --no-shell"


def _browser_paths(browser: str) -> list[str]:
    """Standard system/per-user installations, then executables on PATH."""
    paths = []
    if sys.platform == "darwin":
        app = NAMES[browser]
        paths = [str(root / f"{app}.app" / "Contents" / "MacOS" / app)
                 for root in (Path("/Applications"), Path.home() / "Applications")]
    elif sys.platform == "win32":
        suffix = {
            "chrome": ("Google", "Chrome", "Application", "chrome.exe"),
            "msedge": ("Microsoft", "Edge", "Application", "msedge.exe"),
            "brave": ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        }[browser]
        # Include both machine architectures and per-user installs. Avoid literal
        # %VAR% paths when an environment variable is absent.
        paths = [ntpath.join(root, *suffix)
                 for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
                 if (root := os.environ.get(key))]
    binaries = {
        "chrome": ("google-chrome-stable", "google-chrome", "chrome"),
        "msedge": ("microsoft-edge-stable", "microsoft-edge", "msedge"),
        "brave": ("brave-browser", "brave"),
    }[browser]
    paths.extend(path for binary in binaries if (path := shutil.which(binary)))
    return list(dict.fromkeys(paths))


def get_browser_launch_candidates() -> list[dict]:
    """Prefer installed browsers; keep full Chromium and legacy shell fallbacks.

    BLACKBOARD_BROWSER pins auto/chrome/msedge/brave/chromium. A pinned choice
    never falls through to a different browser. BLACKBOARD_BROWSER_PATH pins a
    custom Chromium executable and takes precedence over BLACKBOARD_BROWSER.
    """
    explicit_path = os.environ.get("BLACKBOARD_BROWSER_PATH", "").strip()
    if explicit_path:
        path = Path(os.path.expandvars(explicit_path)).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("BLACKBOARD_BROWSER_PATH must be an absolute path to a browser executable.")
        return [{"name": "Configured browser", "executable_path": str(path)}]

    preference = os.environ.get("BLACKBOARD_BROWSER", "auto").strip().lower()
    if preference not in ("auto", "chrome", "msedge", "brave", "chromium"):
        raise ValueError("BLACKBOARD_BROWSER must be auto, chrome, msedge, brave, or chromium.")

    candidates = []
    order = ("msedge", "chrome", "brave") if sys.platform == "win32" else ("chrome", "msedge", "brave")
    for browser in order:
        if preference not in ("auto", browser):
            continue
        # Native Playwright channels handle standard Chrome/Edge installs and
        # their current headless mode. Paths cover user Applications/custom PATH.
        if browser != "brave":
            candidates.append({"name": NAMES[browser], "channel": browser})
        for path in _browser_paths(browser):
            if Path(path).is_file():
                candidates.append({"name": NAMES[browser], "executable_path": path})

    if preference == "auto":
        for binary in ("chromium", "chromium-browser"):
            if path := shutil.which(binary):
                candidates.append({"name": "System Chromium", "executable_path": path})
        candidates.append({"name": "Google Chrome Beta", "channel": "chrome-beta"})
    if preference in ("auto", "chromium"):
        # New headless mode uses the SAME full binary as manual/headed login,
        # so installing with --no-shell works for both modes.
        candidates.append({"name": "Playwright Chromium", "channel": "chromium"})
        candidates.append({"name": "Playwright Bundled Chromium", "channel": None})
    return candidates


def browser_unavailable_message() -> str:
    return (
        "Could not launch a compatible browser. Close any Blackboard browser windows/background jobs and retry. "
        "Use an installed Chrome or Edge, or optionally install Chromium with: "
        f"{BROWSER_INSTALL_COMMAND}. If BLACKBOARD_BROWSER or BLACKBOARD_BROWSER_PATH is set, "
        "check that choice or unset it to restore automatic selection."
    )
