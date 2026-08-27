from __future__ import annotations

import ast
from pathlib import Path

_AGENTIC_PACKAGES = (
    "anki_miner.agent",
    "anki_miner.headless",
    "anki_miner.learner",
    "anki_miner.mcp_server",
    "anki_miner.runtime",
)
_AGENTIC_ROOTS = {name.rsplit(".", 1)[-1] for name in _AGENTIC_PACKAGES}


def test_inherited_core_does_not_import_agentic_layers():
    package_root = Path(__file__).parents[3] / "anki_miner"
    violations: list[str] = []

    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root)
        if relative.parts[0] in _AGENTIC_ROOTS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for imported in names:
                if imported.startswith(_AGENTIC_PACKAGES):
                    violations.append(f"{relative}:{node.lineno}: {imported}")

    assert violations == []
