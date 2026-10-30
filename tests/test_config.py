import pytest

from jorgex_social.config import Settings


def test_defaults_to_simulation():
    assert Settings.from_env({}).simulation is True


def test_reads_environment():
    settings = Settings.from_env(
        {
            "JORGEX_SOCIAL_DATABASE_PATH": "/data/x.db",
            "JORGEX_SOCIAL_SIMULATION": "false",
            "JORGEX_SOCIAL_API_DOCS": "true",
        }
    )

    assert str(settings.database_path) == "/data/x.db"
    assert settings.simulation is False
    assert settings.api_docs is True


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
            "JORGEX_SOCIAL_HOURLY_PUBLIC_REPLY_CAP": "5",
            "JORGEX_SOCIAL_REPLIES_PER_MINUTE": "3",
            "JORGEX_SOCIAL_RETENTION_DAYS": "30",
            "JORGEX_SOCIAL_REPLY_COOLDOWN_MINUTES": "15",
        }
    )
    assert (defaults.reply_cooldown_minutes, settings.reply_cooldown_minutes) == (60, 15)
    assert (defaults.hourly_public_reply_cap, settings.hourly_public_reply_cap) == (30, 5)
    assert (defaults.replies_per_minute, settings.replies_per_minute) == (12, 3)

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


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, ("stop", "unsubscribe", "baja")),
        ({"JORGEX_SOCIAL_OPT_OUT_WORDS": " Halt , ,no more,"}, ("Halt", "no more")),
        ({"JORGEX_SOCIAL_OPT_OUT_WORDS": ""}, ()),
    ],
)
def test_reads_opt_out_words(env, expected):
    assert Settings.from_env(env).opt_out_words == expected


def test_retention_must_outlast_the_private_reply_window():
    with pytest.raises(ValueError, match="at least 8"):
        Settings.from_env({"JORGEX_SOCIAL_RETENTION_DAYS": "7"})
    assert Settings.from_env({"JORGEX_SOCIAL_RETENTION_DAYS": "8"}).retention_days == 8


def test_reads_webhook_settings_and_keeps_secrets_out_of_repr():
    settings = Settings.from_env(
        {
            "JORGEX_SOCIAL_WEBHOOK_VERIFY_TOKEN": " verify-QZX ",
            "JORGEX_SOCIAL_META_APP_SECRET": "secret-QZX",
            "JORGEX_SOCIAL_HUMAN_PAUSE_MINUTES": "45",
        }
    )

    assert settings.webhook_enabled is True
    assert settings.webhook_verify_token == "verify-QZX"
    assert settings.human_pause_minutes == 45
    assert "QZX" not in repr(settings)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"JORGEX_SOCIAL_WEBHOOK_VERIFY_TOKEN": "t"},
        {"JORGEX_SOCIAL_META_APP_SECRET": "s"},
        {"JORGEX_SOCIAL_WEBHOOK_VERIFY_TOKEN": " ", "JORGEX_SOCIAL_META_APP_SECRET": "s"},
    ],
)
def test_the_webhook_needs_both_secrets(env):
    assert Settings.from_env(env).webhook_enabled is False


def test_reads_exposure_settings():
    defaults = Settings.from_env({})
    settings = Settings.from_env(
        {
            "JORGEX_SOCIAL_FORWARDED_ALLOW_IPS": "172.30.0.1",
            "JORGEX_SOCIAL_LOGIN_LIMIT_PER_MINUTE": "3",
            "JORGEX_SOCIAL_LOGIN_LIMIT_PER_HOUR": "10",
        }
    )

    assert defaults.forwarded_allow_ips == "127.0.0.1,::1"
    assert settings.forwarded_allow_ips == "172.30.0.1"
    assert (defaults.login_limit_per_minute, defaults.login_limit_per_hour) == (5, 20)
    assert (settings.login_limit_per_minute, settings.login_limit_per_hour) == (3, 10)


def test_a_login_limit_of_zero_is_refused():
    # Zero would refuse every sign-in, the operator's included.
    with pytest.raises(ValueError, match="at least 1"):
        Settings.from_env({"JORGEX_SOCIAL_LOGIN_LIMIT_PER_HOUR": "0"})


@pytest.mark.parametrize("name", ["REPLIES_PER_MINUTE", "HOURLY_PUBLIC_REPLY_CAP"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_reply_limit_below_one_is_refused(name, value):
    # Zero would silently stop the replies it limits.
    with pytest.raises(ValueError, match=f"JORGEX_SOCIAL_{name} must be at least 1"):
        Settings.from_env({f"JORGEX_SOCIAL_{name}": value})


def test_oauth_redirect_uris_are_https_only():
    listed = {"JORGEX_SOCIAL_OAUTH_REDIRECT_URIS": "https://a.example/cb, https://b.example/cb"}

    assert Settings.from_env({}).oauth_redirect_uris == ()
    assert Settings.from_env(listed).oauth_redirect_uris == (
        "https://a.example/cb",
        "https://b.example/cb",
    )
    # As the SDK parses what a client registers.
    loose = {"JORGEX_SOCIAL_OAUTH_REDIRECT_URIS": "HTTPS://A.Example:443/cb, https://b.example"}
    assert Settings.from_env(loose).oauth_redirect_uris == (
        "https://a.example/cb",
        "https://b.example",
    )
    with pytest.raises(ValueError, match="https URIs only"):
        Settings.from_env({"JORGEX_SOCIAL_OAUTH_REDIRECT_URIS": "http://a.example/cb"})
