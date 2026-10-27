import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command, StateSnapshot

from app.agents.ai_edit import EditRun, EditState, RunResult, compile_edit_graph, result_of, run_input, turn_for
from app.agents.create_site import compile_create_graph
from app.agents.thread import UserReply
from app.clients.fallback_model import FallbackModel, ModelSwitch
from app.constants.ai_edit import MAX_PREVIEW_ROUNDS, AiEditOutcome
from app.core.exceptions import AppError, ConflictError, ExternalServiceError
from app.core.limits import KeyLock
from app.repositories.site import AiEditRecordRepository, SiteRepository
from app.repositories.unit_of_work import UnitOfWork
from app.schemas.ai_edit import (
    AiEditAnswer,
    AiEditApplied,
    AiEditAwaitingResult,
    AiEditDebug,
    AiEditEscalated,
    AiEditNeedsInput,
    AiEditPreviewReport,
    AiEditRequest,
    AiModelSwitch,
    FileEdit,
    Question,
)
from app.schemas.ai_key import ModelChoice
from app.schemas.ai_thread import AiPausedRun, AiPreview, AiThreadRead, AiTurnRead
from app.schemas.site import AiEditRecordCreate, SiteSnapshot
from app.schemas.user import UserRead
from app.services.ai_runs import (
    DURABILITY,
    PickedModel,
    create_config,
    edit_config,
    end_paused,
    failure_summary,
    pause_kind,
    pause_value,
    prune,
    switches_read,
)
from app.services.site_service import SiteAccess, SiteConflictError, SiteNotFoundError, SiteService
from app.utils.file_edits import apply_file_edits

logger = logging.getLogger(__name__)

type AiEditResult = AiEditApplied | AiEditEscalated | AiEditNeedsInput | AiEditAwaitingResult


class AiEditRejectedError(ExternalServiceError):
    code = "ai_edit_rejected"
    message = "The AI couldn't make a change that works. Try asking in a different way."


class AiRunInProgressError(ConflictError):
    code = "ai_run_in_progress"
    message = "The AI is still working on this site. Wait for it to finish, or stop it first."


class AiEditBrokePreviewError(ExternalServiceError):
    """The AI's edits broke the preview, and its fixes did too. Nothing was committed; the SPA undoes them."""

    code = "ai_edit_broke_preview"
    message = "The AI's change broke the preview, and it couldn't fix it. Nothing was saved. Try asking differently."


class AiRunSupersededError(ConflictError):
    code = "ai_run_superseded"
    message = "That question was already answered or replaced. Reload the chat."


class ModelSource(Protocol):
    """Picks the model for a request, with the ones it switches to if it stops answering (`AiKeyService`)."""

    async def model_for(
        self, user_id: UUID, choice: ModelChoice | None, *, on_switch: Callable[[ModelSwitch], None] | None = None
    ) -> FallbackModel: ...

    async def key_refused(self, user_id: UUID, key_id: UUID, *, since: datetime) -> None: ...


