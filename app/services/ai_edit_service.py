import asyncio
import logging
from typing import Any, Protocol
from uuid import UUID, uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Durability

from app.agents.ai_edit import EditRun, compile_edit_graph, result_of, run_input
from app.clients.model import AiKeyInvalidError, ModelClient
from app.constants.ai_edit import AiEditOutcome
from app.core.exceptions import AppError, ConflictError, ExternalServiceError, NotFoundError
from app.core.limits import KeyLock
from app.models.ai_thread import AiThread
from app.repositories.ai_thread import AiEditRecordRepository, AiThreadRepository
from app.repositories.unit_of_work import UnitOfWork
from app.schemas.ai_edit import AiEditDebug, AiEditRequest, AiEditTier1, AiEditTier2, FileEdit
from app.schemas.ai_key import ModelChoice
from app.schemas.ai_thread import AiEditRecordCreate, AiThreadCreate, AiThreadRead, AiTurnRead

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


class AiThreadNotFoundError(NotFoundError):
    code = "ai_thread_not_found"
    message = "This site's AI chat wasn't found."


class ModelSource(Protocol):
    """Picks the model for a request (`AiKeyService`)."""

    async def model_for(self, user_id: UUID, choice: ModelChoice | None) -> ModelClient: ...

    async def key_refused(self, user_id: UUID, key_id: UUID) -> None: ...


