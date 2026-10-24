import ast
from pathlib import Path

from simulator.baselines import NoAction

ROOT = Path(__file__).resolve().parent.parent


def test_advisor_never_imports_simulator():
    for f in (ROOT / "your_project").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(n.split(".")[0] == "simulator" for n in names), f


def test_no_action():
    assert NoAction().advise({}) == []