class AiEditService:
    """AI edits on the LangGraph agent (`app/agents/ai_edit`): the agent reads the site's code from its repo at the
    commit the browser is at, asks the model for search/replace edits, checks them, has the preview try them, and
    commits them. Prompts and the model call stay on the server. It also shows the site's chat (the edit thread's and
    the create thread's turns, and a run that's waiting), and Dismiss and Start over for both agents;
    `SiteCreateService` runs the creations. `ai_edit_requests` keeps each request as a row."""

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
        self._create_graph = compile_create_graph(checkpointer)
        self._runs = runs
        self._debug = debug

    async def edit(self, user: UserRead, request: AiEditRequest) -> AiEditResult:
        """Makes the change and commits it to the site's repo, hands the request to Tier 2 (by rule, or because the
        model escalated), or pauses on a question for the user (answered with `answer`). The model is the one the user
        picked (`request.model`), or makable's own. If the user's pick stops answering, another of their models takes
        over (the answer's `model_switches` say which). The earlier requests in the prompt come from the site's thread.
        A run paused on a question is skipped: the new instruction replaces it.

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
            AiKeyInvalidError: The provider refused the user's key (and the ones switched to failed too). Keys refused
                during the request are marked invalid.
            ModelRateLimitedError: The provider is rate limiting the user's key (and the models switched to failed).
            ModelUnavailableError: The model couldn't be reached or answered with an error (and the models switched to
                failed).
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
            if paused is None or pause_kind(paused) != "question" or paused.values.get("run_id") != str(body.run_id):
                raise AiRunSupersededError()
            request = AiEditRequest.model_validate(
                {**paused.values["request"], "base_commit": body.base_commit, "screenshot": body.screenshot}
            )
            reply = UserReply(answer=body.answer, chat=body.chat)
            resume: Command[Any] = Command(resume=reply, update={"base_commit": body.base_commit})
            return await self._invoke(user, request, site, body.run_id, resume, resuming=True)

    async def report(self, user: UserRead, project_id: UUID, body: AiEditPreviewReport) -> AiEditResult:
        """Continues a run waiting for the preview with how it went: it works, and the edits are committed; or it
        failed, and the model tries again with the error (or the request gives up).

        Raises:
            AiRunSupersededError: No run is waiting for that preview any more.
            AiEditBrokePreviewError: The preview failed on every try.
            The errors of `edit`.
        """
        async with self._runs.hold((user.id, project_id)):
            paused = await self._paused(project_id)
            if paused is None or pause_kind(paused) != "preview" or paused.values.get("run_id") != str(body.run_id):
                raise AiRunSupersededError()
            base = paused.values["base_commit"]
            site = await self._site_at(user, project_id, base)
            request = AiEditRequest.model_validate({**paused.values["request"], "base_commit": base})
            resume: Command[Any] = Command(resume=body.model_dump(mode="json"))
            return await self._invoke(user, request, site, body.run_id, resume, resuming=True)

    async def dismiss(self, user: UserRead, project_id: UUID) -> None:
        """Ends the run that's waiting (for the user's answer or the preview), the edit's or the creation's (Dismiss):
        it's recorded as skipped. Nothing waiting is fine.

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
                config = create_config(project_id)
                snapshot = await self._create_graph.aget_state(config)
                await end_paused(
                    self._create_graph,
                    config,
                    snapshot,
                    records=self._records,
                    uow=self._uow,
                    user_id=user.id,
                    project_id=project_id,
                )

    async def thread(self, user: UserRead, project_id: UUID) -> AiThreadRead:
        """The site's chat, oldest first, and the run waiting for the user's answer or the preview, if one is (it
        survives a closed tab). A site with no requests yet has an empty one. A run waiting for the preview comes with
        the files to try, unless the repo can't be read now.

        Raises:
            SiteNotFoundError: The site is another user's.
        """
        site = await self._sites.get(project_id)
        if site is not None and site.user_id != user.id:
            raise SiteNotFoundError()
        if site is None:
            return AiThreadRead(project_id=project_id, status="idle", turns=[])
        snapshot = await self._graph.aget_state(self._config(project_id))
        creation = await self._create_graph.aget_state(create_config(project_id))
        both = [*creation.values.get("turns", []), *snapshot.values.get("turns", [])]
        turns = sorted((AiTurnRead.model_validate(t) for t in both), key=lambda t: t.created_at)
        asked = pause_value(creation)
        if asked is not None:
            run = AiPausedRun(
                run_id=creation.values["run_id"],
                mode="create",
                instruction=creation.values["prompt"],
                target=None,
                question=Question.model_validate(asked),
                questions=creation.values.get("questions", []),
                steps=creation.values.get("steps", []),
            )
            return AiThreadRead(project_id=project_id, status="needs_input", turns=turns, run=run)
        kind = pause_kind(snapshot)
        if kind is None:
            return AiThreadRead(project_id=project_id, status="idle", turns=turns)
        values = snapshot.values
        run = AiPausedRun(
            run_id=values["run_id"],
            instruction=values["instruction"],
            target=values.get("target"),
            questions=values.get("questions", []),
        )
        if kind == "question":
            run.question = Question.model_validate(pause_value(snapshot))
            return AiThreadRead(project_id=project_id, status="needs_input", turns=turns, run=run)
        # The edits are in the state; the files they make are worked out again from the base commit. If the repo can't
        # be read now, the chat still loads, without the edits to try (the run can still be dismissed).
        try:
            access = await self._site_service.access(user, project_id)
            files = await self._preview_files(await access.snapshot(values["base_commit"]), values)
        except AppError as err:
            logger.warning("Couldn't rebuild a paused run's preview", extra={"code": err.code})
        else:
            run.preview = AiPreview(summary=result_of(values).get("summary", ""), files=files)
        return AiThreadRead(project_id=project_id, status="awaiting_result", turns=turns, run=run)

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
            await self._checkpointer.adelete_thread(edit_config(project_id)["configurable"]["thread_id"])
            await self._checkpointer.adelete_thread(create_config(project_id)["configurable"]["thread_id"])
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
        """Runs the graph (a new run, or a paused one resumed) and turns how it ended into the answer. Keys the
        providers refused during the run are marked invalid, however it ended."""
        picked = PickedModel(lambda: self._models.model_for(user.id, request.model))
        run = EditRun(request, pick_model=picked.pick, site=site)
        config = self._config(request.project_id)
        try:
            state = await self._graph.ainvoke(graph_input, config, context=run, durability=DURABILITY)
        except asyncio.CancelledError:
            await self._after_cancel(user, request, run, resuming=resuming)
            raise
        except Exception as err:
            code = err.code if isinstance(err, AppError) else type(err).__name__
            # A resumed run that fails ends too: the answer was saved with it, so its question doesn't come back.
            await self._record_quietly(user, request, run, AiEditOutcome.FAILED, summary=failure_summary(code, err))
            raise
        finally:
            await self._mark_refused(user, picked)
        switches = switches_read(picked.switches)
        if state.get("__interrupt__"):
            value = state["__interrupt__"][0].value
            debug = self._debug_info(run, state.get("attempt", 0))
            if value.get("type") == "preview":
                files = await self._preview_files(
                    SiteSnapshot(commit_sha="", files=await run.files(), binary=[]), state
                )
                summary = result_of(state).get("summary", "")
                return AiEditAwaitingResult(
                    run_id=run_id, summary=summary, files=files, model_switches=switches, debug=debug
                )
            question = Question.model_validate(value)
            return AiEditNeedsInput(run_id=run_id, question=question, model_switches=switches, debug=debug)
        return await self._finish(user, request, run, run_id, state, switches)

    async def _mark_refused(self, user: UserRead, picked: PickedModel) -> None:
        """Marks the keys the providers refused during a run invalid, so the menu stops offering them. A failure is
        logged: the run's answer doesn't depend on it."""
        since = picked.picked_at
        if since is None:
            return
        for key_id in picked.refused_keys:
            try:
                await self._models.key_refused(user.id, key_id, since=since)
            except Exception:
                logger.exception("Couldn't mark a refused AI key invalid")
                await self._uow.rollback()

    @staticmethod
    async def _preview_files(base: SiteSnapshot, values: dict[str, Any]) -> dict[str, str | None]:
        """The files a run's checked edits make, from the base commit's: what the preview tries."""
        edits = [FileEdit.model_validate(e) for e in result_of(values).get("edits", [])]
        applied = apply_file_edits(base.files, edits)
        return {path: applied.files[path] for path in applied.changed}

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
        """The thread's state, if a run is waiting (for the user's answer, or for the preview)."""
        snapshot = await self._graph.aget_state(self._config(project_id))
        return snapshot if pause_kind(snapshot) is not None else None

    async def _skip_paused(self, user: UserRead, project_id: UUID) -> None:
        """Ends an edit run that's waiting (for an answer or the preview; a new instruction or Dismiss replaces it),
        recorded as skipped."""
        snapshot = await self._graph.aget_state(self._config(project_id))
        await end_paused(
            self._graph,
            self._config(project_id),
            snapshot,
            records=self._records,
            uow=self._uow,
            user_id=user.id,
            project_id=project_id,
        )

    async def _finish(
        self,
        user: UserRead,
        request: AiEditRequest,
        run: EditRun,
        run_id: UUID,
        state: dict[str, Any],
        switches: list[AiModelSwitch] | None,
    ) -> AiEditApplied | AiEditEscalated:
        result = result_of(state)
        outcome = AiEditOutcome(result["outcome"])
        attempts = state.get("attempt", 0)
        await self._record(user, request, run, outcome, result=result, attempts=attempts, model=state.get("model"))
        await prune(self._checkpointer, self._config(request.project_id))
        if outcome == AiEditOutcome.REJECTED:
            problem = result.get("problem", "")
            details = {"problem": problem[:300]} if self._debug else None
            if state.get("preview_rounds", 0) > MAX_PREVIEW_ROUNDS:
                raise AiEditBrokePreviewError(details=details)
            raise AiEditRejectedError(details=details)
        debug = self._debug_info(run, attempts, state.get("model"))
        if outcome == AiEditOutcome.ESCALATED:
            reason = result.get("reason", "")
            return AiEditEscalated(run_id=run_id, reason=reason, model_switches=switches, debug=debug)
        return AiEditApplied(
            run_id=run_id,
            summary=result.get("summary", ""),
            commit_sha=result.get("commit_sha", ""),
            files=run.changed_files,
            model_switches=switches,
            debug=debug,
        )

    async def _after_cancel(self, user: UserRead, request: AiEditRequest, run: EditRun, *, resuming: bool) -> None:
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
        result: RunResult = {**result_of(run.committing), "commit_sha": committed}
        try:
            finished = EditState(**{**run.committing, "result": result})
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
        model: str | None = None,
    ) -> None:
        """Writes the request's row and marks the site as used. `model` is the model the run used (from its state),
        else the one this request picked."""
        if result is not None:
            summary = result.get("summary") or result.get("reason") or result.get("problem")
        await self._records.add(
            AiEditRecordCreate(
                site_id=request.project_id,
                user_id=user.id,
                instruction=request.instruction,
                selection=request.target.model_dump(mode="json") if request.target else None,
                model=model or (run.model.name if run.model else None),
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

    @staticmethod
    def _config(project_id: UUID) -> Any:
        return edit_config(project_id)

    def _debug_info(self, run: EditRun, attempts: int, model: str | None = None) -> AiEditDebug | None:
        if not self._debug:
            return None
        name = model or (run.model.name if run.model else "")
        return AiEditDebug(model_input=run.model_input, attempts=attempts, model=name)
