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


def test_token_is_not_in_the_repr():
    settings = Settings.from_env({"JORGEX_SOCIAL_INSTAGRAM_ACCESS_TOKEN": "secret-value"})

    assert "secret-value" not in repr(settings)


def test_real_adapter_needs_credentials():
    from jorgex_social.clock import SystemClock
    from jorgex_social.instagram import make_instagram

    with pytest.raises(RuntimeError):
        make_instagram(Settings(simulation=False), SystemClock())


def test_reads_publishing_settings():
    settings = Settings.from_env(
        {
            "JORGEX_SOCIAL_MEDIA_DIR": "/data/media",
            "JORGEX_SOCIAL_PUBLIC_URL": "https://social.example.com/",
            "JORGEX_SOCIAL_PUBLISH_GRACE_MINUTES": "45",
        }
    )

    assert str(settings.media_dir) == "/data/media"
    assert settings.public_url == "https://social.example.com"
    assert settings.publish_grace_minutes == 45
    assert Settings.from_env({}).public_url is None
