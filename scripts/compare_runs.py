"""Thin wrapper — delegates to src/experiment/compare_runs.py (canonical source)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from experiment.compare_runs import main
if __name__ == "__main__":
    main()
