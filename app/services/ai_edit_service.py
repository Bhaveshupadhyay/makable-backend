import json
import logging
from typing import Protocol
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from app.clients.model import AiKeyInvalidError, ModelClient
from app.constants.ai_edit import FORBIDDEN_PATH, MAX_ATTEMPTS, SITE_WIDE
from app.core.exceptions import ExternalServiceError
from app.schemas.ai_edit import (
    AiEditDebug,
    AiEditRequest,
    AiEditTier1,
    AiEditTier2,
    FileEdit,
    ModelAnswer,
    ModelEdits,
    ModelEscalation,
)
from app.schemas.ai_key import ModelChoice
from app.schemas.chat import ChatMessage
from app.services.ai_edit_prompt import SYSTEM_PROMPT, build_user_prompt, repair_prompt
from app.services.ai_edit_repair import rejection_report
from app.utils.file_edits import AppliedEdits, FileEditError, apply_file_edits
from app.utils.portfolio_source import PortfolioSourceError, parse_portfolio_source
from app.utils.syntax_check import locate_syntax_error, locate_unbalanced_brace, script_language

logger = logging.getLogger(__name__)

_ANSWER: TypeAdapter[ModelEdits | ModelEscalation] = TypeAdapter(ModelAnswer)


class AiEditRejectedError(ExternalServiceError):
    code = "ai_edit_rejected"
    message = "The AI couldn't make a change that works. Try asking in a different way."


def classify(request: AiEditRequest) -> str | None:
    """The reason a request goes straight to Tier 2 without a model call, or None for Tier 1."""
    if not request.files:
        return "No file was found for this change."
    match = SITE_WIDE.search(request.instruction)
    if match:
        return f'"{match.group(0)}" sounds like a change across the site.'
    return None


def check_edits(request: AiEditRequest, edits: list[FileEdit]) -> AppliedEdits:
    """Dry-runs the edits on the files that were sent, the way the browser will apply them, and checks the
    result still parses: the content file must stay a valid portfolio, scripts must have no syntax errors, and
    CSS braces must balance. Edits that change nothing are rejected too, so the user is never told about a
    change that wasn't made.

    Raises:
        FileEditError: An edit can't be applied, or it breaks a file. The message is written for the model.
    """
    forbidden = next((i for i, e in enumerate(edits, start=1) if FORBIDDEN_PATH.search(e.path)), None)
    if forbidden:
        path = edits[forbidden - 1].path
        raise FileEditError(f"{path} can't be edited", kind="forbidden", edit=forbidden, path=path)
    applied = apply_file_edits({f.path: f.content for f in request.files}, edits)
    if not applied.changed:
        raise FileEditError(
            "the edits don't change any file; make the requested change, or escalate if you can't", kind="no_change"
        )
    same = next((i for i, e in enumerate(edits, start=1) if e.search == e.replace), None)
    if same:
        path = edits[same - 1].path
        raise FileEditError(
            f"edit {same} ({path}): its search and replace are the same, so it changes nothing",
            kind="no_change",
            edit=same,
            path=path,
        )
    for path in applied.changed:
        found = _problem_in(path, applied.files[path], request.template.content_path)
        if found:
            problem, offset = found
            raise FileEditError(f"{path}: {problem}", kind="broken_file", path=path, offset=offset)
    return applied


def _problem_in(path: str, code: str, content_path: str) -> tuple[str, int | None] | None:
    """What's wrong with an edited file, and where (a character offset) when that's known."""
    if path == content_path:
        try:
            parse_portfolio_source(code)
        except PortfolioSourceError as err:
            return str(err), None
        return None
    language = script_language(path)
    if language is not None:
        return locate_syntax_error(code, language)
    if path.endswith(".css"):
        return locate_unbalanced_brace(code)
    return None


