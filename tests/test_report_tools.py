from ctrl_a_jr.registry import Registry
from ctrl_a_jr.tools import report_tools


def test_write_report_creates_file(tmp_path):
    reg = Registry()
    report_tools.register_report_tools(reg, tmp_path)
    out = reg.get("write_report").run(filename="run.md", content="# Recovered\n2 of 3")
    assert out.ok
    assert (tmp_path / "run.md").read_text(encoding="utf-8").startswith("# Recovered")


def test_write_report_is_mutating(tmp_path):
    reg = Registry()
    report_tools.register_report_tools(reg, tmp_path)
    assert reg.get("write_report").mutating is True


def test_path_escape_is_refused(tmp_path):
    reg = Registry()
    report_tools.register_report_tools(reg, tmp_path)
    out = reg.get("write_report").run(filename="../../escaped.md", content="x")
    assert out.ok is False
    assert "outside" in (out.error or "").lower()
