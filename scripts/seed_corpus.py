"""Thin wrapper — delegates to src/experiment/seed_corpus.py (canonical source)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from experiment.seed_corpus import main
if __name__ == "__main__":
    main()
