import sys
from pathlib import Path

# Корень проекта — чтобы тесты импортировали agent/ и browser/ как в main.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