class AiEditService:
    """Tier 1 AI edits on the LangGraph agent (`app/agents/ai_edit`), one thread per site: asks the model for
    search/replace edits on the files the browser sent and checks them before answering. Prompts and the model
    call stay on the server. The thread keeps the chat; `ai_edit_requests` keeps each request as a row."""

    def __init__(
        self,
        models: ModelSource,
        threads: AiThreadRepository,
        records: AiEditRecordRepository,
        uow: UnitOfWork,
        checkpointer: BaseCheckpointSaver[str],
        runs: KeyLock,
        *,
        debug: bool = False,
    ) -> None:
        self._models = models
        self._threads = threads
        self._records = records
        self._uow = uow
        self._checkpointer = checkpointer
        self._graph = compile_edit_graph(checkpointer)
        self._runs = runs
        self._debug = debug

    async def edit(self, user_id: UUID, request: AiEditRequest) -> AiEditTier1 | AiEditTier2:
        """Answers Tier 1 edits, or hands the request to Tier 2 (by rule, or because the model escalated).
        The model is the one the user picked (`request.model`), or makable's own. The earlier requests in the
        prompt come from the site's thread, which is created on its first request.

        Raises:
            AiThreadNotFoundError: The site's thread is another user's.
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
        async with self._runs.hold((user_id, project_id)):
            await self._own_thread(user_id, project_id, create=True)
            run = EditRun(request, pick_model=lambda: self._models.model_for(user_id, request.model))
            run_id = uuid4()
            try:
                state = await self._graph.ainvoke(
                    run_input(run_id, request), self._config(project_id), context=run, durability=DURABILITY
                )
            except asyncio.CancelledError:
                await self._record_quietly(user_id, request, run, AiEditOutcome.CANCELLED, summary="stopped")
                raise
            except AiKeyInvalidError as err:
                if request.model is not None:
                    await self._models.key_refused(user_id, request.model.key_id)
                await self._record_quietly(user_id, request, run, AiEditOutcome.FAILED, summary=err.code)
                raise
            except Exception as err:
                code = err.code if isinstance(err, AppError) else type(err).__name__
                await self._record_quietly(user_id, request, run, AiEditOutcome.FAILED, summary=code)
                raise
            return await self._finish(user_id, request, run, state)

    async def thread(self, user_id: UUID, project_id: UUID) -> AiThreadRead:
        """The site's chat, oldest first. A site with no thread yet has an empty one.

        Raises:
            AiThreadNotFoundError: The thread is another user's.
        """
        thread = await self._own_thread(user_id, project_id, create=False)
        turns: list[AiTurnRead] = []
        if thread is not None:
            snapshot = await self._graph.aget_state(self._config(project_id))
            turns = [AiTurnRead.model_validate(t) for t in snapshot.values.get("turns", [])]
        return AiThreadRead(project_id=project_id, status="idle", turns=turns)

    async def start_over(self, user_id: UUID, project_id: UUID) -> None:
        """Deletes the site's thread (Start over): its checkpoints, its requests and the thread itself. The
        site's files aren't touched. Nothing to delete is fine.

        Raises:
            AiThreadNotFoundError: The thread is another user's.
            AiRunInProgressError: A request is in progress on the site.
        """
        async with self._runs.hold((user_id, project_id)):
            thread = await self._own_thread(user_id, project_id, create=False)
            if thread is None:
                return
            # Checkpoints first: if that fails, the thread row is still there to try again with.
            await self._checkpointer.adelete_thread(str(project_id))
            await self._records.delete_for_thread(project_id)
            await self._threads.delete(thread)
            await self._uow.commit()
            logger.info("AI thread deleted", extra={"user_id": str(user_id), "project_id": str(project_id)})

    async def _finish(
        self, user_id: UUID, request: AiEditRequest, run: EditRun, state: dict[str, Any]
    ) -> AiEditTier1 | AiEditTier2:
        result = result_of(state)
        outcome = AiEditOutcome(result["outcome"])
        attempts = state.get("attempt", 0)
        summary = result.get("summary") or result.get("reason") or result.get("problem")
        await self._record(user_id, request, run, outcome, tier=result.get("tier"), summary=summary, attempts=attempts)
        await self._prune(request.project_id)
        if outcome == AiEditOutcome.REJECTED:
            problem = result.get("problem", "")
            raise AiEditRejectedError(details={"problem": problem[:300]} if self._debug else None)
        debug = self._debug_info(run, attempts)
        if outcome == AiEditOutcome.ESCALATED:
            return AiEditTier2(reason=result.get("reason", ""), debug=debug)
        edits = [FileEdit.model_validate(e) for e in result.get("edits", [])]
        return AiEditTier1(summary=result.get("summary", ""), edits=edits, debug=debug)

    async def _own_thread(self, user_id: UUID, project_id: UUID, *, create: bool) -> AiThread | None:
        """The site's thread if it's the user's; with `create`, made if there's none (and committed, so the model
        call doesn't hold a transaction open)."""
        thread = await self._threads.get(project_id)
        if thread is not None and thread.user_id != user_id:
            raise AiThreadNotFoundError()
        if thread is None and create:
            thread = await self._threads.create(AiThreadCreate(id=project_id, user_id=user_id))
            await self._uow.commit()
        return thread

    async def _record(
        self,
        user_id: UUID,
        request: AiEditRequest,
        run: EditRun,
        outcome: AiEditOutcome,
        *,
        tier: int | None = None,
        summary: str | None = None,
        attempts: int = 0,
    ) -> None:
        """Writes the request's row and marks the thread as used."""
        await self._records.add(
            AiEditRecordCreate(
                thread_id=request.project_id,
                user_id=user_id,
                instruction=request.instruction,
                selection=request.target.model_dump(mode="json") if request.target else None,
                model=run.model.name if run.model else None,
                tier=tier,
                outcome=outcome,
                summary=summary,
                attempts=attempts,
            )
        )
        await self._threads.touch(request.project_id)
        await self._uow.commit()

    async def _record_quietly(
        self, user_id: UUID, request: AiEditRequest, run: EditRun, outcome: AiEditOutcome, *, summary: str
    ) -> None:
        """Records a request that ended with an error, without hiding that error if recording fails too."""
        try:
            await self._uow.rollback()
            await self._record(user_id, request, run, outcome, summary=summary)
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
        return AiEditDebug(model_input=run.user_prompt, attempts=attempts, model=run.model.name if run.model else "")
