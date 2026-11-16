"""T08b (docs/test-plan.md): the T01 flow plus one fault, judged against expected.json in this folder.
The fault and the conditions it meets: ../tamper.py; how to run: ../SETUP_T05_T08.md."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tamper import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main("T08b"))
