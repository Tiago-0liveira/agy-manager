"""Decode final responses while leaving the full provider stream in the trace."""
from __future__ import annotations
import json
from typing import Any


def decode_response(stream: str) -> tuple[str, str | None, dict[str, Any] | None]:
    response: Any = None
    conversation_id = None
    usage = None
    terminal = False
    denied_actions: list[Any] = []
    last_step_error: str | None = None
    diagnostic_message: str | None = None

    for line in stream.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        try:
            event = json.loads(trimmed)
        except ValueError:
            # Preserve diagnostics in stdout.log; they are not a final response.
            if "permission" in trimmed.lower() or "denied" in trimmed.lower() or trimmed.startswith("jetski:"):
                diagnostic_message = trimmed
            continue
        if not isinstance(event, dict):
            continue

        conversation_id = (event.get("conversation_id") or event.get("conversationId")
                           or event.get("session_id") or conversation_id)
        kind = event.get("event") or event.get("step_type") or event.get("type")

        # Track errors from step_update (e.g. tool execution errors or permission failures)
        su = event.get("step_update") if isinstance(event.get("step_update"), dict) else event
        if isinstance(su, dict):
            if su.get("state") == "ERROR" or su.get("error"):
                tool_info = su.get("tool_info") if isinstance(su.get("tool_info"), dict) else {}
                err_data = su.get("error") or tool_info.get("error")
                if isinstance(err_data, dict):
                    err_msg = err_data.get("message") or err_data.get("error") or str(err_data)
                elif err_data:
                    err_msg = str(err_data)
                else:
                    err_msg = str(su.get("message") or su)
                last_step_error = err_msg

        if kind == "error" or event.get("is_error"):
            raise ValueError(str(event.get("error") or event.get("message") or event.get("result") or event))

        if kind not in ("result", "turn_complete", "response"):
            continue

        terminal = True
        res_payload = event.get("result", event.get("response", event.get("output")))
        usage = event.get("usage") or usage

        if isinstance(res_payload, dict):
            conversation_id = (res_payload.get("conversation_id") or res_payload.get("conversationId")
                               or conversation_id)
            usage = res_payload.get("usage") or usage
            if str(res_payload.get("status", "")).upper() == "ERROR":
                raise ValueError(str(res_payload.get("error") or "Provider returned ERROR"))
            if res_payload.get("denied_actions"):
                denied_actions.extend(res_payload["denied_actions"])
            response = res_payload.get("response", res_payload.get("output", res_payload.get("text", res_payload)))
        else:
            response = res_payload

        if event.get("denied_actions"):
            denied_actions.extend(event["denied_actions"])

    if not terminal:
        raise ValueError("Subprocess stream ended without a final result event")

    is_empty_response = (
        response is None
        or (isinstance(response, str) and not response.strip())
        or response == ""
    )

    if is_empty_response:
        if denied_actions:
            action_names = [
                a.get("display_name") or a.get("action") or str(a)
                if isinstance(a, dict) else str(a)
                for a in denied_actions
            ]
            names_str = ", ".join(action_names)
            if last_step_error:
                raise ValueError(f"Tool permission denied for {names_str}: {last_step_error}")
            if diagnostic_message:
                raise ValueError(f"Tool permission denied for {names_str}: {diagnostic_message}")
            raise ValueError(f"Tool permission denied: {names_str} auto-denied in headless mode")

        if last_step_error:
            raise ValueError(f"Empty response after tool error: {last_step_error}")
        if diagnostic_message:
            raise ValueError(f"Empty response from provider: {diagnostic_message}")
        raise ValueError("Empty response in final result event")

    text = response if isinstance(response, str) else json.dumps(response)
    return text, conversation_id, usage



def extract_activity_description(line: str, *, max_length: int = 120) -> str | None:
    """Extract a short, safe lifecycle description from one stream-json line.

    Unknown provider events intentionally return None and remain available in
    the raw attempt trace instead of polluting the main TTY.
    """
    try:
        event = json.loads(line.strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None

    kind = str(event.get("event") or event.get("step_type") or event.get("type") or "").lower()
    if kind in {"result", "turn_complete", "response", "error"}:
        return None

    update = event.get("step_update") if isinstance(event.get("step_update"), dict) else event
    if not isinstance(update, dict):
        return None

    candidates: list[str] = []
    for key in ("activity", "summary", "message", "title", "description"):
        value = update.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    tool = update.get("tool_info")
    if isinstance(tool, dict):
        tool_name = str(tool.get("display_name") or tool.get("name") or tool.get("action") or "").strip()
        target = str(
            tool.get("path")
            or tool.get("file")
            or tool.get("query")
            or tool.get("target")
            or ""
        ).strip()
        if tool_name:
            candidates.insert(0, f"{tool_name} {target}".strip())

    if not candidates:
        return None

    activity = " ".join(candidates[0].split())
    if not activity:
        return None
    return activity[:max_length]
