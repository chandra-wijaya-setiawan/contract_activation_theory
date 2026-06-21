"""Thin wrapper — delegates to src/experiment/extract_excerpts.py (canonical source)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from experiment.extract_excerpts import main
if __name__ == "__main__":
    main()
