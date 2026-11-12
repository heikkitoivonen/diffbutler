"""Successful Git transports are remembered without losing user configuration."""

import asyncio
from pathlib import Path

import pytest

from diffbutler.config.settings import ConfigError, load_settings
from diffbutler.config.transport import save_git_transport


def test_transport_persists_in_selected_config_and_preserves_comments(tmp_path: Path) -> None:
    path = tmp_path / "custom.toml"
    path.write_text('# Keep this\n[repos."acme/api"]\npath = "../checkout" # local source\n')
    path.chmod(0o640)
    settings = load_settings(path)
    assert settings.config_file == path
    assert settings.repos["acme/api"].git_transport == "auto"
    save_git_transport(path, "acme/api", "ssh", expected="auto")
    reloaded = load_settings(path)
    assert reloaded.repos["acme/api"].git_transport == "ssh"
    assert reloaded.repos["acme/api"].path == tmp_path / "../checkout"
    assert "# Keep this" in path.read_text() and "# local source" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o640
    assert "config_file" not in reloaded.model_dump()


def test_transport_changes_only_when_expected_and_supports_new_repositories(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save_git_transport(path, "acme/api", "ssh", expected="auto")
    original = path.read_text()
    save_git_transport(path, "acme/api", "https", expected="auto")
    assert path.read_text() == original
    save_git_transport(path, "acme/api", "https", expected="ssh")
    save_git_transport(path, "acme/other", "ssh", expected="auto")
    settings = load_settings(path)
    assert settings.repos["acme/api"].git_transport == "https"
    assert settings.repos["acme/other"].git_transport == "ssh"


@pytest.mark.asyncio
async def test_concurrent_saves_preserve_both_repositories(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    await asyncio.gather(
        asyncio.to_thread(save_git_transport, path, "acme/api", "ssh", expected="auto"),
        asyncio.to_thread(save_git_transport, path, "acme/other", "https", expected="auto"),
    )
    settings = load_settings(path)
    assert settings.repos["acme/api"].git_transport == "ssh"
    assert settings.repos["acme/other"].git_transport == "https"


def test_invalid_configuration_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("invalid = [")
    with pytest.raises(ConfigError):
        save_git_transport(path, "acme/api", "ssh", expected="auto")
    assert path.read_text() == "invalid = ["
    path.write_text('[repos."acme/api"]\ngit_transport = "ftp"\n')
    with pytest.raises(ConfigError, match="git_transport"):
        load_settings(path)


def test_transport_reuses_a_repository_key_written_in_another_case(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[repos."Acme/API"]\nenabled = false\n')
    save_git_transport(path, "acme/api", "ssh", expected="auto")
    assert path.read_text() == '[repos."Acme/API"]\nenabled = false\ngit_transport = "ssh"\n'
    repository = load_settings(path).repos["acme/api"]
    assert repository.enabled is False and repository.git_transport == "ssh"


def test_transport_keeps_case_variant_entries_consistent(tmp_path: Path) -> None:
    # Earlier releases wrote the lowercase key beside a user's mixed-case table.
    path = tmp_path / "config.toml"
    path.write_text(
        '[repos."Acme/API"]\ngit_transport = "https"\n[repos."acme/api"]\ngit_transport = "ssh"\n'
    )
    assert load_settings(path).repos["acme/api"].git_transport == "ssh"
    save_git_transport(path, "acme/api", "https", expected="ssh")
    assert load_settings(path).repos["acme/api"].git_transport == "https"
    assert path.read_text().count('git_transport = "https"') == 2
