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


def test_reads_reply_settings():
    defaults = Settings.from_env({})
    settings = Settings.from_env(
        {
            "JORGEX_SOCIAL_REPLY_POLL_SECONDS": "60",
            "JORGEX_SOCIAL_HOURLY_REPLY_CAP": "50",
            "JORGEX_SOCIAL_RETENTION_DAYS": "30",
            "JORGEX_SOCIAL_REPLY_COOLDOWN_MINUTES": "15",
        }
    )
    assert (defaults.reply_cooldown_minutes, settings.reply_cooldown_minutes) == (60, 15)

    assert (defaults.reply_poll_seconds, defaults.hourly_reply_cap, defaults.retention_days) == (
        120,
        200,
        90,
    )
    assert (settings.reply_poll_seconds, settings.hourly_reply_cap, settings.retention_days) == (
        60,
        50,
        30,
    )


def test_retention_must_outlast_the_private_reply_window():
    with pytest.raises(ValueError, match="at least 8"):
        Settings.from_env({"JORGEX_SOCIAL_RETENTION_DAYS": "7"})
    assert Settings.from_env({"JORGEX_SOCIAL_RETENTION_DAYS": "8"}).retention_days == 8
