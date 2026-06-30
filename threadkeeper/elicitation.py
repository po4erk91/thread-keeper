"""Small helpers for MCP form-mode elicitation.

The MCP elicitation schema subset is intentionally flat: one object with
primitive fields only. Keep the shared confirmation schema here so high-stakes
tools do not hand-roll subtly different prompts.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field


CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"


class ConfirmRejectForm(BaseModel):
    """Flat form-mode schema for high-stakes confirmations."""

    decision: str = Field(
        title="Decision",
        description="Choose confirm to apply the change, or reject to leave memory unchanged.",
        json_schema_extra={"enum": ["confirm", "reject"]},
    )


def _extra_get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict) and key in extra:
        return extra[key]
    extra = getattr(obj, "__pydantic_extra__", None)
    if isinstance(extra, dict) and key in extra:
        return extra[key]
    return getattr(obj, key, None)


def _elicitation_supports_form(elicitation: Any) -> bool:
    if elicitation is None:
        return False
    if isinstance(elicitation, dict):
        if "form" in elicitation:
            return elicitation.get("form") is not None
        # Back-compat in the draft spec: `elicitation: {}` means form mode.
        return "url" not in elicitation

    form = getattr(elicitation, "form", None)
    url = getattr(elicitation, "url", None)
    if form is not None:
        return True
    # Back-compat with SDK / hosts that model `elicitation: {}` as an object
    # whose known mode fields are all None.
    return url is None


def _capabilities_support_form_elicitation(capabilities: Any) -> bool:
    if capabilities is None:
        return False
    if isinstance(capabilities, dict):
        return _elicitation_supports_form(capabilities.get("elicitation"))
    return _elicitation_supports_form(getattr(capabilities, "elicitation", None))


def supports_form_elicitation(ctx: Context | None) -> bool:
    """Return whether this MCP request can receive form-mode elicitation.

    The current Python SDK records initialize-time client capabilities on the
    session. The draft spec also allows per-request capabilities in `_meta`; we
    check both so hosts can move without changing thread-keeper's call sites.
    """
    if ctx is None:
        return False

    request_context = getattr(ctx, "request_context", None)
    meta = getattr(request_context, "meta", None)
    meta_caps = _extra_get(meta, CLIENT_CAPABILITIES_META)
    if meta_caps is not None:
        return _capabilities_support_form_elicitation(meta_caps)

    session = getattr(request_context, "session", None)
    client_params = getattr(session, "_client_params", None)
    return _capabilities_support_form_elicitation(
        getattr(client_params, "capabilities", None)
    )


async def elicit_confirm_reject(ctx: Context | None, message: str) -> str | None:
    """Ask for a confirm/reject choice when the host supports elicitation.

    Returns:
      - ``None`` when elicitation is unsupported (callers should use their
        existing fallback behavior);
      - ``"confirm"`` / ``"reject"`` for accepted form submissions;
      - ``"decline"`` / ``"cancel"`` for host-level dismissal actions;
      - ``"invalid"`` if the client returns an out-of-schema value;
      - ``"error"`` if the client advertised elicitation but the request fails.
    """
    if not supports_form_elicitation(ctx):
        return None

    try:
        result = await ctx.elicit(message=message, schema=ConfirmRejectForm)
    except Exception:
        return "error"
    action = getattr(result, "action", "")
    if action != "accept":
        return action if action in {"decline", "cancel"} else "invalid"

    data = getattr(result, "data", None)
    if isinstance(data, dict):
        decision = data.get("decision")
    else:
        decision = getattr(data, "decision", None)
    return decision if decision in {"confirm", "reject"} else "invalid"
