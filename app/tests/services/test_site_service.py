"""Sites on the fake GitHub: creating the repo from the starter files, loading a site, saving manual edits as commits,
and undoing a change."""

import json
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.site import SITE_DESCRIPTION
from app.models.site import Site
from app.repositories.site import SqlSiteRepository
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.schemas.site import SiteNew, SiteSave, SiteUndo
from app.services.site_service import (
    SiteConflictError,
    SiteGithubNotConnectedError,
    SiteInvalidError,
    SiteNotFoundError,
    SiteNotReadyError,
    SiteRepoChangedError,
    SiteRepoMissingError,
    SiteRepoTakenError,
    SiteService,
    SiteUndoConflictError,
)
from app.tests.ai_edit_fixtures import BASE_COMMIT, GREEN_HERO, HERO, HERO_PATH, PROJECT_ID, REPO, SITE_FILES, USER
from app.tests.conftest import make_site_service
from app.tests.fakes import FAKE_CIPHER, FakeCredentials, FakeGithubClient

NEW_PROJECT = UUID("44444444-4444-4444-8444-444444444444")


@pytest.fixture
def sites(session: AsyncSession, github: FakeGithubClient) -> SiteService:
    return make_site_service(session, github)


async def test_a_new_site_gets_a_private_repo_with_the_starter_files(
    sites: SiteService, github: FakeGithubClient
) -> None:
    created = await sites.create(USER, NEW_PROJECT, SiteNew(name="Bakery in Pune!"))

    repo = github.repos["octocat/bakery-in-pune"]
    assert (repo.private, repo.description) == (True, SITE_DESCRIPTION)
    assert created.site.repo_name == "bakery-in-pune"
    assert created.site.repo_url == "https://github.com/octocat/bakery-in-pune"
    assert created.commit_sha == github.heads[repo.full_name].commit_sha
    files = github.files[repo.full_name]
    assert set(created.files) == set(files)
    assert json.loads(files[".makable/manifest.json"].content)["projectId"] == str(NEW_PROJECT)
    assert json.loads(files["package.json"].content)["name"] == "bakery-in-pune"
    assert '<h1 className="text-4xl font-bold">{"Bakery in Pune!"}</h1>' in files["src/App.tsx"].content
    assert files["README.md"].content.startswith("# Bakery in Pune!")
    assert github.commits[-1][1] == "Start Bakery in Pune! with makable"
    assert [s.name for s in await sites.list_sites(USER)] == ["Bakery in Pune!"]


async def test_creating_again_just_loads_the_site(sites: SiteService, github: FakeGithubClient) -> None:
    first = await sites.create(USER, NEW_PROJECT, SiteNew(name="Bakery"))

    again = await sites.create(USER, NEW_PROJECT, SiteNew(name="Something else"))

    assert again.commit_sha == first.commit_sha
    assert len(github.repos) == 1


async def test_a_repo_name_thats_taken_is_refused(sites: SiteService, github: FakeGithubClient) -> None:
    github.add_repo("bakery", private=False, files={"index.html": "<p>mine</p>"})

    with pytest.raises(SiteRepoTakenError) as caught:
        await sites.create(USER, NEW_PROJECT, SiteNew(name="Bakery"))

    assert caught.value.details == {"repoName": "bakery"}
    assert github.files["octocat/bakery"]["index.html"].content == "<p>mine</p>"


async def test_a_repo_name_another_site_has_is_refused_before_github_is_touched(
    sites: SiteService, github: FakeGithubClient, site: str
) -> None:
    del github.repos[REPO]  # The other site's repo is gone from GitHub, but its row still has the name.
    commits = len(github.commits)

    with pytest.raises(SiteRepoTakenError) as caught:
        await sites.create(USER, NEW_PROJECT, SiteNew(name="My Site"))

    assert caught.value.details == {"repoName": "my-site"}
    assert REPO not in github.repos
    assert len(github.commits) == commits


async def test_a_repo_with_this_sites_manifest_is_taken_over_without_overwriting_it(
    sites: SiteService, github: FakeGithubClient
) -> None:
    head = github.heads.get(REPO)
    assert head is None
    github.add_repo("my-site", private=True, files=SITE_FILES)

    created = await sites.create(USER, PROJECT_ID, SiteNew(name="My site"))

    assert created.files[HERO_PATH] == HERO
    assert created.files["src/App.tsx"] == SITE_FILES["src/App.tsx"]
    # Only starter files it was missing were added.
    assert "vite.config.ts" in created.files
    assert set(github.commits[-1][2]) == {"vite.config.ts", ".gitignore", "README.md", "index.html", "tsconfig.json"}


async def test_an_unfinished_create_is_finished(sites: SiteService, github: FakeGithubClient) -> None:
    github.add_repo("bakery", private=True, files={"README.md": "# bakery\n"}, description=SITE_DESCRIPTION)

    created = await sites.create(USER, NEW_PROJECT, SiteNew(name="Bakery"))

    assert created.files["README.md"].startswith("# Bakery\n\nBuilt with")


async def test_a_site_loads_its_files_with_binaries_left_out(
    sites: SiteService, github: FakeGithubClient, site: str
) -> None:
    github.edit_on_github(REPO, "src/index.css", "body {}\n")
    files = dict(github.files[REPO])

    loaded = await sites.load(USER, PROJECT_ID)

    assert loaded.commit_sha == github.heads[REPO].commit_sha != site
    assert loaded.files == {path: f.content for path, f in files.items()}
    assert loaded.binary == []
    assert loaded.site.name == "My site"


