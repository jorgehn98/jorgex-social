import pytest
from fastapi.testclient import TestClient

from jorgex_social.api import create_app
from jorgex_social.cli import migrate
from jorgex_social.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    settings = Settings(
        database_path=tmp_path / "jorgex_social.db",
        media_dir=tmp_path / "media",
        secret_key_file=tmp_path / "secret.key",
    )
    migrate(settings)
    return settings


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)
