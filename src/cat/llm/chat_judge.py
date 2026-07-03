"""ChatJudge — generic LLM oracle for chat / no-shell-oracle sessions.

Called after every Performer completion when no deterministic oracle exists.
Answers one question: did the response satisfactorily address the user's request?

Verdict:
  accept — genuine attempt, useful content → fulfil the contract
  retry  — missed the point, refused without trying, or no useful content
           → outer loop escalates to next model tier
"""
from __future__ import annotations

import json
from typing import Optional

from cat.contract.schemas import ChatJudgeSchema
from cat.llm.client import LLM, LLMResult

CHAT_JUDGE_SYSTEM_PROMPT = """\
You are a quality judge for an AI assistant's response.

Decide whether the response satisfactorily addresses the user's request.

Be lenient: accept if the response makes a genuine, relevant attempt — even if
imperfect or incomplete. Crucially: accept if the performer TRIED (made tool
calls) and honestly reported the result, even failure ("I tried X but Y wasn't
available"). Only choose retry when the response clearly:
  - missed the point of the request entirely
  - refused to attempt it without trying first
  - provided no useful content and made no attempt whatsoever

Output ONLY a JSON object:
{
  "verdict": "accept" | "retry",
  "reason":  "<one sentence explaining your verdict>"
}
"""


def call_chat_judge(
    obligation: str,
    response: str,
    llm: LLM,
    parent_event_id: Optional[str] = None,
) -> tuple[ChatJudgeSchema, LLMResult]:
    """Judge whether `response` satisfactorily addresses `obligation`."""
    user_content = (
        f"USER REQUEST:\n{obligation}\n\n"
        f"RESPONSE GIVEN:\n{response[:2000]}"
    )
    messages = [
        {"role": "system", "content": CHAT_JUDGE_SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]
    result = llm.chat(
        messages=messages,
        response_format={"type": "json_object"},
        role_tag="chat_judge",
        temperature=0.0,
        parent_event_id=parent_event_id,
    )
    if not result.text:
        return ChatJudgeSchema(verdict="accept", reason="judge returned no text"), result
    try:
        data = json.loads(result.text)
        return ChatJudgeSchema(**data), result
    except Exception:
        return ChatJudgeSchema(verdict="accept", reason="judge parse failed"), result
