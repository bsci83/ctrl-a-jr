"""Write a run report to local disk. Confined to one directory."""

from __future__ import annotations

from pathlib import Path

from ..registry import Registry, ToolSpec
from ..types import ToolResult


def register_report_tools(registry: Registry, out_dir: Path) -> None:
    root = Path(out_dir).resolve()

    def write_report(filename: str, content: str) -> ToolResult:
        try:
            target = (root / filename).resolve()
            # Path confinement: the model does not get to choose where writes land.
            if not target.is_relative_to(root):
                return ToolResult(False, "", f"refusing to write outside {root}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return ToolResult(True, f"wrote {target.name} ({len(content)} chars)")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False, "", str(exc))

    registry.register(ToolSpec(
        name="write_report",
        description="Write the run summary to a local markdown file.",
        schema={"type": "object",
                "properties": {"filename": {"type": "string"}, "content": {"type": "string"}},
                "required": ["filename", "content"]},
        mutating=True,
        run=write_report,
        render=lambda filename, content: f"Write {filename}:\n\n{content}",
    ))
