"""IsolationScanner: an Evaluator-side safeguard over each Run's artifacts (Req 24.6, design "Canary checks").

It looks for the Answer_Key canary and the Answer_Key_Root path (as configured and as resolved), case
insensitively, in prompts and output files. The test suite runs the stricter checks; this one makes a
leak in a real evaluation visible in the Results_Table.
"""

from collections.abc import Iterable
from pathlib import Path


class IsolationScanner:
    def __init__(self, canary: str, answer_key_root: Path):
        root = Path(answer_key_root)
        candidates = (("canary", canary), ("answer_key_root", str(root)), ("answer_key_root", str(root.resolve())))
        self._needles: dict[str, str] = {}  # needle -> name, so equal paths are reported once
        for name, value in candidates:
            if value and value.lower() not in self._needles:
                self._needles[value.lower()] = name

    def scan(self, texts: Iterable[tuple[str, str]]) -> list[str]:
        """``texts`` are ``(label, text)`` pairs; returns one message per needle found in each text."""
        found = []
        for label, text in texts:
            lowered = text.lower()
            names = {name for needle, name in self._needles.items() if needle in lowered}
            found.extend(f"{name} found in {label}" for name in sorted(names))
        return found
