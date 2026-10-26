import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from typing import Any, cast
from uuid import UUID, uuid4

import anyio
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from app.agents.create_site import (
    CreateFailedError,
    CreateRun,
    NameTakenError,
    compile_create_graph,
    create_input,
)
from app.agents.create_site.tools.names import suggest_name
from app.agents.thread import RunResult, Turn, UserReply
from app.clients.fallback_model import ModelSwitch
from app.constants.ai_create import HEARTBEAT_SECONDS
from app.constants.ai_edit import AiEditOutcome
from app.core.database import utc_now
from app.core.exceptions import AppError, ConflictError, ExternalServiceError
from app.core.limits import KeyLock
from app.repositories.site import AiEditRecordRepository, SiteRepository
from app.repositories.unit_of_work import UnitOfWork
from app.schemas.ai_create import AiCreateAnswer, AiCreated, AiCreateRequest
from app.schemas.ai_edit import AiEditNeedsInput, Question
from app.schemas.site import AiEditRecordCreate
from app.schemas.user import UserRead
from app.services.ai_edit_service import AiRunSupersededError, ModelSource
from app.services.ai_runs import (
    DURABILITY,
    PickedModel,
    create_config,
    end_paused,
    pause_value,
    prune,
    switches_read,
)
from app.services.site_service import SiteNotFoundError, SiteService

logger = logging.getLogger(__name__)

type Event = dict[str, Any]


class SiteCreateFailedError(ExternalServiceError):
    code = "ai_create_failed"
    message = "The AI couldn't build the site. Try again, maybe with another model."


class SiteNamesTakenError(ConflictError):
    code = "site_repo_taken"
    message = "Those names are taken by repositories on your GitHub. Start again with another name."


class CreationEvents:
    """A creation's events. They hold the site's lock: closing them (or reading them to the end) stops the creation if
    it's still going and frees the site. Closing them works also when they were never read (an async generator that
    never started doesn't run its `finally`), so whoever gets them must close them."""

    def __init__(self, events: AsyncGenerator[Event], stack: AsyncExitStack) -> None:
        self._events = events
        self._stack = stack

    def __aiter__(self) -> CreationEvents:
        return self

    async def __anext__(self) -> Event:
        return await self._events.__anext__()

    async def aclose(self) -> None:
        try:
            await self._events.aclose()
        finally:
            await self._stack.aclose()


