"""The one shared browser launcher (sync and async).

Policy: use an INSTALLED Chromium-family browser. Chrome first on macOS and
Linux, Edge first on Windows, then the others, then a system Chromium. The
Playwright-downloaded Chromium is an optional last resort and is never
required. Nothing here downloads a browser.

Only a missing or broken binary falls through to the next candidate. A locked
profile (another bb or browser owns it) stops immediately: no other browser
can open the same profile.
"""

import logging
import ntpath
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger("blackboard.browser")

NAMES = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "brave": "Brave Browser", "chromium": "Chromium"}
BROWSER_INSTALL_COMMAND = "python -m playwright install chromium --no-shell"
PROFILE_LOCKED_MESSAGE = (
    "Another bb command or browser window is using the Blackboard browser profile. "
    "Close it (or wait for the other bb run to finish) and try again."
)
_LOCK_MARKERS = (
    "processsingleton", "singletonlock", "opening in existing browser session",
    "profile appears to be in use", "already in use",
)
_PREFERENCES = ("auto", "chrome", "msedge", "brave", "chromium")

_winner: dict | None = None  # candidate that launched first in this process
_announced = False

_WINDOWS_PROGIDS = {"chromehtml": "chrome", "msedgehtm": "msedge", "bravehtml": "brave", "chromiumhtm": "chromium"}
_MAC_BUNDLES = {
    "com.google.chrome": "chrome", "com.microsoft.edgemac": "msedge",
    "com.brave.browser": "brave", "org.chromium.chromium": "chromium",
}
_LINUX_DESKTOP = (
    ("google-chrome", "chrome"), ("microsoft-edge", "msedge"), ("brave", "brave"), ("chromium", "chromium"),
)


class BrowserProfileLockedError(RuntimeError):
    """The profile directory is owned by another browser process."""

    def __init__(self, message: str = PROFILE_LOCKED_MESSAGE):
        super().__init__(message)


class BrowserUnavailableError(RuntimeError):
    """No usable browser could be launched."""


def is_profile_locked(error: BaseException) -> bool:
    text = str(error).lower()
    if any(marker in text for marker in _LOCK_MARKERS):
        return True
    return "target directory" in text and "is in use" in text


def default_browser() -> tuple[str | None, str]:
    """The OS default web browser: (chromium-family key or None, human label).

    Only Chromium-family browsers can open the Blackboard profile, so Firefox,
    Safari and anything unknown map to None. Read once per process; any error
    reads as "unknown".
    """
    global _default_cache
    if _default_cache is None:
        try:
            _default_cache = _read_default_browser()
        except Exception:
            _default_cache = (None, "unknown")
    return _default_cache


_default_cache: tuple[str | None, str] | None = None


def _read_default_browser() -> tuple[str | None, str]:
    if sys.platform == "win32":
        import winreg
        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            prog_id = str(winreg.QueryValueEx(handle, "ProgId")[0])
        base = prog_id.split("-")[0]
        label = "Firefox" if base.lower().startswith("firefox") else base
        return _WINDOWS_PROGIDS.get(prog_id.lower()), label
    if sys.platform == "darwin":
        import plistlib
        path = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
        handlers = plistlib.loads(path.read_bytes()).get("LSHandlers", [])
        for handler in handlers:
            if str(handler.get("LSHandlerURLScheme", "")).lower() == "http":
                bundle = str(handler.get("LSHandlerRoleAll") or handler.get("LSHandlerRoleViewer") or "").lower()
                label = {"com.apple.safari": "Safari", "org.mozilla.firefox": "Firefox"}.get(bundle, bundle or "unknown")
                return _MAC_BUNDLES.get(bundle), label
        return None, "Safari"  # no explicit choice recorded: macOS ships Safari
    import subprocess
    desktop = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True, text=True, timeout=2).stdout.strip()
    for marker, key in _LINUX_DESKTOP:
        if desktop.lower().startswith(marker):
            return key, desktop
    return None, desktop or "unknown"


def _windows_registered_path(exe: str) -> str | None:
    """App Paths registry entry: finds browsers installed in custom locations."""
    try:
        import winreg
    except ImportError:
        return None
    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as handle:
                return winreg.QueryValue(handle, None) or None
        except OSError:
            continue
    return None


def _browser_paths(browser: str) -> list[str]:
    """Standard system/per-user installations, then registry, then PATH."""
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
            "chromium": ("Chromium", "Application", "chrome.exe"),
        }[browser]
        # Both machine architectures and per-user installs. No literal %VAR%
        # paths when an environment variable is absent.
        paths = [ntpath.join(root, *suffix)
                 for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
                 if (root := os.environ.get(key))]
        if browser != "chromium" and (registered := _windows_registered_path(suffix[-1])):
            paths.append(registered)
    binaries = {
        "chrome": ("google-chrome-stable", "google-chrome", "chrome"),
        "msedge": ("microsoft-edge-stable", "microsoft-edge", "msedge"),
        "brave": ("brave-browser", "brave"),
        "chromium": ("chromium", "chromium-browser"),
    }[browser]
    paths.extend(path for binary in binaries if (path := shutil.which(binary)))
    return list(dict.fromkeys(paths))


def _configured() -> tuple[str, str]:
    """Browser override (env wins over config.json): (preference, executable path)."""
    preference = os.environ.get("BLACKBOARD_BROWSER", "").strip().lower()
    path = os.environ.get("BLACKBOARD_BROWSER_PATH", "").strip()
    if not preference and not path:
        try:
            from core.config import load_config
            config = load_config()
        except Exception:
            config = {}
        preference = str(config.get("browser") or "").strip().lower()
        path = str(config.get("browser_path") or "").strip()
    return preference or "auto", path


