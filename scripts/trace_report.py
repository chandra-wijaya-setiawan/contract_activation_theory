"""Thin wrapper — delegates to src/experiment/trace_report.py (canonical source)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from experiment.trace_report import main
if __name__ == "__main__":
    main()
