from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.dependencies import get_model_client, get_model_client_factory
from app.clients.model import AiKeyInvalidError, ModelClient
from app.core.client import get_postgres_client
from app.core.config import Settings
from app.core.security import TokenCipher
from app.models.ai_key import AiKey
from app.schemas.ai_key import ModelEndpoint
from app.schemas.chat import ChatMessage
from app.tests.ai_edit_fixtures import GOOD_REPLY, request_body
from app.tests.api.test_auth import sign_in
from app.tests.fakes import VALID_AI_KEY, FakeAiProviders, FakeModelClient

AI = "/api/v1/ai"


class RefusingModel:
    name = "refusing"

    async def complete(self, messages: list[ChatMessage]) -> str:
        raise AiKeyInvalidError()


@pytest.fixture
def models(client: TestClient) -> dict[str, Any]:
    """The default model and the clients built for users' picks, with the endpoints they were built for."""
    built: dict[str, Any] = {"default": FakeModelClient(), "user": FakeModelClient(), "endpoints": []}

    def factory(endpoint: ModelEndpoint) -> ModelClient:
        built["endpoints"].append(endpoint)
        model: ModelClient = built["user"]
        return model

    client.app.dependency_overrides[get_model_client] = lambda: built["default"]  # type: ignore[attr-defined]
    client.app.dependency_overrides[get_model_client_factory] = lambda: factory  # type: ignore[attr-defined]
    return built


def add_key(client: TestClient, **body: Any) -> Any:
    return client.post(f"{AI}/keys", json={"provider": "openai", "apiKey": VALID_AI_KEY, **body})


async def stored_keys(settings: Settings) -> list[AiKey]:
    async with get_postgres_client()() as session:
        return list(await session.scalars(select(AiKey)))


def test_needs_a_signed_in_user(client: TestClient) -> None:
    for res in (client.get(f"{AI}/keys"), add_key(client), client.get(f"{AI}/models")):
        assert res.status_code == 401


def test_lists_the_fixed_providers(client: TestClient) -> None:
    sign_in(client)

    providers = client.get(f"{AI}/providers").json()["data"]

    assert {"id": "openai", "name": "OpenAI", "keysUrl": "https://platform.openai.com/api-keys"} in providers
    assert len(providers) == 8


async def test_a_checked_key_is_stored_encrypted_and_only_its_end_is_shown(
    client: TestClient, ai_providers: FakeAiProviders, settings: Settings
) -> None:
    sign_in(client)

    res = add_key(client, models=["gpt-5", "not-listed"])

    assert res.status_code == 201
    data = res.json()["data"]
    assert data["availableModels"] == ["gpt-4.1-mini", "gpt-5", "o4-mini"]
    key = data["key"]
    assert (key["provider"], key["keyHint"], key["status"]) == ("openai", "1234", "valid")
    assert key["models"] == ["gpt-5"]  # Only models the key can use.
    assert VALID_AI_KEY not in res.text
    assert ai_providers.calls == [("openai", VALID_AI_KEY)]
    [row] = await stored_keys(settings)
    assert row.api_key != VALID_AI_KEY
    assert TokenCipher(settings.token_encryption_key.get_secret_value()).decrypt(row.api_key) == VALID_AI_KEY
    assert client.get(f"{AI}/keys").json()["data"] == [key]


async def test_a_refused_key_is_not_stored(client: TestClient, settings: Settings) -> None:
    sign_in(client)

    res = client.post(f"{AI}/keys", json={"provider": "openai", "apiKey": "sk-wrong-key"})

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "ai_key_invalid"
    assert await stored_keys(settings) == []


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "somewhere", "apiKey": VALID_AI_KEY},
        {"provider": "openai", "apiKey": "short"},
        {"provider": "openai", "apiKey": "has a space in it"},
        {"provider": "openai", "apiKey": VALID_AI_KEY, "models": ["bad model id"]},
    ],
)
def test_rejects_invalid_keys_and_model_ids(client: TestClient, body: dict[str, Any]) -> None:
    sign_in(client)

    assert client.post(f"{AI}/keys", json=body).status_code == 422


def test_a_new_key_replaces_the_providers_key_and_keeps_its_models(client: TestClient) -> None:
    sign_in(client)
    first = add_key(client, models=["gpt-5"]).json()["data"]["key"]

    second = add_key(client).json()["data"]["key"]

    assert second["id"] == first["id"]
    assert second["models"] == ["gpt-5"]
    assert len(client.get(f"{AI}/keys").json()["data"]) == 1


def test_models_are_picked_and_shown_in_the_menu(client: TestClient, models: dict[str, Any]) -> None:
    sign_in(client)
    key = add_key(client).json()["data"]["key"]

    res = client.patch(f"{AI}/keys/{key['id']}", json={"models": ["o4-mini", "gpt-5", "o4-mini"]})

    assert res.json()["data"]["models"] == ["o4-mini", "gpt-5"]
    assert client.get(f"{AI}/models").json()["data"] == [
        {"keyId": None, "provider": None, "model": "fake-model"},
        {"keyId": key["id"], "provider": "openai", "model": "o4-mini"},
        {"keyId": key["id"], "provider": "openai", "model": "gpt-5"},
    ]


