from fastapi.testclient import TestClient

from app.tests.ai_edit_fixtures import HERO, HERO_PATH, PROJECT_ID, REPO
from app.tests.api.test_auth import sign_in
from app.tests.fakes import FakeGithubClient

SITES = "/api/v1/sites"
SITE = f"{SITES}/{PROJECT_ID}"


def test_sites_need_a_signed_in_user(client: TestClient) -> None:
    assert client.get(SITES).status_code == 401
    assert client.post(SITE, json={"name": "x"}).status_code == 401


def test_a_site_is_created_loaded_saved_and_undone(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)

    created = client.post(SITE, json={"name": "My site"})
    assert created.status_code == 201
    data = created.json()["data"]
    assert data["site"]["repoName"] == "my-site"
    assert "src/App.tsx" in data["files"]
    base = data["commitSha"]

    assert client.get(SITE).json()["data"]["commitSha"] == base
    assert [s["projectId"] for s in client.get(SITES).json()["data"]] == [str(PROJECT_ID)]

    app = data["files"]["src/App.tsx"]
    saved = client.put(f"{SITE}/files", json={"baseCommit": base, "files": {"src/App.tsx": app + "// edited\n"}})
    assert saved.status_code == 200
    commit = saved.json()["data"]["commitSha"]
    assert saved.json()["data"]["changed"] is True
    assert github.files[REPO]["src/App.tsx"].content.endswith("// edited\n")

    undone = client.post(f"{SITE}/undo", json={"commitSha": commit, "baseCommit": commit})
    assert undone.status_code == 200
    assert undone.json()["data"]["files"] == {"src/App.tsx": app}


def test_a_save_on_an_old_commit_is_a_conflict(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    base = client.post(SITE, json={"name": "My site"}).json()["data"]["commitSha"]
    github.edit_on_github(REPO, "src/index.css", "body {}\n")

    res = client.put(f"{SITE}/files", json={"baseCommit": base, "files": {HERO_PATH: HERO}})

    assert res.status_code == 409
    body = res.json()["error"]
    assert (body["code"], body["details"]["commitSha"]) == ("site_conflict", github.heads[REPO].commit_sha)


def test_bad_saves_are_refused(client: TestClient) -> None:
    sign_in(client)
    base = client.post(SITE, json={"name": "My site"}).json()["data"]["commitSha"]

    for body in (
        {"baseCommit": "main", "files": {}},
        {"baseCommit": base, "files": {".github/workflows/x.yml": "x"}},
        {"baseCommit": base, "files": {"../x": "x"}},
        {"baseCommit": base, "files": {}, "deletes": [".makable/manifest.json"]},
    ):
        assert client.put(f"{SITE}/files", json=body).status_code == 422


def test_a_taken_repo_name_says_which(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    github.add_repo("my-site", private=False, files={"index.html": "mine"})

    res = client.post(SITE, json={"name": "My site"})

    assert res.status_code == 409
    assert res.json()["error"]["details"] == {"repoName": "my-site"}
