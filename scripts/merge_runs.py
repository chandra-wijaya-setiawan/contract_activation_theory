"""Thin wrapper — delegates to src/experiment/merge_runs.py (canonical source)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from experiment.merge_runs import main
if __name__ == "__main__":
    main()
