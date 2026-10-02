"""Reviewer readiness is handled before entering the live dashboard."""

import sys
import tomllib
from pathlib import Path

import pytest

from diffbutler.config.settings import ConfigError, Reviewer, load_settings
from diffbutler.core.setup import check_reviewer, startup_settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("available", "default"),
    [
        (("codex", "claude"), "codex"),
        (("codex", "claude"), "claude"),
        (("codex",), "codex"),
        (("claude",), "claude"),
        ((), "codex"),
        ((), "claude"),
    ],
)
async def test_new_config_detects_agents_and_comments_missing_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available: tuple[str, ...], default: str
) -> None:
    path = tmp_path / "config.toml"
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    marker = tmp_path / "launched"
    for command in (*available, "git", "custom-agent"):
        executable = bin_path / command
        executable.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n"
        )
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_path))

    async def no_repository(directory: Path) -> None:
        return None

    async def public(base_url: str, repository: str) -> bool:
        return True

    async def validate(base_url: str, repository: str, login: str, token: str | None) -> str:
        return login

    monkeypatch.setattr("diffbutler.core.setup.discover_repository", no_repository)
    monkeypatch.setattr("diffbutler.core.setup.repository_is_public", public)
    monkeypatch.setattr("diffbutler.core.setup.validate_access", validate)
    answers = iter(["github", "acme/api", "anonymous", "reader"])
    reviewer_questions: list[str] = []

    async def ask(label: str, suggested: str) -> str:
        assert not path.exists()
        if "reviewer" in label.lower():
            reviewer_questions.append(label)
            if available:
                assert label == f"Default AI reviewer ({', '.join(available)})"
                assert suggested == available[0]
            return "custom-agent" if label.startswith("Reviewer executable") else default
        return next(answers)

    settings = await startup_settings(path, directory=tmp_path, ask=ask)
    assert len(reviewer_questions) == (1 if available else 2)
    assert settings.review.default_reviewer == default
    assert [reviewer.id for reviewer in settings.reviewers] == list(available or (default,))
    for reviewer in settings.reviewers:
        assert reviewer.preset == reviewer.id
        assert reviewer.command == [str(bin_path / (reviewer.id if available else "custom-agent"))]
    content = path.read_text()
    missing = {"codex", "claude"} - {reviewer.id for reviewer in settings.reviewers}
    assert content.count("# [[reviewers]]") == len(missing)
    for name in missing:
        assert f'# id = "{name}"' in content
        assert f'# preset = "{name}"' in content
        assert f'# command = ["{name}"]' in content
    uncommented = "\n".join(
        line.removeprefix("# ")
        if line.startswith(("# [[", "# id", "# preset", "# command"))
        else line
        for line in content.splitlines()
    )
    assert {entry["id"] for entry in tomllib.loads(uncommented)["reviewers"]} == {"codex", "claude"}
    again = await startup_settings(path, directory=tmp_path, ask=None)
    assert again.model_dump() == settings.model_dump()
    assert path.read_text() == content
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["codex", "claude"])
async def test_existing_forge_gets_reviewer_setup_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str
) -> None:
    path = tmp_path / "config.toml"
    original = (
        '# keep\nreviewers=[]\n[[forges]]\nkind="github"\nbase_url="https://api.github.com"\n'
    )
    path.write_text(original)
    answers = iter([preset, sys.executable])
    checked: list[Reviewer] = []

    async def ask(label: str, default: str) -> str:
        return next(answers)

    async def check(reviewer: Reviewer, directory: Path) -> None:
        checked.append(reviewer)
        assert path.read_text() == original  # Save only after validation.

    monkeypatch.setattr("diffbutler.core.setup.check_reviewer", check)
    settings = await startup_settings(path, directory=tmp_path, ask=ask)
    assert settings.review.default_reviewer == preset
    assert settings.reviewers[0].preset == preset
    assert len(checked) == 1 and "# keep" in path.read_text()
    again = await startup_settings(path, directory=tmp_path, ask=None)
    assert again.model_dump() == settings.model_dump() and len(checked) == 1


@pytest.mark.asyncio
async def test_existing_reviewer_can_be_selected_without_overwriting_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[[forges]]\nkind="github"\nbase_url="https://api.github.com"\n'
        '[[reviewers]]\nid="custom"\npreset="codex"\ncommand=["custom-codex", "--flag"] # keep\n'
    )

    async def ask(label: str, default: str) -> str:
        assert default == "custom"
        return default

    async def check(reviewer: Reviewer, directory: Path) -> None:
        assert reviewer.command == ["custom-codex", "--flag"]

    monkeypatch.setattr("diffbutler.core.setup.check_reviewer", check)
    settings = await startup_settings(path, directory=tmp_path, ask=ask)
    assert settings.review.default_reviewer == "custom"
    assert len(settings.reviewers) == 1 and "# keep" in path.read_text()


@pytest.mark.asyncio
async def test_failed_reviewer_check_preserves_existing_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = '[[forges]]\nkind="github"\nbase_url="https://api.github.com"\n'
    path.write_text(original)
    answers = iter(["codex", sys.executable])

    async def ask(label: str, default: str) -> str:
        return next(answers)

    async def fail(*args: object) -> None:
        raise ConfigError("Reviewer executable was not found")

    monkeypatch.setattr("diffbutler.core.setup.check_reviewer", fail)
    with pytest.raises(ConfigError, match="executable"):
        await startup_settings(path, directory=tmp_path, ask=ask)
    assert path.read_text() == original
    assert not load_settings(path).reviewers


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["codex", "claude"])
async def test_setup_does_not_launch_reviewer(tmp_path: Path, preset: str) -> None:
    marker = tmp_path / "launched"
    script = tmp_path / "reviewer.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    reviewer = Reviewer(
        id=preset,
        preset="codex" if preset == "codex" else "claude",
        command=[sys.executable, str(script)],
    )
    await check_reviewer(reviewer, tmp_path)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_missing_executable_is_reported_before_dashboard(tmp_path: Path) -> None:
    reviewer = Reviewer(id="codex", preset="codex", command=["/nonexistent/diffbutler-test-agent"])
    with pytest.raises(ConfigError, match="executable was not found"):
        await check_reviewer(reviewer, tmp_path)
