import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import SESSION_DIR

PID_FILE = SESSION_DIR / "bot.pid"
LOG_FILE = SESSION_DIR / "bot.log"


def get_process_memory_mb(pid: int) -> float:
    """Returns memory usage of process in MB."""
    try:
        import resource
        # If current process
        if pid == os.getpid():
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # On macOS, ru_maxrss is in bytes; on Linux it's in KB
            if sys.platform == "darwin":
                return round(usage / (1024 * 1024), 2)
            else:
                return round(usage / 1024, 2)
    except Exception:
        pass

    # Fallback to ps command on Unix/macOS
    try:
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)]).decode().strip()
        if out:
            # RSS in KB
            return round(int(out) / 1024, 2)
    except Exception:
        pass
    return 0.0


def is_pid_alive(pid: int) -> bool:
    """Check if process with given PID exists and is running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_bot_pid_lock() -> bool:
    """Write current PID to bot.pid. Returns False if another instance is already alive."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            if is_pid_alive(old_pid) and old_pid != os.getpid():
                return False
        except (ValueError, OSError):
            pass

    PID_FILE.write_text(str(os.getpid()))
    return True


def release_bot_pid_lock() -> None:
    """Remove bot.pid lock file if owned by current process."""
    try:
        if PID_FILE.exists():
            current = PID_FILE.read_text().strip()
            if current == str(os.getpid()):
                PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def get_bot_status() -> Dict[str, Any]:
    """Retrieve running status of the Telegram bot daemon."""
    if not PID_FILE.exists():
        return {"running": False, "pid": None, "memory_mb": 0, "log_file": str(LOG_FILE)}

    try:
        pid = int(PID_FILE.read_text().strip())
        if is_pid_alive(pid):
            mem = get_process_memory_mb(pid)
            return {
                "running": True,
                "pid": pid,
                "memory_mb": mem,
                "log_file": str(LOG_FILE),
            }
        else:
            # Stale PID file
            PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    return {"running": False, "pid": None, "memory_mb": 0, "log_file": str(LOG_FILE)}


def start_bot_daemon() -> bool:
    """Spawn the bot in background as a detached daemon process."""
    status = get_bot_status()
    if status["running"]:
        print(f"⚠️ Telegram bot is already running in background (PID: {status['pid']}).")
        return False

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    script_path = Path(__file__).resolve().parent.parent / "telegram_bot.py"
    python_bin = sys.executable

    with open(LOG_FILE, "a") as log_f:
        log_f.write(f"\n--- Bot Daemon Started at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        proc = subprocess.Popen(
            [python_bin, str(script_path)],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(script_path.parent),
        )

    # Allow a second to confirm PID lock
    time.sleep(1.0)
    if is_pid_alive(proc.pid):
        print(f"🚀 Telegram Bot Daemon started in background.")
        print(f"   PID:  {proc.pid}")
        print(f"   Logs: {LOG_FILE}")
        return True
    else:
        print(f"❌ Failed to start Telegram Bot Daemon. Check logs at {LOG_FILE}.")
        return False


def stop_bot_daemon() -> bool:
    """Gracefully terminate running background Telegram bot daemon."""
    status = get_bot_status()
    if not status["running"]:
        print("ℹ️ Telegram bot daemon is not currently running.")
        return True

    pid = status["pid"]
    print(f"⏳ Stopping Telegram Bot Daemon (PID: {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        # Wait up to 5s for clean shutdown
        for _ in range(25):
            time.sleep(0.2)
            if not is_pid_alive(pid):
                PID_FILE.unlink(missing_ok=True)
                print("✅ Telegram Bot Daemon stopped successfully.")
                return True

        # Force kill if still unresponsive
        print("⚠️ Process did not terminate in 5s. Sending SIGKILL...")
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
        PID_FILE.unlink(missing_ok=True)
        print("✅ Telegram Bot Daemon force terminated.")
        return True
    except Exception as e:
        print(f"❌ Error stopping daemon: {e}")
        return False


def restart_bot_daemon() -> bool:
    """Restart the background Telegram bot daemon."""
    stop_bot_daemon()
    time.sleep(1.0)
    return start_bot_daemon()
