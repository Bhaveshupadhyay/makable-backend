"""Cross-cutting behaviour: health probes, envelopes, request ids and security headers."""

from fastapi.testclient import TestClient


def test_liveness_and_readiness(client: TestClient) -> None:
    for path in ("/api/v1/health/live", "/api/v1/health/ready"):
        res = client.get(path)
        assert res.status_code == 200
        assert res.json() == {"success": True, "data": {"status": "ok"}}


def test_incoming_request_id_is_echoed(client: TestClient) -> None:
    res = client.get("/api/v1/health/live", headers={"X-Request-ID": "abc-123"})
    assert res.headers["x-request-id"] == "abc-123"


def test_malformed_request_id_is_replaced(client: TestClient) -> None:
    res = client.get("/api/v1/health/live", headers={"X-Request-ID": "bad id\x7f"})
    assert res.headers["x-request-id"] != "bad id\x7f"


def test_security_headers(client: TestClient) -> None:
    res = client.get("/api/v1/health/live")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"


def test_unknown_route_uses_the_error_envelope(client: TestClient) -> None:
    res = client.get("/api/v1/nope")
    assert res.status_code == 404
    assert res.json()["success"] is False
    assert res.json()["error"]["code"] == "http_error"


def test_validation_errors_use_the_error_envelope(client: TestClient) -> None:
    res = client.get("/api/v1/auth/github/login", params={"returnTo": "/" + "a" * 3000})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "validation_error"