def test_a_check_drops_models_the_provider_no_longer_lists(client: TestClient, ai_providers: FakeAiProviders) -> None:
    sign_in(client)
    key = add_key(client, models=["gpt-5", "o4-mini"]).json()["data"]["key"]
    ai_providers.models = ["gpt-5"]

    checked = client.post(f"{AI}/keys/{key['id']}/check").json()["data"]

    assert (checked["key"]["status"], checked["key"]["models"]) == ("valid", ["gpt-5"])


def test_a_check_marks_a_refused_key_invalid_and_the_menu_drops_it(
    client: TestClient, ai_providers: FakeAiProviders, models: dict[str, Any]
) -> None:
    sign_in(client)
    key = add_key(client, models=["gpt-5"]).json()["data"]["key"]

    ok = client.post(f"{AI}/keys/{key['id']}/check").json()["data"]
    assert (ok["key"]["status"], ok["availableModels"]) == ("valid", ai_providers.models)

    ai_providers.calls.clear()
    # The key gets revoked at the provider.
    ai_providers.models = []
    original = ai_providers.list_models

    async def refuse(provider: Any, api_key: str) -> list[str]:
        raise AiKeyInvalidError()

    ai_providers.list_models = refuse  # type: ignore[method-assign]
    res = client.post(f"{AI}/keys/{key['id']}/check")
    ai_providers.list_models = original  # type: ignore[method-assign]

    assert res.status_code == 200
    assert (res.json()["data"]["key"]["status"], res.json()["data"]["availableModels"]) == ("invalid", [])
    assert [o["keyId"] for o in client.get(f"{AI}/models").json()["data"]] == [None]


def test_an_unknown_key_is_not_found(client: TestClient) -> None:
    sign_in(client)
    missing = "00000000-0000-0000-0000-000000000000"

    for res in (
        client.delete(f"{AI}/keys/{missing}"),
        client.patch(f"{AI}/keys/{missing}", json={"models": []}),
        client.post(f"{AI}/keys/{missing}/check"),
    ):
        assert (res.status_code, res.json()["error"]["code"]) == (404, "ai_key_not_found")


def test_a_key_is_deleted(client: TestClient) -> None:
    sign_in(client)
    key = add_key(client).json()["data"]["key"]

    assert client.delete(f"{AI}/keys/{key['id']}").status_code == 204
    assert client.get(f"{AI}/keys").json()["data"] == []


def test_key_checks_are_rate_limited(client: TestClient) -> None:
    sign_in(client)

    statuses = [add_key(client).status_code for _ in range(11)]

    assert statuses[:10] == [201] * 10
    assert statuses[10] == 429


def test_an_edit_uses_the_picked_model_with_the_users_key(client: TestClient, models: dict[str, Any]) -> None:
    sign_in(client)
    key = add_key(client, provider="anthropic", models=["gpt-5"]).json()["data"]["key"]
    models["user"].replies.append(GOOD_REPLY)

    res = client.post("/api/v1/ai/edit", json=request_body(model={"keyId": key["id"], "model": "gpt-5"}))

    assert res.status_code == 200
    assert models["default"].calls == []
    [endpoint] = models["endpoints"]
    assert endpoint == ModelEndpoint(
        base_url="https://api.anthropic.com/v1",
        api_key=VALID_AI_KEY,
        model="gpt-5",
        headers={"x-api-key": VALID_AI_KEY, "anthropic-version": "2023-06-01"},
    )


def test_an_edit_without_a_pick_uses_makables_model(client: TestClient, models: dict[str, Any]) -> None:
    sign_in(client)
    models["default"].replies.append(GOOD_REPLY)

    assert client.post("/api/v1/ai/edit", json=request_body()).status_code == 200
    assert models["endpoints"] == []


def test_an_edit_with_a_model_that_isnt_turned_on_is_refused(client: TestClient, models: dict[str, Any]) -> None:
    sign_in(client)
    key = add_key(client, models=["gpt-5"]).json()["data"]["key"]

    res = client.post("/api/v1/ai/edit", json=request_body(model={"keyId": key["id"], "model": "o4-mini"}))

    assert (res.status_code, res.json()["error"]["code"]) == (422, "ai_model_not_enabled")
    assert models["endpoints"] == []


def test_a_key_refused_during_an_edit_is_marked_invalid(client: TestClient, models: dict[str, Any]) -> None:
    sign_in(client)
    key = add_key(client, models=["gpt-5"]).json()["data"]["key"]
    models["user"] = RefusingModel()
    pick = {"keyId": key["id"], "model": "gpt-5"}

    res = client.post("/api/v1/ai/edit", json=request_body(model=pick))

    assert (res.status_code, res.json()["error"]["code"]) == (422, "ai_key_invalid")
    assert client.get(f"{AI}/keys").json()["data"][0]["status"] == "invalid"
    # Later edits with it are refused without calling the provider.
    again = client.post("/api/v1/ai/edit", json=request_body(model=pick))
    assert again.json()["error"]["code"] == "ai_key_invalid"
    assert len(models["endpoints"]) == 1
