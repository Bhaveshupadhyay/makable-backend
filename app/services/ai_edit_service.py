import asyncio
import logging
from typing import Any, Protocol
from uuid import UUID, uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Durability

from app.agents.ai_edit import EditRun, EditState, RunResult, compile_edit_graph, result_of, run_input, turn_for
from app.clients.model import AiKeyInvalidError, ModelClient
from app.constants.ai_edit import AiEditOutcome
from app.core.exceptions import AppError, ConflictError, ExternalServiceError
from app.core.limits import KeyLock
from app.repositories.site import AiEditRecordRepository, SiteRepository
from app.repositories.unit_of_work import UnitOfWork
from app.schemas.ai_edit import AiEditApplied, AiEditDebug, AiEditEscalated, AiEditRequest
from app.schemas.ai_key import ModelChoice
from app.schemas.ai_thread import AiThreadRead, AiTurnRead
from app.schemas.site import AiEditRecordCreate
from app.schemas.user import UserRead
from app.services.site_service import SiteConflictError, SiteNotFoundError, SiteService

logger = logging.getLogger(__name__)

# The checkpoint is written when a run ends (or pauses), not after every step: a run is short, and a cancelled or
# failed one then leaves nothing half-done behind (only its input), which the next run's input replaces.
DURABILITY: Durability = "exit"


class AiEditRejectedError(ExternalServiceError):
    code = "ai_edit_rejected"
    message = "The AI couldn't make a change that works. Try asking in a different way."


class AiRunInProgressError(ConflictError):
    code = "ai_run_in_progress"
    message = "The AI is still working on this site. Wait for it to finish, or stop it first."


class ModelSource(Protocol):
    """Picks the model for a request (`AiKeyService`)."""

    async def model_for(self, user_id: UUID, choice: ModelChoice | None) -> ModelClient: ...

    async def key_refused(self, user_id: UUID, key_id: UUID) -> None: ...


