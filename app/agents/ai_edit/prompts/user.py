"""The request as the model reads it: earlier requests, the instruction, the selection and the files, all as tagged
data."""

from app.agents.ai_edit.state import Turn
from app.constants.ai_edit import MAX_HISTORY, MAX_HISTORY_REPLY
from app.schemas.ai_edit import AiEditRequest, AiEditTarget, AiEditTurn

WHOLE_PAGE = "<selected_element>none: the request is about the whole page</selected_element>"


def build_user_prompt(request: AiEditRequest, history: list[AiEditTurn] | None = None) -> str:
    """The request as tagged data: earlier requests, instruction, selection, template, file tree and file
    contents. `history` is the thread's (the graph passes it); without it, the request's own."""
    template = request.template
    history = request.history if history is None else history
    parts = [
        *([_describe_history(history)] if history else []),
        f"<instruction>\n{request.instruction}\n</instruction>",
        _describe_target(request.target) if request.target else WHOLE_PAGE,
        f'<template name="{_attr(template.name)}" kind="{template.kind}" content_file="{template.content_path}" />',
        "<file_tree>\n" + "\n".join(request.file_tree) + "\n</file_tree>",
        *(f'<file path="{f.path}" reason="{_attr(f.reason)}">\n{f.content}\n</file>' for f in request.files),
    ]
    return "\n\n".join(parts)


def _describe_target(target: AiEditTarget) -> str:
    lines = [f"- Element: <{target.tag}>{f' showing "{target.text}"' if target.text else ''}"]
    if target.section:
        section = target.section
        id_attr = f' id="{_attr(section.id)}"' if section.id else ""
        heading = f' with heading "{section.heading}"' if section.heading else ""
        lines.append(f"- Inside: <{section.tag}{id_attr}>{heading}")
    if target.content_path:
        lines.append(f"- Content path: {target.content_path}")
    lines.append(f"- DOM path: {target.selector}")
    return "<selected_element>\n" + "\n".join(lines) + f"\n<html>\n{target.html}\n</html>\n</selected_element>"


def _describe_history(history: list[AiEditTurn]) -> str:
    entries = []
    for i, turn in enumerate(history, start=1):
        target = f" (selected: {turn.target})" if turn.target else ""
        entries.append(f"{i}. User{target}: {turn.instruction}\n   Result: {turn.reply}")
    return "<earlier_requests>\n" + "\n".join(entries) + "\n</earlier_requests>"


def _attr(value: str) -> str:
    return value.translate({ord(c): None for c in '"<>&'})


def earlier_requests(turns: list[Turn]) -> list[AiEditTurn]:
    """The thread's last turns, for the prompt's `<earlier_requests>`."""
    return [
        AiEditTurn(instruction=t["instruction"], target=t["target"], reply=t["reply"][:MAX_HISTORY_REPLY])
        for t in turns[-MAX_HISTORY:]
    ]
