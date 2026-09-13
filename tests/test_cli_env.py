"""The dotenv loader — precedence and non-clobbering."""

from pathlib import Path

from ctrl_a_jr.cli import load_dotenv


def test_env_local_wins_over_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("K=from_env\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("K=from_local\n", encoding="utf-8")
    monkeypatch.delenv("K", raising=False)
    load_dotenv((tmp_path / ".env.local", tmp_path / ".env"))
    import os
    assert os.environ["K"] == "from_local"


def test_the_real_environment_beats_both_files(tmp_path, monkeypatch):
    """A shell export must not be silently overwritten by a stale file."""
    (tmp_path / ".env.local").write_text("K=from_local\n", encoding="utf-8")
    monkeypatch.setenv("K", "from_shell")
    load_dotenv((tmp_path / ".env.local",))
    import os
    assert os.environ["K"] == "from_shell"


def test_a_missing_file_is_not_an_error(tmp_path):
    load_dotenv((tmp_path / "nope.env",))


def test_comments_and_blank_lines_are_skipped(tmp_path, monkeypatch):
    (tmp_path / ".env.local").write_text("# a comment\n\nK2=v\n", encoding="utf-8")
    monkeypatch.delenv("K2", raising=False)
    load_dotenv((tmp_path / ".env.local",))
    import os
    assert os.environ["K2"] == "v"


def test_quotes_are_stripped(tmp_path, monkeypatch):
    (tmp_path / ".env.local").write_text('K3="quoted"\n', encoding="utf-8")
    monkeypatch.delenv("K3", raising=False)
    load_dotenv((tmp_path / ".env.local",))
    import os
    assert os.environ["K3"] == "quoted"


def test_gitignore_covers_env_local():
    """A credential file that git can see is a credential file that gets pushed."""
    patterns = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(p.strip() in (".env.*", ".env.local") for p in patterns)
