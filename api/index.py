"""Vercel serverless entry point — re-exports the FastAPI app."""

import sys
from pathlib import Path

# Ensure project root is on the path so `scripts` package resolves
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402, F401
