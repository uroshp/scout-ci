"""Print the research layer digest (scout/research.py). $0."""
import sys
sys.path.insert(0, ".")
from scout import research  # noqa: E402

if __name__ == "__main__":
    print(research.render(research.report()))
