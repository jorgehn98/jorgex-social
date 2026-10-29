import pytest

from jorgex_social.config import Settings


def test_defaults_to_simulation():
    assert Settings.from_env({}).simulation is True


def test_reads_environment():
    settings = Settings.from_env(
        {"JORGEX_SOCIAL_DATABASE_PATH": "/data/x.db", "JORGEX_SOCIAL_SIMULATION": "false"}
    )

    assert str(settings.database_path) == "/data/x.db"
    assert settings.simulation is False


def test_rejects_ambiguous_flag():
    with pytest.raises(ValueError):
        Settings.from_env({"JORGEX_SOCIAL_SIMULATION": "maybe"})
