import asyncio
import logging
from typing import Any, Protocol
from uuid import UUID, uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command, Durability, StateSnapshot

from app.agents.ai_edit import EditRun, EditState, RunResult, compile_edit_graph, result_of, run_input, turn_for
from app.clients.model import AiKeyInvalidError, ModelClient
from app.constants.ai_edit import AiEditOutcome
from app.core.exceptions import AppError, ConflictError, ExternalServiceError
from app.core.limits import KeyLock
from app.repositories.site import AiEditRecordRepository, SiteRepository
from app.repositories.unit_of_work import UnitOfWork
from app.schemas.ai_edit import (
    AiEditAnswer,
    AiEditApplied,
    AiEditDebug,
    AiEditEscalated,
    AiEditNeedsInput,
    AiEditRequest,
    Question,
)
from app.schemas.ai_key import ModelChoice
from app.schemas.ai_thread import AiPausedRun, AiThreadRead, AiTurnRead
from app.schemas.site import AiEditRecordCreate
from app.schemas.user import UserRead
from app.services.site_service import SiteAccess, SiteConflictError, SiteNotFoundError, SiteService

logger = logging.getLogger(__name__)

type AiEditResult = AiEditApplied | AiEditEscalated | AiEditNeedsInput

# The checkpoint is written when a run ends (or pauses), not after every step: a run is short, and a cancelled or
# failed one then leaves nothing half-done behind (only its input), which the next run's input replaces.
DURABILITY: Durability = "exit"


class AiEditRejectedError(ExternalServiceError):
    code = "ai_edit_rejected"
    message = "The AI couldn't make a change that works. Try asking in a different way."


class AiRunInProgressError(ConflictError):
    code = "ai_run_in_progress"
    message = "The AI is still working on this site. Wait for it to finish, or stop it first."