def get_browser_launch_candidates() -> list[dict]:
    """Installed browsers that exist on disk, in preference order, then the optional bundled ones.

    BLACKBOARD_BROWSER (or config.json "browser") pins auto/chrome/msedge/brave/chromium
    and never falls through to a different browser. BLACKBOARD_BROWSER_PATH (or
    "browser_path") pins a custom executable and takes precedence.
    """
    preference, explicit_path = _configured()
    if explicit_path:
        path = Path(os.path.expandvars(explicit_path)).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("BLACKBOARD_BROWSER_PATH must be an absolute path to a browser executable.")
        return [{"name": "Configured browser", "executable_path": str(path)}]
    if preference not in _PREFERENCES:
        raise ValueError("BLACKBOARD_BROWSER must be auto, chrome, msedge, brave, or chromium.")

    candidates = []
    order = ("msedge", "chrome", "brave", "chromium") if sys.platform == "win32" else ("chrome", "msedge", "brave", "chromium")
    if preference == "auto":
        default_key, _ = default_browser()
        if default_key:  # the user's own browser goes first when it is Chromium-family
            order = (default_key,) + tuple(b for b in order if b != default_key)
    for browser in order:
        if preference not in ("auto", browser):
            continue
        found = False
        for path in _browser_paths(browser):
            if Path(path).is_file():
                candidates.append({"name": NAMES[browser], "executable_path": path})
                found = True
            else:
                logger.debug("Skipping %s: %s does not exist", NAMES[browser], path)
        if not found:
            logger.debug("Skipping %s: not installed", NAMES[browser])
    if preference in ("auto", "chromium"):
        # Optional last resort; only used if it was downloaded (checked at launch).
        candidates.append({"name": "Playwright Chromium", "channel": "chromium", "bundled": True})
        candidates.append({"name": "Playwright Bundled Chromium", "channel": None, "bundled": True})
    return candidates


def browser_unavailable_message() -> str:
    return (
        "No usable browser found. Install Google Chrome (or Microsoft Edge on Windows, Brave, or Chromium) "
        "and run again. To pin one: set BLACKBOARD_BROWSER=chrome|msedge|brave|chromium or "
        "BLACKBOARD_BROWSER_PATH=<absolute path to the executable>. "
        f"Optional last resort: {BROWSER_INSTALL_COMMAND}"
    )


def _ordered(candidates: list[dict]) -> list[dict]:
    """The candidate that worked earlier in this process goes first."""
    if _winner is not None and _winner in candidates:
        return [_winner] + [c for c in candidates if c != _winner]
    return candidates


def _skip_bundled(pw, spec: dict) -> bool:
    """A bundled candidate is only attempted when its download exists."""
    if not spec.get("bundled"):
        return False
    try:
        if not Path(pw.chromium.executable_path).is_file():
            logger.debug("Skipping %s: not downloaded", spec["name"])
            return True
    except Exception:
        pass
    return False


def _announce(spec: dict) -> None:
    """One stderr line, once per process, when the OS default browser could not be used."""
    global _announced
    logger.info("Using %s for Blackboard", spec["name"])
    if _announced:
        return
    _announced = True
    if _configured() != ("auto", ""):
        return
    default_key, label = default_browser()
    if default_key and NAMES.get(default_key) == spec["name"]:
        return
    if default_key:
        reason = f"your default browser ({NAMES[default_key]}) is not installed"
    else:
        reason = f"your default browser ({label}) can't run the Blackboard profile"
    print(f"Using {spec['name']}: {reason}.", file=sys.stderr)


def _launch_kwargs(spec: dict, user_data_dir, headless: bool, extra: dict) -> dict:
    kwargs = {
        "user_data_dir": str(user_data_dir),
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
        "viewport": {"width": 1280, "height": 800},
        **extra,
    }
    if spec.get("channel"):
        kwargs["channel"] = spec["channel"]
    if spec.get("executable_path"):
        kwargs["executable_path"] = spec["executable_path"]
    return kwargs


def _failed(spec: dict, error: Exception) -> None:
    """Stop on a profile lock; log and let a missing/broken binary fall through."""
    if is_profile_locked(error):
        raise BrowserProfileLockedError() from error
    logger.debug("Skipping %s: %s", spec["name"], type(error).__name__)


def launch_persistent_context(pw, user_data_dir, headless: bool = True, **extra):
    """Sync: launch the profile in the best installed browser. Returns (context, spec)."""
    global _winner
    last_error = None
    for spec in _ordered(get_browser_launch_candidates()):
        if _skip_bundled(pw, spec):
            continue
        try:
            context = pw.chromium.launch_persistent_context(**_launch_kwargs(spec, user_data_dir, headless, extra))
        except Exception as error:
            last_error = error
            _failed(spec, error)
            continue
        _winner = spec
        _announce(spec)
        return context, spec
    raise BrowserUnavailableError(browser_unavailable_message()) from last_error


async def launch_persistent_context_async(pw, user_data_dir, headless: bool = True, **extra):
    """Async twin of launch_persistent_context (same candidates, same stop rules)."""
    global _winner
    last_error = None
    for spec in _ordered(get_browser_launch_candidates()):
        if _skip_bundled(pw, spec):
            continue
        try:
            context = await pw.chromium.launch_persistent_context(**_launch_kwargs(spec, user_data_dir, headless, extra))
        except Exception as error:
            last_error = error
            _failed(spec, error)
            continue
        _winner = spec
        _announce(spec)
        return context, spec
    raise BrowserUnavailableError(browser_unavailable_message()) from last_error
