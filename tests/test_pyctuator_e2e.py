import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Generator

import psutil
import pytest
import requests
from _pytest.monkeypatch import MonkeyPatch

from tests.conftest import Endpoints, ActuatorServer, RegistrationRequest, RegistrationTrackerFixture
from tests.fast_api_test_server import FastApiActuatorServer
from tests.flask_test_server import FlaskActuatorServer


@pytest.fixture(params=[FastApiActuatorServer, FlaskActuatorServer], ids=["FastAPI", "Flask"])
def actuator_server(request) -> Generator:  # type: ignore
    # Start a the web-server in which the actuator is integrated
    actuator_server: ActuatorServer = request.param()
    actuator_server.start()

    # Yield back to pytest until the module is done
    yield None

    # Once the module is done, stop the actuator-server
    actuator_server.stop()


@pytest.mark.usefixtures("boot_admin_server", "actuator_server")
@pytest.mark.mark_self_endpoint
def test_self_endpoint(endpoints: Endpoints) -> None:
    response = requests.get(endpoints.actuator)
    assert response.status_code == 200
    assert response.json()["_links"] is not None


@pytest.mark.usefixtures("boot_admin_server", "actuator_server")
@pytest.mark.mark_env_endpoint
def test_env_endpoint(endpoints: Endpoints) -> None:
    actual_key, actual_value = list(os.environ.items())[3]
    response = requests.get(endpoints.env)
    assert response.status_code == 200
    property_sources = response.json()["propertySources"]
    assert property_sources
    system_properties = [source for source in property_sources if source["name"] == "systemEnvironment"]
    assert system_properties
    assert system_properties[0]["properties"][actual_key]["value"] == actual_value

    # TODO should move to a dedicated test once info is implemented
    response = requests.get(endpoints.info)
    assert response.status_code == 200
    assert response.json()["app"] is not None


@pytest.mark.usefixtures("boot_admin_server", "actuator_server")
@pytest.mark.mark_builtin_health_endpoint
def test_health_endpoint(endpoints: Endpoints, monkeypatch: MonkeyPatch) -> None:
    # Verify that the diskSpace health check is returning some reasonable values
    response = requests.get(endpoints.health)
    assert response.status_code == 200
    assert response.json()["status"] == "UP"
    disk_space_health = response.json()["details"]["diskSpace"]
    assert disk_space_health["status"] == "UP"
    assert disk_space_health["details"]["free"] > 110000000

    # Now mock the results of psutil so it'll show very small amount of free space
    @dataclass
    class MockDiskUsage:
        total: int
        free: int

    def mock_disk_usage(path: str) -> MockDiskUsage:
        # pylint: disable=unused-argument
        return MockDiskUsage(100000000, 9999999)

    monkeypatch.setattr(psutil, "disk_usage", mock_disk_usage)
    response = requests.get(endpoints.health)
    assert response.status_code == 200
    assert response.json()["status"] == "DOWN"
    disk_space_health = response.json()["details"]["diskSpace"]
    assert disk_space_health["status"] == "DOWN"
    assert disk_space_health["details"]["free"] == 9999999
    assert disk_space_health["details"]["total"] == 100000000


@pytest.mark.usefixtures("boot_admin_server", "actuator_server")
@pytest.mark.mark_metrics_endpoint
def test_metrics_endpoint(endpoints: Endpoints) -> None:
    response = requests.get(endpoints.metrics)
    assert response.status_code == 200
    metric_names = response.json()["names"]
    assert "memory.rss" in metric_names
    assert "thread.count" in metric_names

    response = requests.get(f"{endpoints.metrics}/memory.rss")
    assert response.status_code == 200
    metric_json = response.json()
    assert metric_json["name"] == "memory.rss"
    assert metric_json["measurements"][0]["statistic"] == "VALUE"
    assert metric_json["measurements"][0]["value"] > 10000

    response = requests.get(f"{endpoints.metrics}/thread.count")
    assert response.status_code == 200
    metric_json = response.json()
    assert metric_json["name"] == "thread.count"
    assert metric_json["measurements"][0]["statistic"] == "COUNT"
    assert metric_json["measurements"][0]["value"] > 8


@pytest.mark.usefixtures("boot_admin_server", "actuator_server")
@pytest.mark.mark_recurring_registration
def test_recurring_registration(registration_tracker: RegistrationTrackerFixture) -> None:
    # Verify that at least 4 registrations occurred within 10 seconds since the test started
    start = time.time()
    while registration_tracker.count < 4:
        time.sleep(0.5)
        if time.time() - start > 15:
            pytest.fail(
                f"Expected at least 4 recurring registrations within 10 seconds but got {registration_tracker.count}")

    # Verify that the reported startup time is the same across all the registrations and that its later then the test's
    # start time
    assert isinstance(registration_tracker.registration, RegistrationRequest)
    assert registration_tracker.start_time == registration_tracker.registration.metadata["startup"]
    registration_start_time = datetime.fromisoformat(registration_tracker.start_time)
    assert registration_start_time > registration_tracker.test_start_time - timedelta(seconds=10)
