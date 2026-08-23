from pathlib import Path
from core.config import OUTPUT_BASE

def ensure_output_dir(section: str) -> Path:
    """Create and return output directory for a section."""
    out_dir = OUTPUT_BASE / section
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir
