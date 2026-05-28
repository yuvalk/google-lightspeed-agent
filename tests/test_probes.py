"""Tests for the health/readiness probe server."""

from fastapi.testclient import TestClient

from lightspeed_agent.probes.server import create_probe_app


class TestProbeHealthEndpoint:
    """Tests for the /health (liveness) endpoint."""

    def test_health_endpoint_returns_200(self):
        """Test GET /health returns 200 with healthy status."""
        app = create_probe_app(service_name="test-agent")
        client = TestClient(app)

        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    def test_health_endpoint_includes_service_name(self):
        """Test that the service name passed to create_probe_app appears in /health response."""
        app = create_probe_app(service_name="my-custom-service")
        client = TestClient(app)

        response = client.get("/health")

        data = response.json()
        assert data["service"] == "my-custom-service"

    def test_health_endpoint_unaffected_by_readiness_checks(self):
        """Test /health stays 200 even when readiness checks are configured."""
        async def _failing_check() -> None:
            raise RuntimeError("dependency down")

        app = create_probe_app(
            service_name="test-agent",
            readiness_checks={"broken": _failing_check},
        )
        client = TestClient(app)

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"


class TestProbeReadyEndpointNoChecks:
    """Tests for /ready when no readiness checks are registered."""

    def test_ready_endpoint_returns_200(self):
        """Test GET /ready returns 200 with ready status when no checks configured."""
        app = create_probe_app(service_name="test-agent")
        client = TestClient(app)

        response = client.get("/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"

    def test_ready_endpoint_includes_service_name(self):
        """Test that the service name appears in /ready response."""
        app = create_probe_app(service_name="my-custom-service")
        client = TestClient(app)

        response = client.get("/ready")

        data = response.json()
        assert data["service"] == "my-custom-service"

    def test_ready_no_checks_key_when_none_registered(self):
        """Test /ready response has no 'checks' key when no checks are registered."""
        app = create_probe_app(service_name="test-agent")
        client = TestClient(app)

        response = client.get("/ready")

        data = response.json()
        assert "checks" not in data


class TestProbeReadyEndpointWithChecks:
    """Tests for /ready when readiness checks are registered."""

    def test_all_checks_pass_returns_200(self):
        """Test /ready returns 200 when all checks pass."""
        async def _ok_check() -> None:
            pass

        app = create_probe_app(
            service_name="test-agent",
            readiness_checks={"database": _ok_check, "redis": _ok_check},
        )
        client = TestClient(app)

        response = client.get("/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["redis"] == "ok"

    def test_one_check_fails_returns_503(self):
        """Test /ready returns 503 when one check fails."""
        async def _ok_check() -> None:
            pass

        async def _failing_check() -> None:
            raise ConnectionError("connection refused")

        app = create_probe_app(
            service_name="test-agent",
            readiness_checks={"database": _ok_check, "redis": _failing_check},
        )
        client = TestClient(app)

        response = client.get("/ready")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"
        assert data["checks"]["database"] == "ok"
        assert "failed:" in data["checks"]["redis"]
        assert "connection refused" in data["checks"]["redis"]

    def test_all_checks_fail_returns_503(self):
        """Test /ready returns 503 when all checks fail."""
        async def _fail_db() -> None:
            raise RuntimeError("db unreachable")

        async def _fail_redis() -> None:
            raise TimeoutError("redis timeout")

        app = create_probe_app(
            service_name="test-agent",
            readiness_checks={"database": _fail_db, "redis": _fail_redis},
        )
        client = TestClient(app)

        response = client.get("/ready")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"
        assert "failed:" in data["checks"]["database"]
        assert "failed:" in data["checks"]["redis"]

    def test_checks_do_not_short_circuit(self):
        """Test all checks run even if the first one fails."""
        call_log: list[str] = []

        async def _fail_first() -> None:
            call_log.append("first")
            raise RuntimeError("first failed")

        async def _ok_second() -> None:
            call_log.append("second")

        app = create_probe_app(
            service_name="test-agent",
            readiness_checks={"first": _fail_first, "second": _ok_second},
        )
        client = TestClient(app)

        client.get("/ready")

        assert "first" in call_log
        assert "second" in call_log

    def test_ready_includes_service_name_with_checks(self):
        """Test /ready response includes service name when checks are present."""
        async def _ok() -> None:
            pass

        app = create_probe_app(
            service_name="my-agent",
            readiness_checks={"db": _ok},
        )
        client = TestClient(app)

        response = client.get("/ready")

        assert response.json()["service"] == "my-agent"


class TestProbeAppGeneral:
    """General probe app tests."""

    def test_probe_app_has_no_other_routes(self):
        """Test that GET to a random path returns 404 (no unexpected routes)."""
        app = create_probe_app(service_name="test-agent")
        client = TestClient(app)

        response = client.get("/nonexistent")

        assert response.status_code == 404

    def test_probe_app_different_service_names(self):
        """Test that different service names produce correct responses."""
        app_a = create_probe_app(service_name="agent-service")
        app_b = create_probe_app(service_name="marketplace-service")
        client_a = TestClient(app_a)
        client_b = TestClient(app_b)

        health_a = client_a.get("/health").json()
        health_b = client_b.get("/health").json()

        assert health_a["service"] == "agent-service"
        assert health_b["service"] == "marketplace-service"
        assert health_a["service"] != health_b["service"]