class AiRunSupersededError(ConflictError):
    code = "ai_run_superseded"
    message = "That question was already answered or replaced. Reload the chat."


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

    async def edit(self, user: UserRead, request: AiEditRequest) -> AiEditResult:
        """Makes the change and commits it to the site's repo, hands the request to Tier 2 (by rule, or because the
        model escalated), or pauses on a question for the user (answered with `answer`). The model is the one the user
        picked (`request.model`), or makable's own. The earlier requests in the prompt come from the site's thread. A
        run paused on a question is skipped: the new instruction replaces it.

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
            site = await self._site_at(user, project_id, request.base_commit)
            await self._skip_paused(user, project_id)
            run_id = uuid4()
            return await self._invoke(user, request, site, run_id, run_input(run_id, request), resuming=False)

    async def answer(self, user: UserRead, project_id: UUID, body: AiEditAnswer) -> AiEditResult:
        """Continues a run paused on a question with the user's answer, on the site as it is now (`base_commit`).

        Raises:
            AiRunSupersededError: No run is waiting for that answer (it was answered, replaced or dismissed).
            The errors of `edit`.
        """
        async with self._runs.hold((user.id, project_id)):
            site = await self._site_at(user, project_id, body.base_commit)
            paused = await self._paused(project_id)
            if paused is None or paused.values.get("run_id") != str(body.run_id):
                raise AiRunSupersededError()
            request = AiEditRequest.model_validate(
                {**paused.values["request"], "base_commit": body.base_commit, "screenshot": body.screenshot}
            )
            resume: Command[Any] = Command(resume=body.answer, update={"base_commit": body.base_commit})
            return await self._invoke(user, request, site, body.run_id, resume, resuming=True)

    async def dismiss(self, user: UserRead, project_id: UUID) -> None:
        """Ends the run that's waiting for the user (Dismiss): it's recorded as skipped. Nothing waiting is fine.

        Raises:
            SiteNotFoundError: The site is another user's.
            AiRunInProgressError: A request is in progress on the site.
        """
        async with self._runs.hold((user.id, project_id)):
            site = await self._sites.get(project_id)
            if site is not None and site.user_id != user.id:
                raise SiteNotFoundError()
            if site is not None:
                await self._skip_paused(user, project_id)

    async def thread(self, user: UserRead, project_id: UUID) -> AiThreadRead:
        """The site's chat, oldest first, and the run waiting for the user's answer, if one is (it survives a closed
        tab). A site with no requests yet has an empty one.

        Raises:
            SiteNotFoundError: The site is another user's.
        """
        site = await self._sites.get(project_id)
        if site is not None and site.user_id != user.id:
            raise SiteNotFoundError()
        if site is None:
            return AiThreadRead(project_id=project_id, status="idle", turns=[])
        snapshot = await self._graph.aget_state(self._config(project_id))
        turns = [AiTurnRead.model_validate(t) for t in snapshot.values.get("turns", [])]
        paused = _paused_question(snapshot)
        if paused is None:
            return AiThreadRead(project_id=project_id, status="idle", turns=turns)
        run = AiPausedRun(
            run_id=snapshot.values["run_id"],
            instruction=snapshot.values["instruction"],
            target=snapshot.values.get("target"),
            question=paused,
        )
        return AiThreadRead(project_id=project_id, status="needs_input", turns=turns, run=run)

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

    async def _invoke(
        self,
        user: UserRead,
        request: AiEditRequest,
        site: SiteAccess,
        run_id: UUID,
        graph_input: Any,
        *,
        resuming: bool,
    ) -> AiEditResult:
        """Runs the graph (a new run, or a paused one resumed) and turns how it ended into the answer."""
        run = EditRun(request, pick_model=lambda: self._models.model_for(user.id, request.model), site=site)
        config = self._config(request.project_id)
        try:
            state = await self._graph.ainvoke(graph_input, config, context=run, durability=DURABILITY)
        except asyncio.CancelledError:
            await self._after_cancel(user, request, run, run_id, resuming=resuming)
            raise
        except AiKeyInvalidError as err:
            if request.model is not None:
                await self._models.key_refused(user.id, request.model.key_id)
            await self._record_quietly(user, request, run, AiEditOutcome.FAILED, summary=err.code)
            raise
        except Exception as err:
            code = err.code if isinstance(err, AppError) else type(err).__name__
            # A resumed run that fails ends too: the answer was saved with it, so its question doesn't come back.
            await self._record_quietly(user, request, run, AiEditOutcome.FAILED, summary=code)
            raise
        if state.get("__interrupt__"):
            question = Question.model_validate(state["__interrupt__"][0].value)
            debug = self._debug_info(run, state.get("attempt", 0))
            return AiEditNeedsInput(run_id=run_id, question=question, debug=debug)
        return await self._finish(user, request, run, run_id, state)

    async def _site_at(self, user: UserRead, project_id: UUID, base_commit: str) -> SiteAccess:
        """The site's repo, which must be at `base_commit`: the browser saves its own edits first, so if it isn't,
        another tab or device changed it, and the edit would be for code that's gone. Checked again by the commit.

        Raises:
            SiteConflictError: The site isn't at `base_commit`.
        """
        site = await self._site_service.access(user, project_id)
        head = await site.head()
        if head != base_commit:
            raise SiteConflictError(details={"commitSha": head})
        return site

    async def _paused(self, project_id: UUID) -> StateSnapshot | None:
        """The thread's state, if a run is waiting for the user's answer."""
        snapshot = await self._graph.aget_state(self._config(project_id))
        return snapshot if _paused_question(snapshot) is not None else None

    async def _skip_paused(self, user: UserRead, project_id: UUID) -> None:
        """Ends a run that's waiting for the user (a new instruction or Dismiss replaces it), recorded as skipped."""
        paused = await self._paused(project_id)
        if paused is None:
            return
        values = paused.values
        result = RunResult(outcome=AiEditOutcome.SKIPPED.value)
        await self._graph.aupdate_state(
            self._config(project_id), {"result": result, "messages": [], "reply": ""}, as_node="finish"
        )
        request = AiEditRequest.model_validate(values["request"])
        await self._records.add(
            AiEditRecordCreate(
                site_id=project_id,
                user_id=user.id,
                instruction=values["instruction"],
                selection=request.target.model_dump(mode="json") if request.target else None,
                model=None,
                tier=None,
                outcome=AiEditOutcome.SKIPPED,
                summary="replaced or dismissed while waiting for an answer",
                attempts=values.get("attempt", 0),
            )
        )
        await self._uow.commit()

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

    async def _after_cancel(
        self, user: UserRead, request: AiEditRequest, run: EditRun, run_id: UUID, *, resuming: bool
    ) -> None:
        """Records a cancelled run. A commit that had started is finished first: then the change is on GitHub, so it's
        recorded as applied and added to the chat (where the SPA finds it and offers Undo). A resumed run that's
        stopped ends: its question doesn't come back."""
        committed: str | None = None
        if run.commit_task is not None and run.committing is not None:
            try:
                committed = await asyncio.shield(run.commit_task)
            except Exception:
                logger.warning("The commit of a cancelled AI edit failed", exc_info=True)
        if committed is None or run.committing is None:
            if resuming:
                try:
                    cancelled = RunResult(outcome=AiEditOutcome.CANCELLED.value)
                    ended = {"result": cancelled, "messages": [], "reply": ""}
                    await self._graph.aupdate_state(self._config(request.project_id), ended, as_node="finish")
                except Exception:
                    logger.exception("Couldn't end a stopped AI run that was waiting for an answer")
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


def _paused_question(snapshot: StateSnapshot) -> Question | None:
    """The question a paused run is waiting on, if the thread has one."""
    for interrupt in snapshot.interrupts:
        value = interrupt.value
        if isinstance(value, dict) and value.get("type") == "question":
            return Question.model_validate(value)
    return None
