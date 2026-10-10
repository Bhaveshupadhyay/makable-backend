import json
import logging

from pydantic import TypeAdapter, ValidationError

from app.clients.model import ModelClient
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
from app.schemas.chat import ChatMessage
from app.services.ai_edit_prompt import SYSTEM_PROMPT, build_user_prompt, rejection_prompt
from app.utils.file_edits import AppliedEdits, FileEditError, apply_file_edits
from app.utils.portfolio_source import PortfolioSourceError, parse_portfolio_source
from app.utils.syntax_check import css_braces_balanced, script_language, script_syntax_error

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
    forbidden = next((e for e in edits if FORBIDDEN_PATH.search(e.path)), None)
    if forbidden:
        raise FileEditError(f"{forbidden.path} can't be edited")
    applied = apply_file_edits({f.path: f.content for f in request.files}, edits)
    if not applied.changed:
        raise FileEditError("the edits don't change any file; make the requested change, or escalate if you can't")
    for path in applied.changed:
        problem = _problem_in(path, applied.files[path], request.template.content_path)
        if problem:
            raise FileEditError(f"{path}: {problem}")
    return applied


def _problem_in(path: str, code: str, content_path: str) -> str | None:
    if path == content_path:
        try:
            parse_portfolio_source(code)
        except PortfolioSourceError as err:
            return str(err)
        return None
    language = script_language(path)
    if language is not None:
        return script_syntax_error(code, language)
    if path.endswith(".css") and not css_braces_balanced(code):
        return "unbalanced braces"
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


class AiEditService:
    """Tier 1 AI edits: asks the model for search/replace edits on the files the browser sent, and checks
    them before answering. Prompts and the model call stay on the server."""

    def __init__(self, model: ModelClient, *, debug: bool = False) -> None:
        self._model = model
        self._debug = debug

    async def edit(self, request: AiEditRequest) -> AiEditTier1 | AiEditTier2:
        """Answers Tier 1 edits, or hands the request to Tier 2 (by rule, or because the model escalated).

        A rejected answer is sent back once with the error, because free models often get the shape or the
        search text wrong the first time.

        Raises:
            ModelUnavailableError: The model couldn't be reached or answered with an error.
            AiEditRejectedError: The model's answer was rejected on every attempt.
        """
        rule = classify(request)
        if rule:
            return AiEditTier2(reason=rule, debug=self._debug_info("", 0))
        user_prompt = build_user_prompt(request)
        # One user turn, no system role: the free models behind OmniRoute ignore a system message and answer
        # in their own agent format, but follow the same text in the user turn.
        messages = [ChatMessage(role="user", content=f"{SYSTEM_PROMPT}\n\n{user_prompt}")]
        problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            reply = await self._model.complete(messages)
            try:
                answer = _ANSWER.validate_python(extract_json(reply))
                if isinstance(answer, ModelEscalation):
                    return AiEditTier2(reason=answer.escalate, debug=self._debug_info(user_prompt, attempt))
                check_edits(request, answer.edits)
            except (ValueError, FileEditError) as err:  # ValidationError and JSONDecodeError are ValueErrors.
                problem = _describe(err)
                logger.info("AI edit answer rejected", extra={"attempt": attempt, "problem": problem[:300]})
                messages += [
                    ChatMessage(role="assistant", content=reply),
                    ChatMessage(role="user", content=rejection_prompt(problem)),
                ]
                continue
            return AiEditTier1(summary=answer.summary, edits=answer.edits, debug=self._debug_info(user_prompt, attempt))
        raise AiEditRejectedError(details={"problem": problem[:300]} if self._debug else None)

    def _debug_info(self, model_input: str, attempts: int) -> AiEditDebug | None:
        if not self._debug:
            return None
        return AiEditDebug(model_input=model_input, attempts=attempts, model=self._model.name)


def _describe(err: Exception) -> str:
    if isinstance(err, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'the answer'}: {e['msg']}" for e in err.errors()[:5])
    return str(err)
