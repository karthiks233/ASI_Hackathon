import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")
DATA_BUNDLE_DIR: Path = Path(os.getenv("DATA_BUNDLE_DIR", "data"))
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")
CAPACITY_SCALE: float = float(os.getenv("CAPACITY_SCALE", "0.3"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# Simulation constants
VISIT_SAMPLE_MIN: int = 2          # sample interval when building sector visit timelines
SIM_STEP_MIN: int = 5              # frame step exposed to the UI
SIM_CACHE_DIR: Path = Path(".sim_cache")
MAX_COPILOT_ROUNDS: int = 5        # max tool-use rounds per suggest call

# Weather conflict thresholds (per data spec)
REFC_THRESHOLD_DBZ: float = 40.0

# Reroute lateral offset (degrees lat/lon)
REROUTE_OFFSET_DEG: float = 1.5