async def test_saving_commits_only_what_changed(sites: SiteService, github: FakeGithubClient, site: str) -> None:
    save = SiteSave(
        base_commit=site,
        files={HERO_PATH: GREEN_HERO, "src/App.tsx": SITE_FILES["src/App.tsx"]},
        deletes=["src/index.css", "src/nope.css"],
    )

    saved = await sites.save(USER, PROJECT_ID, save)

    assert saved.changed
    assert saved.commit_sha == github.heads[REPO].commit_sha
    assert github.commits[-1] == (REPO, "Edit 2 files", [HERO_PATH, "src/index.css"])
    assert "src/index.css" not in github.files[REPO]
    nothing = await sites.save(USER, PROJECT_ID, SiteSave(base_commit=saved.commit_sha, files={HERO_PATH: GREEN_HERO}))
    assert (nothing.changed, nothing.commit_sha) == (False, saved.commit_sha)


async def test_a_save_on_an_old_commit_is_a_conflict(sites: SiteService, github: FakeGithubClient, site: str) -> None:
    github.edit_on_github(REPO, "src/index.css", "body {}\n")

    with pytest.raises(SiteConflictError) as caught:
        await sites.save(USER, PROJECT_ID, SiteSave(base_commit=site, files={HERO_PATH: GREEN_HERO}))

    assert caught.value.details == {"commitSha": github.heads[REPO].commit_sha}
    assert github.files[REPO][HERO_PATH].content == HERO


async def test_a_file_cant_be_both_saved_and_deleted(sites: SiteService, site: str) -> None:
    with pytest.raises(SiteInvalidError):
        await sites.save(USER, PROJECT_ID, SiteSave(base_commit=site, files={HERO_PATH: "x"}, deletes=[HERO_PATH]))


def test_makables_own_files_cant_be_saved() -> None:
    with pytest.raises(ValueError, match="makable's or git's own"):
        SiteSave(base_commit=BASE_COMMIT, files={".makable/manifest.json": "{}"})
    with pytest.raises(ValueError, match="inside the project"):
        SiteSave(base_commit=BASE_COMMIT, files={"../x": "{}"})


async def test_undo_restores_what_a_change_replaced(sites: SiteService, github: FakeGithubClient, site: str) -> None:
    saved = await sites.save(
        USER, PROJECT_ID, SiteSave(base_commit=site, files={HERO_PATH: GREEN_HERO, "src/New.tsx": "x"})
    )

    undone = await sites.undo(USER, PROJECT_ID, SiteUndo(commit_sha=saved.commit_sha, base_commit=saved.commit_sha))

    assert undone.files == {HERO_PATH: HERO, "src/New.tsx": None}
    assert github.files[REPO][HERO_PATH].content == HERO
    assert "src/New.tsx" not in github.files[REPO]
    assert github.commits[-1][1] == "Undo: Edit 2 files"


async def test_undo_refuses_when_the_files_changed_again(
    sites: SiteService, github: FakeGithubClient, site: str
) -> None:
    first = await sites.save(USER, PROJECT_ID, SiteSave(base_commit=site, files={HERO_PATH: GREEN_HERO}))
    second = await sites.save(USER, PROJECT_ID, SiteSave(base_commit=first.commit_sha, files={HERO_PATH: "changed"}))

    with pytest.raises(SiteUndoConflictError):
        await sites.undo(USER, PROJECT_ID, SiteUndo(commit_sha=first.commit_sha, base_commit=second.commit_sha))
    with pytest.raises(SiteInvalidError):
        await sites.undo(USER, PROJECT_ID, SiteUndo(commit_sha=site, base_commit=second.commit_sha))


async def test_other_users_and_unknown_sites_are_not_found(
    sites: SiteService, session: AsyncSession, site: str
) -> None:
    other = USER.model_copy(update={"id": UUID("55555555-5555-4555-8555-555555555555")})

    with pytest.raises(SiteNotFoundError):
        await sites.load(other, PROJECT_ID)
    with pytest.raises(SiteNotFoundError):
        await sites.create(other, PROJECT_ID, SiteNew(name="Mine now"))
    with pytest.raises(SiteNotFoundError):
        await sites.load(USER, NEW_PROJECT)
    session.add(Site(id=NEW_PROJECT, user_id=USER.id, name="Not yet"))
    await session.commit()
    with pytest.raises(SiteNotReadyError):
        await sites.load(USER, NEW_PROJECT)


async def test_a_repo_thats_gone_or_lost_its_manifest_is_refused(
    session: AsyncSession, github: FakeGithubClient, site: str
) -> None:
    github.edit_on_github(REPO, ".makable/manifest.json", None)
    with pytest.raises(SiteRepoChangedError):
        await make_site_service(session, github).load(USER, PROJECT_ID)

    del github.repos[REPO]
    with pytest.raises(SiteRepoMissingError):
        await make_site_service(session, github).load(USER, PROJECT_ID)


async def test_without_a_github_token_the_user_must_reconnect(
    session: AsyncSession, github: FakeGithubClient, site: str
) -> None:
    sites = SiteService(
        github, SqlSiteRepository(session), FakeCredentials(None), FAKE_CIPHER, SqlAlchemyUnitOfWork(session)
    )

    with pytest.raises(SiteGithubNotConnectedError):
        await sites.load(USER, PROJECT_ID)


async def test_a_commit_the_service_makes_doesnt_need_another_download(
    sites: SiteService, github: FakeGithubClient, site: str
) -> None:
    await sites.load(USER, PROJECT_ID)
    saved = await sites.save(USER, PROJECT_ID, SiteSave(base_commit=site, files={HERO_PATH: GREEN_HERO}))

    loaded = await sites.load(USER, PROJECT_ID)

    assert loaded.files[HERO_PATH] == GREEN_HERO
    assert github.archive_reads == [(REPO, site)]
    assert loaded.commit_sha == saved.commit_sha
