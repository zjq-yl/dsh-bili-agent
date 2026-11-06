"""Allow stdlib unittest to test the local src tree without installing a package."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
