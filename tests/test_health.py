from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from jorgex_social.api import create_app


def test_health_reports_ok_and_simulation(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "simulation": True}


def test_health_answers_head(client):
    assert client.head("/health").status_code == 200


def test_api_docs_are_served_in_simulation(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200


@pytest.mark.parametrize(("flag", "served"), [(False, False), (True, True)])
def test_api_docs_outside_simulation_only_when_enabled(settings, flag, served):
    client = TestClient(create_app(replace(settings, simulation=False, api_docs=flag)))

    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == (200 if served else 404)