class SiteCreateService:
    """Creating a site from a prompt with the create agent (`app/agents/create_site`), on the site's create thread
    (`<projectId>:create`): it asks for the site's name (a pause: answered with `answer`), creates the repo with the
    starter files, has the model plan and write the site's files, checks them, and commits them.

    A creation takes minutes, so it's streamed: `create` and `answer` check the request and take the site's lock (one
    AI request per site at a time, shared with edits) before they return, so those errors are answered as usual; then
    the events come (`progress`, a `ping` while nothing else happens, and a `result` or an `error` at the end). Closing
    the events stops the creation, unless its commit had started (then it's finished and recorded)."""

    def __init__(
        self,
        models: ModelSource,
        site_service: SiteService,
        sites: SiteRepository,
        records: AiEditRecordRepository,
        uow: UnitOfWork,
        checkpointer: BaseCheckpointSaver[str],
        runs: KeyLock,
        *,
        heartbeat: float = HEARTBEAT_SECONDS,
    ) -> None:
        self._models = models
        self._site_service = site_service
        self._sites = sites
        self._records = records
        self._uow = uow
        self._checkpointer = checkpointer
        self._graph = compile_create_graph(checkpointer)
        self._runs = runs
        self._heartbeat = heartbeat

    async def create(self, user: UserRead, request: AiCreateRequest) -> CreationEvents:
        """Starts creating the site from the prompt. A site that has its repo already is made again over its files (no
        name question). A creation waiting for its name is replaced.

        Raises:
            SiteNotFoundError: The site is another user's.
            AiRunInProgressError: The site already has an AI request in progress.
            The site's repo errors of `SiteService.access`, for a site that has one.
        """
        project_id = request.project_id
        stack = AsyncExitStack()
        await stack.enter_async_context(self._runs.hold((user.id, project_id)))
        try:
            site = await self._site_service.ensure(user, project_id, suggest_name(request.prompt))
            name: str | None = None
            base: str | None = None
            if site.repo_name:
                name = site.name
                base = await (await self._site_service.access(user, project_id)).head()
            await end_paused(
                self._graph,
                create_config(project_id),
                await self._graph.aget_state(create_config(project_id)),
                records=self._records,
                uow=self._uow,
                user_id=user.id,
                project_id=project_id,
            )
            run_id = uuid4()
            graph_input = create_input(run_id, request, name=name, base_commit=base)
        except BaseException:
            await stack.aclose()
            raise
        return CreationEvents(self._events(user, request, run_id, graph_input, stack), stack)

    async def answer(self, user: UserRead, project_id: UUID, body: AiCreateAnswer) -> CreationEvents:
        """Continues the creation that asked (the site's name) with the user's answer.

        Raises:
            SiteNotFoundError: The site is another user's.
            AiRunSupersededError: No creation is waiting for that answer.
            AiRunInProgressError: The site already has an AI request in progress.
        """
        stack = AsyncExitStack()
        await stack.enter_async_context(self._runs.hold((user.id, project_id)))
        try:
            site = await self._sites.get(project_id)
            if site is None or site.user_id != user.id:
                raise SiteNotFoundError()
            snapshot = await self._graph.aget_state(create_config(project_id))
            if pause_value(snapshot) is None or snapshot.values.get("run_id") != str(body.run_id):
                raise AiRunSupersededError()
            request = AiCreateRequest.model_validate(snapshot.values["request"])
            await self._uow.rollback()
        except BaseException:
            await stack.aclose()
            raise
        resume: Command[Any] = Command(resume=UserReply(answer=body.answer, chat=body.chat))
        return CreationEvents(self._events(user, request, body.run_id, resume, stack), stack)

    async def _events(
        self, user: UserRead, request: AiCreateRequest, run_id: UUID, graph_input: Any, stack: AsyncExitStack
    ) -> AsyncGenerator[Event]:
        """Runs the graph and streams what it says. The run is a task of its own, so pings can go out while the model
        writes; it's cancelled when the stream is (the request closed). When the user's model stops answering and
        another of theirs takes over, the stream says so; keys the providers refused are marked invalid, however it
        ends."""
        queue: asyncio.Queue[Event] = asyncio.Queue()

        def announce(switch: ModelSwitch) -> None:
            queue.put_nowait({"type": "progress", "stage": "model", "message": switch.message})

        picked = PickedModel(lambda: self._models.model_for(user.id, request.model, on_switch=announce))
        run = CreateRun(request, pick_model=picked.pick, site=self._site_service.creator(user, request.project_id))

        async def drive() -> dict[str, Any]:
            final: dict[str, Any] = {}
            async for mode, chunk in self._graph.astream(
                graph_input,
                create_config(request.project_id),
                context=run,
                stream_mode=["custom", "values"],
                durability=DURABILITY,
            ):
                if mode == "custom":
                    queue.put_nowait(cast(Event, chunk))
                else:
                    final = cast(dict[str, Any], chunk)
            return final

        task = asyncio.ensure_future(drive())
        finished = False
        try:
            while not task.done() or not queue.empty():
                if not queue.empty():
                    yield queue.get_nowait()
                    continue
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait(
                    {task, getter}, timeout=self._heartbeat, return_when=asyncio.FIRST_COMPLETED
                )
                if getter in done:
                    yield getter.result()
                    continue
                getter.cancel()
                if not done:
                    yield {"type": "ping"}
            finished = True
            try:
                state = task.result()
            except Exception as err:
                with anyio.CancelScope(shield=True):
                    await self._failed(user, request, run, err)
                yield {"type": "error", "error": _error_body(err)}
                return
            with anyio.CancelScope(shield=True):
                result = await self._result(user, request, run, run_id, state)
            result.model_switches = switches_read(picked.switches)
            yield {"type": "result", "data": result.model_dump(mode="json", by_alias=True, exclude_none=True)}
        finally:
            with anyio.CancelScope(shield=True):
                if not finished:
                    await self._stopped(user, request, run, run_id, task)
                await self._mark_refused(user, picked)
                await stack.aclose()

    async def _mark_refused(self, user: UserRead, picked: PickedModel) -> None:
        """Marks the keys the providers refused during a run invalid, so the menu stops offering them. A failure is
        logged: the creation doesn't depend on it."""
        for key_id in picked.refused_keys:
            try:
                await self._models.key_refused(user.id, key_id)
            except Exception:
                logger.exception("Couldn't mark a refused AI key invalid")
                await self._uow.rollback()

    async def _result(
        self, user: UserRead, request: AiCreateRequest, run: CreateRun, run_id: UUID, state: dict[str, Any]
    ) -> AiCreated | AiEditNeedsInput:
        if state.get("__interrupt__"):
            return AiEditNeedsInput(run_id=run_id, question=Question.model_validate(state["__interrupt__"][0].value))
        result: RunResult = state["result"]
        await self._record(user, request, AiEditOutcome.APPLIED, result=result, state=state, run=run)
        await prune(self._checkpointer, create_config(request.project_id))
        site = await self._site_service.read(user, request.project_id)
        return AiCreated(run_id=run_id, summary=result.get("summary", ""), commit_sha=result["commit_sha"], site=site)

    async def _failed(self, user: UserRead, request: AiCreateRequest, run: CreateRun, err: Exception) -> None:
        summary = _error_body(err)["code"]
        if isinstance(err, CreateFailedError):
            # Why the model's last reply was rejected, which the user's message doesn't say: kept with the request.
            summary = f"{summary}: {err}"
            logger.warning(
                "site creation failed",
                extra={
                    "user_id": str(user.id),
                    "project_id": str(request.project_id),
                    "model": run.model.name if run.model else None,
                    "reason": str(err),
                },
            )
        await self._record_quietly(user, request, AiEditOutcome.FAILED, summary=summary, run=run)

    async def _stopped(
        self, user: UserRead, request: AiCreateRequest, run: CreateRun, run_id: UUID, task: asyncio.Future[Any]
    ) -> None:
        """The stream closed before the creation ended (Stop, or the page went away): the creation is cancelled, but a
        commit that had started is finished, recorded and added to the chat."""
        task.cancel()
        await asyncio.wait({task})
        committed: str | None = None
        if run.commit_task is not None:
            try:
                committed = await asyncio.shield(run.commit_task)
            except Exception:
                logger.warning("The commit of a stopped creation failed", exc_info=True)
        if committed is None:
            await self._record_quietly(user, request, AiEditOutcome.CANCELLED, summary="stopped", run=run)
            return
        try:
            # The run's own state at the commit: the saved one is older (a run is saved when it pauses or ends).
            values: dict[str, Any] = dict(run.committing or {})
            summary = values.get("summary", "")
            result = RunResult(outcome=AiEditOutcome.APPLIED.value, summary=summary, commit_sha=committed)
            turn = Turn(
                run_id=str(run_id),
                instruction=request.prompt,
                target=None,
                outcome=result["outcome"],
                reply=summary,
                questions=values.get("questions", []),
                steps=values.get("steps", []),
                commit_sha=committed,
                created_at=utc_now().isoformat(),
            )
            update = {"turns": [turn], "result": result}
            await self._graph.aupdate_state(create_config(request.project_id), update, as_node="finish")
            await self._uow.rollback()
            await self._record(user, request, AiEditOutcome.APPLIED, result=result, state=values, run=run)
        except Exception:
            logger.exception("Couldn't record a creation committed after Stop")

    async def _record(
        self,
        user: UserRead,
        request: AiCreateRequest,
        outcome: AiEditOutcome,
        *,
        run: CreateRun,
        result: RunResult | None = None,
        state: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> None:
        if result is not None:
            summary = result.get("summary")
        model = (state or {}).get("model") or (run.model.name if run.model else None)
        await self._records.add(
            AiEditRecordCreate(
                site_id=request.project_id,
                user_id=user.id,
                instruction=request.prompt,
                selection=None,
                model=model,
                tier=None,
                outcome=outcome,
                summary=summary,
                attempts=(state or {}).get("attempt", 0),
                commit_sha=result.get("commit_sha") if result else None,
            )
        )
        await self._sites.touch(request.project_id)
        await self._uow.commit()

    async def _record_quietly(
        self, user: UserRead, request: AiCreateRequest, outcome: AiEditOutcome, *, summary: str, run: CreateRun
    ) -> None:
        try:
            await self._uow.rollback()
            await self._record(user, request, outcome, summary=summary, run=run)
        except Exception:
            logger.exception("Couldn't record a creation", extra={"outcome": outcome.value})


def _error_body(err: BaseException) -> dict[str, Any]:
    """The error envelope's `error` for a creation that failed while it streamed."""
    if isinstance(err, NameTakenError):
        err = SiteNamesTakenError(details={"repoName": err.repo_name})
    elif isinstance(err, CreateFailedError):
        err = SiteCreateFailedError()
    if isinstance(err, AppError):
        return {"code": err.code, "message": err.message, "details": err.details}
    logger.error("A creation failed", exc_info=err)
    return {"code": "internal_error", "message": "Something went wrong while building the site.", "details": None}