class AiEditService:
    """AI edits on the LangGraph agent (`app/agents/ai_edit`), one thread per site: the agent reads the site's code
    from its repo at the commit the browser is at, asks the model for search/replace edits, checks them, and commits
    them. Prompts and the model call stay on the server. The thread keeps the chat; `ai_edit_requests` keeps each
    request as a row."""

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
        debug: bool = False,
    ) -> None:
        self._models = models
        self._site_service = site_service
        self._sites = sites
        self._records = records
        self._uow = uow
        self._checkpointer = checkpointer
        self._graph = compile_edit_graph(checkpointer)
        self._runs = runs
        self._debug = debug

    async def edit(self, user: UserRead, request: AiEditRequest) -> AiEditApplied | AiEditEscalated:
        """Makes the change and commits it to the site's repo, or hands the request to Tier 2 (by rule, or because the
        model escalated). The model is the one the user picked (`request.model`), or makable's own. The earlier
        requests in the prompt come from the site's thread.

        If the request is cancelled (Stop) once the commit has started, the commit still finishes and is recorded,
        and the chat shows it.

        Raises:
            SiteNotFoundError: No such site, or it's another user's.
            SiteNotReadyError: The site has no repo yet.
            SiteConflictError: The site isn't at `base_commit` on GitHub any more.
            AiRunInProgressError: The site already has a request in progress.
            AiModelRequiredError: Nothing was picked and there's no makable model.
            AiKeyNotFoundError: The picked key isn't the user's (any more).
            AiModelNotEnabledError: The picked model isn't turned on for the key.
            AiKeyInvalidError: The provider refused the user's key. It's marked invalid.
            ModelRateLimitedError: The provider is rate limiting the user's key.
            ModelUnavailableError: The model couldn't be reached or answered with an error.
            AiEditRejectedError: The model's answer was rejected on every attempt.
        """
        project_id = request.project_id
        async with self._runs.hold((user.id, project_id)):
            site = await self._site_service.access(user, project_id)
            # The browser saves its own edits first, so the site is at its base commit: if not, another tab or device
            # changed it, and the edit would be for code that's gone. Checked again by the commit.
            head = await site.head()
            if head != request.base_commit:
                raise SiteConflictError(details={"commitSha": head})
            run = EditRun(request, pick_model=lambda: self._models.model_for(user.id, request.model), site=site)
            run_id = uuid4()
            try:
                state = await self._graph.ainvoke(
                    run_input(run_id, request), self._config(project_id), context=run, durability=DURABILITY
                )
            except asyncio.CancelledError:
                await self._after_cancel(user, request, run, run_id)
                raise
            except AiKeyInvalidError as err:
                if request.model is not None:
                    await self._models.key_refused(user.id, request.model.key_id)
                await self._record_quietly(user, request, run, AiEditOutcome.FAILED, summary=err.code)
                raise
            except Exception as err:
                code = err.code if isinstance(err, AppError) else type(err).__name__
                await self._record_quietly(user, request, run, AiEditOutcome.FAILED, summary=code)
                raise
            return await self._finish(user, request, run, run_id, state)

    async def thread(self, user: UserRead, project_id: UUID) -> AiThreadRead:
        """The site's chat, oldest first. A site with no requests yet has an empty one.

        Raises:
            SiteNotFoundError: The site is another user's.
        """
        site = await self._sites.get(project_id)
        if site is not None and site.user_id != user.id:
            raise SiteNotFoundError()
        turns: list[AiTurnRead] = []
        if site is not None:
            snapshot = await self._graph.aget_state(self._config(project_id))
            turns = [AiTurnRead.model_validate(t) for t in snapshot.values.get("turns", [])]
        return AiThreadRead(project_id=project_id, status="idle", turns=turns)

    async def start_over(self, user: UserRead, project_id: UUID) -> None:
        """Forgets the site in makable (Start over): its chat (the checkpoints), its requests and its row. The repo on
        the user's GitHub isn't touched. Nothing to delete is fine.

        Raises:
            SiteNotFoundError: The site is another user's.
            AiRunInProgressError: A request is in progress on the site.
        """
        async with self._runs.hold((user.id, project_id)):
            site = await self._sites.get(project_id)
            if site is None:
                return
            if site.user_id != user.id:
                raise SiteNotFoundError()
            # Checkpoints first: if that fails, the site row is still there to try again with.
            await self._checkpointer.adelete_thread(str(project_id))
            await self._records.delete_for_site(project_id)
            await self._sites.delete(site)
            await self._uow.commit()
            logger.info("site forgotten", extra={"user_id": str(user.id), "project_id": str(project_id)})

    async def _finish(
        self, user: UserRead, request: AiEditRequest, run: EditRun, run_id: UUID, state: dict[str, Any]
    ) -> AiEditApplied | AiEditEscalated:
        result = result_of(state)
        outcome = AiEditOutcome(result["outcome"])
        attempts = state.get("attempt", 0)
        await self._record(user, request, run, outcome, result=result, attempts=attempts)
        await self._prune(request.project_id)
        if outcome == AiEditOutcome.REJECTED:
            problem = result.get("problem", "")
            raise AiEditRejectedError(details={"problem": problem[:300]} if self._debug else None)
        debug = self._debug_info(run, attempts)
        if outcome == AiEditOutcome.ESCALATED:
            return AiEditEscalated(run_id=run_id, reason=result.get("reason", ""), debug=debug)
        return AiEditApplied(
            run_id=run_id,
            summary=result.get("summary", ""),
            commit_sha=result.get("commit_sha", ""),
            files=run.changed_files,
            debug=debug,
        )

    async def _after_cancel(self, user: UserRead, request: AiEditRequest, run: EditRun, run_id: UUID) -> None:
        """Records a cancelled run. A commit that had started is finished first: then the change is on GitHub, so it's
        recorded as applied and added to the chat (where the SPA finds it and offers Undo)."""
        committed: str | None = None
        if run.commit_task is not None and run.committing is not None:
            try:
                committed = await asyncio.shield(run.commit_task)
            except Exception:
                logger.warning("The commit of a cancelled AI edit failed", exc_info=True)
        if committed is None or run.committing is None:
            await self._record_quietly(user, request, run, AiEditOutcome.CANCELLED, summary="stopped")
            return
        result: RunResult = {**run.committing, "commit_sha": committed}
        try:
            finished = EditState(
                run_id=str(run_id),
                instruction=request.instruction,
                target=run_input(run_id, request)["target"],
                result=result,
            )
            update: dict[str, Any] = {"turns": [turn_for(finished)], "result": result, "messages": [], "reply": ""}
            await self._graph.aupdate_state(self._config(request.project_id), update, as_node="finish")
            await self._uow.rollback()
            await self._record(user, request, run, AiEditOutcome.APPLIED, result=result, attempts=0)
        except Exception:
            logger.exception("Couldn't record an AI edit committed after Stop")

    async def _record(
        self,
        user: UserRead,
        request: AiEditRequest,
        run: EditRun,
        outcome: AiEditOutcome,
        *,
        result: RunResult | None = None,
        summary: str | None = None,
        attempts: int = 0,
    ) -> None:
        """Writes the request's row and marks the site as used."""
        if result is not None:
            summary = result.get("summary") or result.get("reason") or result.get("problem")
        await self._records.add(
            AiEditRecordCreate(
                site_id=request.project_id,
                user_id=user.id,
                instruction=request.instruction,
                selection=request.target.model_dump(mode="json") if request.target else None,
                model=run.model.name if run.model else None,
                tier=result.get("tier") if result else None,
                outcome=outcome,
                summary=summary,
                attempts=attempts,
                commit_sha=result.get("commit_sha") if result else None,
            )
        )
        await self._sites.touch(request.project_id)
        await self._uow.commit()

    async def _record_quietly(
        self, user: UserRead, request: AiEditRequest, run: EditRun, outcome: AiEditOutcome, *, summary: str
    ) -> None:
        """Records a request that ended with an error, without hiding that error if recording fails too."""
        try:
            await self._uow.rollback()
            await self._record(user, request, run, outcome, summary=summary)
        except Exception:
            logger.exception("Couldn't record an AI edit request", extra={"outcome": outcome.value})

    async def _prune(self, project_id: UUID) -> None:
        """Keeps only the thread's latest checkpoint: it has the whole chat, and nothing reads older ones."""
        try:
            await self._checkpointer.aprune([str(project_id)], strategy="keep_latest")
        except NotImplementedError:
            pass  # The in-memory checkpointer (tests, SQLite dev) can't prune; it's gone at exit anyway.
        except Exception:
            # Best effort: the next run prunes again.
            logger.warning("Couldn't prune an AI thread's checkpoints", exc_info=True)

    @staticmethod
    def _config(project_id: UUID) -> Any:
        return {"configurable": {"thread_id": str(project_id)}}

    def _debug_info(self, run: EditRun, attempts: int) -> AiEditDebug | None:
        if not self._debug:
            return None
        return AiEditDebug(model_input=run.model_input, attempts=attempts, model=run.model.name if run.model else "")