def extract_json(text: str) -> object:
    """Pulls the JSON object out of a reply that may be wrapped in fences or prose.

    Raises:
        ValueError: The reply has no JSON object.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("The model did not return JSON")
    return json.loads(text[start : end + 1])


class ModelSource(Protocol):
    """Picks the model for a request (`AiKeyService`)."""

    async def model_for(self, user_id: UUID, choice: ModelChoice | None) -> ModelClient: ...

    async def key_refused(self, user_id: UUID, key_id: UUID) -> None: ...


class AiEditService:
    """Tier 1 AI edits: asks the model for search/replace edits on the files the browser sent, and checks
    them before answering. Prompts and the model call stay on the server."""

    def __init__(self, models: ModelSource, *, debug: bool = False) -> None:
        self._models = models
        self._debug = debug

    async def edit(self, user_id: UUID, request: AiEditRequest) -> AiEditTier1 | AiEditTier2:
        """Answers Tier 1 edits, or hands the request to Tier 2 (by rule, or because the model escalated).
        The model is the one the user picked (`request.model`), or makable's own.

        A rejected answer is sent back once with a repair prompt and a report of what failed and where (the
        file's real text at that place), because free models often get the shape or the search text wrong the
        first time.

        Raises:
            AiModelRequiredError: Nothing was picked and there's no makable model.
            AiKeyNotFoundError: The picked key isn't the user's (any more).
            AiModelNotEnabledError: The picked model isn't turned on for the key.
            AiKeyInvalidError: The provider refused the user's key. It's marked invalid.
            ModelRateLimitedError: The provider is rate limiting the user's key.
            ModelUnavailableError: The model couldn't be reached or answered with an error.
            AiEditRejectedError: The model's answer was rejected on every attempt.
        """
        rule = classify(request)
        if rule:
            return AiEditTier2(reason=rule, debug=self._debug_info("", 0, None))
        model = await self._models.model_for(user_id, request.model)
        try:
            return await self._edit(request, model)
        except AiKeyInvalidError:
            if request.model is not None:
                await self._models.key_refused(user_id, request.model.key_id)
            raise

    async def _edit(self, request: AiEditRequest, model: ModelClient) -> AiEditTier1 | AiEditTier2:
        user_prompt = build_user_prompt(request)
        # One user turn, no system role: the free models behind OmniRoute ignore a system message and answer
        # in their own agent format, but follow the same text in the user turn.
        messages = [ChatMessage(role="user", content=f"{SYSTEM_PROMPT}\n\n{user_prompt}")]
        problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            answer: ModelEdits | ModelEscalation | None = None
            reply = await model.complete(messages)
            try:
                answer = _ANSWER.validate_python(extract_json(reply))
                if isinstance(answer, ModelEscalation):
                    return AiEditTier2(reason=answer.escalate, debug=self._debug_info(user_prompt, attempt, model))
                check_edits(request, answer.edits)
            except (ValueError, FileEditError) as err:  # ValidationError and JSONDecodeError are ValueErrors.
                problem = _describe(err)
                logger.info("AI edit answer rejected", extra={"attempt": attempt, "problem": problem[:300]})
                # A separate prompt for the retry: how to repair an answer, and exactly what failed and where.
                edits = answer.edits if isinstance(answer, ModelEdits) else None
                messages += [
                    ChatMessage(role="assistant", content=reply),
                    ChatMessage(role="user", content=repair_prompt(rejection_report(request, edits, err))),
                ]
                continue
            debug = self._debug_info(user_prompt, attempt, model)
            return AiEditTier1(summary=answer.summary, edits=answer.edits, debug=debug)
        raise AiEditRejectedError(details={"problem": problem[:300]} if self._debug else None)

    def _debug_info(self, model_input: str, attempts: int, model: ModelClient | None) -> AiEditDebug | None:
        if not self._debug:
            return None
        return AiEditDebug(model_input=model_input, attempts=attempts, model=model.name if model else "")


def _describe(err: Exception) -> str:
    if isinstance(err, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'the answer'}: {e['msg']}" for e in err.errors()[:5])
    return str(err)
