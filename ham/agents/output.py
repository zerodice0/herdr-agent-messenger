"""Compact agent payloads and deterministic terminal-output deltas."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Protocol
import zlib


CURSOR_VERSION = 1
CURSOR_TAIL_BYTES = 4096
MAX_CURSOR_TOKEN_BYTES = 32 * 1024
MAX_CURSOR_STATE_BYTES = 16 * 1024
MIN_PARTIAL_OVERLAP_BYTES = 64


class AgentSummary(Protocol):
    host: str
    name: str
    pane_id: str
    workspace_id: str
    workspace_label: str
    status: str
    local: bool


class InvalidOutputCursor(ValueError):
    """Raised when an output cursor cannot be decoded safely."""


@dataclass(frozen=True)
class _CursorState:
    stream: str
    snapshot_size: int
    snapshot_hash: str
    tail: bytes


@dataclass(frozen=True)
class OutputDelta:
    """One bounded output result plus the cursor for the full new snapshot."""

    output: str
    truncated: bool
    cursor: str
    cursor_status: str
    delta: bool


def agent_address(agent: AgentSummary) -> str:
    """Return the stable human-readable address for one observed agent."""

    host = "local" if agent.local else agent.host
    target = agent.name or agent.pane_id
    return f"{host}/{target}"


def compact_agent_payload(agent: AgentSummary) -> dict[str, str]:
    """Return only fields needed to select and inspect a current agent."""

    return {
        "address": agent_address(agent),
        "status": agent.status,
        "workspace": agent.workspace_label or agent.workspace_id,
    }


def compact_board_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project stored status for agents without changing TUI or peer records."""

    rows = payload["agents"]
    multi_repo = len({row.get("repo_id") for row in rows}) > 1
    agents = []
    for row in rows:
        agent = {
            "address": f"{row['host']}/{row.get('alias') or row['terminal_id']}",
            "host": row["host"],
            "workspace": row.get("workspace_label") or row["workspace_id"],
            "repo": row["repo_id"].rsplit("/", 1)[-1],
        }
        for key in ("turn_state", "status", "role", "unread", "phase", "summary",
                    "stale", "extension_present", "client"):
            if key in row:
                agent[key] = row[key]
        if multi_repo:
            agent["repo_id"] = row["repo_id"]
        for key in ("devices", "servers"):
            if key in row:
                agent[key] = len(row[key])
        agents.append(agent)
    compact = {key: value for key, value in payload.items()
               if key not in {"agents", "hosts", "workspaces"}}
    compact["agents"] = agents
    if "hosts" in payload:
        hosts = []
        for row in payload["hosts"]:
            host = {key: row[key] for key in ("host", "stale", "error", "unread") if key in row}
            resources = row.get("resources")
            if isinstance(resources, Mapping):
                host["resources"] = {
                    key: len(resources[key])
                    for key in ("servers_unowned", "dead_serve", "stale_devices")
                    if key in resources
                }
                if "error" in resources:
                    host["resources"]["error"] = resources["error"]
            hosts.append(host)
        compact["hosts"] = hosts
    return compact


DISMISSAL_FIELDS = ("message_id", "actor_alias", "actor_terminal_id", "reason", "dismissed_at")


def compact_dismissal(audit: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Keep the stable sender-resolution fields, not the retained failure evidence."""

    if audit is None:
        return None
    return {key: audit[key] for key in DISMISSAL_FIELDS if key in audit}


def compact_inbox_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep readable mail and safe reply context, not delivery diagnostics."""

    messages = []
    for row in payload["messages"]:
        message = {
            key: row[key]
            for key in ("message_id", "thread_id", "kind", "priority", "state", "body",
                        "reply_to", "hop", "truncated", "body_truncated", "body_omitted")
            if row.get(key) is not None
        }
        if row.get("dismissal") is not None:
            message["dismissal"] = compact_dismissal(row["dismissal"])
        message["sender"] = f"{row['origin_host']}/{row['sender_label']}"
        for key in ("hold_reason", "withdraw_requested"):
            if row.get(key):
                message[key] = row[key]
        messages.append(message)
    return {**{key: value for key, value in payload.items()
               if key not in {"terminal_id", "messages"} and value is not None},
            "messages": messages}


def compact_dismiss_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return sender-resolution audits without retained failure or identity evidence."""

    return {**{key: value for key, value in payload.items() if key != "dismissals"},
            "dismissals": [compact_dismissal(audit) for audit in payload["dismissals"]]}


def _compact_receipt(receipt: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Keep the identity, current state and nonempty hold/withdraw evidence of a receipt."""

    compact = {key: receipt[key] for key in keys if key in receipt}
    compact["state"] = receipt["state"]
    for key in ("hold_reason", "withdraw_requested"):
        if receipt.get(key):
            compact[key] = receipt[key]
    return compact


def compact_send_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the queued message's identity, resolved recipient and advisory fields."""

    compact = _compact_receipt(payload, ("message_id", "thread_id", "recipient_address"))
    if payload.get("fyi_turn_end"):
        compact["fyi_turn_end"] = True
    for key in ("fyi_wake_after_seconds", "warnings"):
        if payload.get(key):
            compact[key] = payload[key]
    return compact


def compact_handle_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return only each handled message's identity and final state."""

    return {**{key: value for key, value in payload.items() if key != "receipts"},
            "receipts": [_compact_receipt(receipt, ("message_id",)) for receipt in payload["receipts"]]}


def compact_status_set_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Report display refresh only when it failed; success is the unremarkable default."""

    compact = {key: value for key, value in payload.items() if key != "display"}
    display = payload.get("display")
    if isinstance(display, Mapping) and display.get("failed"):
        compact["display"] = {"failed": display["failed"]}
    return compact


def compact_repo_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Brief repository participation and current agents; settings keep only their values."""

    compact = {key: payload[key] for key in ("repo_id", "host", "workspace_id", "mode")}
    if payload.get("requested_workspace_id") != payload["workspace_id"]:
        compact["requested_workspace_id"] = payload.get("requested_workspace_id")
    for key in ("home_host", "main", "main_source", "members", "computed_main",
                "conflicts", "propagation_pending"):
        if payload.get(key):
            compact[key] = payload[key]
    extra = {name: setting["value"] for name, setting in payload["settings"].items()
             if name not in {"home", "mode", "main", "members"}}
    if extra:
        compact["settings"] = extra
    agents = []
    for row in payload["agents"]:
        agent = {"address": f"{row['host']}/{row.get('alias') or row['terminal_id']}",
                 "workspace": row["workspace_id"]}
        for key in ("client", "role", "turn_state", "extension_present", "stale"):
            if row.get(key) is not None:
                agent[key] = row[key]
        for key in ("phase", "summary"):
            if row.get(key):
                agent[key] = row[key]
        agents.append(agent)
    compact["agents"] = agents
    return compact


def _box_rule(line: str) -> bool:
    characters = [character for character in line if not character.isspace()]
    return len(characters) >= 3 and all(
        "\u2500" <= character <= "\u257f" for character in characters
    )


def _footer_rule(line: str) -> bool:
    """Recognize horizontal rules and input corners, never table junctions."""

    return _box_rule(line) and all(
        character.isspace() or character in "─━═╌╍┄┅┈┉╭╮╰╯┌┐└┘┏┓┗┛╔╗╚╝"
        for character in line
    )


def _content_box_borders(lines: list[str]) -> set[int]:
    """Keep framed tool/table borders from being mistaken for input chrome."""

    borders: set[int] = set()
    start: int | None = None
    divided = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if _box_rule(line) and stripped.startswith(("╭", "┌", "┏", "╔")):
            start = index
            divided = False
        elif start is not None:
            if stripped.startswith(("├", "┣", "╠")):
                divided = True
            elif _box_rule(line) and stripped.startswith(("╰", "└", "┗", "╚")):
                if divided:
                    borders.update((start, index))
                start = None
    return borders


_PRIVATE_USE_STATUS = re.compile(
    r"[\ue000-\uf8ff\U000f0000-\U000ffffd]\s+"
    r"(?:Working|Thinking|Running|Processing|Waiting|Loading|Generating|Reasoning)"
    r"(?:(?:…|\.{3})(?:\s.*)?)?",
    re.IGNORECASE,
)


def clean_agent_text(text: str, *, prompt: str | None = None) -> str:
    """Remove terminal chrome and the last matching prompt echo.

    ASCII rules such as Markdown's ``---`` are reply content, not chrome.
    Horizontal rules and input-box corners identify a trailing footer block;
    table junctions and framed tool-card borders do not.
    Private-use spinner status lines are discarded without treating ordinary
    answer text containing those glyphs or status words as chrome.
    Framed lines have terminal-width padding and long strokes compacted outside
    fenced or indented code; ordinary reply text is otherwise unchanged.
    Reply indentation is preserved; outer whitespace and blank runs are trimmed.
    """

    lines = text.splitlines()
    content_borders = _content_box_borders(lines)
    trailing_box = next(
        (
            index
            for index in range(max(0, len(lines) - 8), len(lines))
            if _footer_rule(lines[index]) and index not in content_borders
        ),
        None,
    )
    if trailing_box is not None:
        start = trailing_box
        previous = start - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous >= 0 and re.fullmatch(
            r"Working(?:…|\.{3})?", lines[previous].strip(), re.IGNORECASE
        ):
            start = previous
        lines = lines[:start]

    if prompt:
        prompt_lines = prompt.splitlines()
        anchor = prompt_lines[0].strip()
        echoed = [
            index
            for index, line in enumerate(lines)
            if anchor and line.strip() == anchor
        ]
        if echoed:
            start = echoed[-1]
            end = start + 1
            for expected in prompt_lines[1:]:
                if not expected.strip():
                    continue
                following = end
                while following < len(lines) and not lines[following].strip():
                    following += 1
                if following == len(lines) or lines[following].strip() != expected.strip():
                    break
                end = following + 1
            del lines[start:end]

    cleaned: list[str] = []
    code_fence: str | None = None
    for line in lines:
        stripped = line.strip()
        fence = re.match(r"(`{3,}|~{3,})", stripped)
        if fence:
            if code_fence is None:
                code_fence = fence[1]
            elif stripped.startswith(code_fence):
                code_fence = None
        if _box_rule(line) or _PRIVATE_USE_STATUS.fullmatch(stripped) or (
            stripped
            and "\u2800" <= stripped[0] <= "\u28ff"
            and (len(stripped) == 1 or stripped[1].isspace())
        ):
            continue
        if (
            stripped
            and code_fence is None
            and not line.startswith(("    ", "\t"))
            and "\u2500" <= stripped[0] <= "\u257f"
            and "\u2500" <= stripped[-1] <= "\u257f"
        ):
            if stripped.startswith("│") and stripped.endswith("│"):
                line = re.sub(r" +│(?=\s*$)", " │", line)
            line = re.sub(r"([─━═])\1{3,}", r"\1\1\1", line)
        if stripped:
            cleaned.append(line)
        elif cleaned and cleaned[-1]:
            cleaned.append("")
    return "\n".join(cleaned).strip()


def _payload_address(payload: Mapping[str, Any]) -> str:
    address = payload.get("address")
    if address:
        return str(address)
    name = payload.get("name") or payload.get("label")
    if not name:
        return ""
    host = "local" if payload.get("local") else payload.get("host") or "local"
    return f"{host}/{name}"


def _request_address(record: Mapping[str, Any]) -> str:
    recipient = record.get("recipient")
    if isinstance(recipient, Mapping):
        address = _payload_address(recipient)
        if address:
            return address
    context = record.get("_context")
    return _payload_address(context) if isinstance(context, Mapping) else ""


def compact_request_payload(
    record: Mapping[str, Any], *, response_text: str | None
) -> dict[str, Any]:
    """Return the request decision fields without transport or sender metadata."""

    payload = {
        key: record.get(key)
        for key in ("request_id", "state", "terminal", "submitted")
    }
    for key in ("error_code", "error"):
        if record.get(key) is not None:
            payload[key] = record[key]
    payload["phase"] = record.get("phase")
    payload["address"] = _request_address(record)
    defer = record.get("defer")
    if record.get("state") in {"deferred", "awaiting_decision"} and isinstance(
        defer, Mapping
    ):
        payload["defer"] = {
            key: defer[key]
            for key in ("reason", "deadline")
            if defer.get(key) is not None
        }
    if record.get("changed") is not None:
        payload["changed"] = record["changed"]
    response = record.get("response")
    response = response if isinstance(response, Mapping) else {}
    if response_text is None:
        response_text = clean_agent_text(
            str(response.get("output") or ""), prompt=record.get("_prompt")
        )
    if response_text or record.get("state") == "submitted_settled":
        payload["response"] = {
            "text": response_text,
            "truncated": bool(response.get("truncated")),
            "correlated": bool(response.get("correlated")),
        }
        if response.get("anchored") is True:
            payload["response"]["anchored"] = True
    return payload


def compact_spawn_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the created agent's address and placement, plus reporting failures."""

    compact = {
        key: payload[key]
        for key in ("address", "marker", "created", "pane_id", "tab_id", "workspace_id")
        if key != "tab_id" or payload.get(key) is not None
    }
    for key in ("label_metadata_reported", "placement_label_reported"):
        if payload.get(key) is False:
            compact[key] = False
    initial = payload.get("initial_prompt")
    if isinstance(initial, Mapping):
        compact["initial_prompt"] = {
            key: initial[key]
            for key in ("operation_id", "submission_id", "acceptance", "evidence", "error_code", "error")
            if key in initial
        }
    return compact


def compact_status_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the current agent's address and status, retaining stale warnings."""

    agent = payload["agent"]
    compact = {"address": _payload_address(agent), "status": agent["status"]}
    if agent.get("stale"):
        compact["stale"] = True
    return compact


def compact_read_payload(
    payload: Mapping[str, Any], *, cleaned_output: str
) -> dict[str, Any]:
    """Keep cleaned output and the fields required for subsequent delta reads."""

    return {
        "address": payload["address"],
        "output": cleaned_output,
        "delta": payload["delta"],
        "truncated": payload["truncated"],
        "cursor": payload["cursor"],
    }


def compact_batch_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one dispatcher result without routes or correlation metadata."""

    compact = {
        "address": _payload_address(item) or _request_address(item),
        "status": item["status"],
    }
    for key in ("request_id", "state"):
        if item.get(key) is not None:
            compact[key] = item[key]
    if "error_code" in item:
        error_code, error = item.get("error_code"), item.get("error")
    else:
        error_code, error = item.get("error"), item.get("detail")
    if error_code:
        compact["error_code"] = error_code
    if error:
        compact["error"] = error
    response = item.get("response")
    if isinstance(response, Mapping):
        compact["response"] = {
            "text": clean_agent_text(
                str(response.get("text", response.get("output")) or ""),
                prompt=item.get("_prompt"),
            ),
            "truncated": bool(response.get("truncated")),
        }
    return compact


def output_stream_id(identity: str) -> str:
    """Keep raw session metadata out of portable output cursors."""

    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _urlsafe_decode(value: str) -> bytes:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise InvalidOutputCursor("The output cursor is invalid.") from None
    if not encoded or len(encoded) > MAX_CURSOR_TOKEN_BYTES:
        raise InvalidOutputCursor("The output cursor is invalid.")
    try:
        return base64.b64decode(
            encoded + (b"=" * (-len(encoded) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except binascii.Error:
        raise InvalidOutputCursor("The output cursor is invalid.") from None


def _decompress_cursor(value: bytes) -> bytes:
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(value, MAX_CURSOR_STATE_BYTES + 1)
        if len(decoded) > MAX_CURSOR_STATE_BYTES:
            raise InvalidOutputCursor("The output cursor is invalid.")
        decoded += decompressor.flush(MAX_CURSOR_STATE_BYTES + 1 - len(decoded))
    except (ValueError, zlib.error):
        raise InvalidOutputCursor("The output cursor is invalid.") from None
    if (
        len(decoded) > MAX_CURSOR_STATE_BYTES
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise InvalidOutputCursor("The output cursor is invalid.")
    return decoded


def _decode_cursor(cursor: str) -> _CursorState:
    decoded = _decompress_cursor(_urlsafe_decode(cursor))
    try:
        payload = json.loads(decoded)
        version = payload["v"]
        stream = payload["s"]
        snapshot_size = payload["n"]
        snapshot_hash = payload["h"]
        tail = base64.b64decode(payload["t"], validate=True)
        tail_hash = payload["q"]
    except (
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        RecursionError,
    ):
        raise InvalidOutputCursor("The output cursor is invalid.") from None
    hashes = (stream, snapshot_hash, tail_hash)
    if (
        not isinstance(payload, dict)
        or type(version) is not int
        or version != CURSOR_VERSION
        or not isinstance(snapshot_size, int)
        or isinstance(snapshot_size, bool)
        or snapshot_size < 0
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        )
        or len(tail) > CURSOR_TAIL_BYTES
        or len(tail) > snapshot_size
        or _hash(tail) != tail_hash
    ):
        raise InvalidOutputCursor("The output cursor is invalid.")
    return _CursorState(stream, snapshot_size, snapshot_hash, tail)


def _encode_cursor(snapshot: bytes, stream: str) -> str:
    tail = snapshot[-CURSOR_TAIL_BYTES:]
    payload = {
        "h": _hash(snapshot),
        "n": len(snapshot),
        "q": _hash(tail),
        "s": stream,
        "t": base64.b64encode(tail).decode("ascii"),
        "v": CURSOR_VERSION,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    compressed = zlib.compress(encoded, level=9)
    return base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")


def _find_exact_snapshot_end(snapshot: bytes, state: _CursorState) -> int | None:
    """Find the rightmost byte where the complete prior snapshot ends."""

    if state.snapshot_size == 0:
        return 0
    if not state.tail:
        return None
    search_from = len(snapshot)
    while search_from >= 0:
        tail_start = snapshot.rfind(state.tail, 0, search_from)
        if tail_start < 0:
            return None
        end = tail_start + len(state.tail)
        start = end - state.snapshot_size
        if start >= 0 and _hash(snapshot[start:end]) == state.snapshot_hash:
            return end
        search_from = tail_start
    return None


def _tail_overlap_delta(snapshot: bytes, state: _CursorState) -> bytes | None:
    """Use a long, exact tail overlap only when a full hash is unavailable."""

    if len(state.tail) < MIN_PARTIAL_OVERLAP_BYTES:
        return None

    tail_start = snapshot.rfind(state.tail)
    if tail_start >= 0:
        end = tail_start + len(state.tail)
        # An unchanged tail at EOF with a changed full hash is evidence of a
        # rewrite earlier in the screen, not evidence of an empty delta.
        return snapshot[end:] or None

    maximum = min(len(state.tail), len(snapshot))
    for overlap_size in range(maximum, MIN_PARTIAL_OVERLAP_BYTES - 1, -1):
        if state.tail[-overlap_size:] == snapshot[:overlap_size]:
            delta = snapshot[overlap_size:]
            return delta or None
    return None


def _utf8_suffix(value: bytes, max_bytes: int) -> tuple[str, bool]:
    if len(value) <= max_bytes:
        return value.decode("utf-8"), False
    suffix = value[-max_bytes:]
    while suffix:
        try:
            return suffix.decode("utf-8"), True
        except UnicodeDecodeError as error:
            if error.start != 0:
                raise
            suffix = suffix[error.end:]
    return "", True


def compact_output(
    snapshot: str,
    *,
    stream: str,
    max_bytes: int,
    cursor: str | None = None,
) -> OutputDelta:
    """Return a bounded full snapshot or a conservative delta.

    A complete prior snapshot is recognized by its hash even if a rolling
    terminal window dropped older bytes. If that is impossible, a long exact
    tail overlap is accepted. Rewrites and lost overlap safely fall back to the
    current snapshot and mark the cursor expired.
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    current = snapshot.encode("utf-8")
    next_cursor = _encode_cursor(current, stream)

    if cursor is None:
        candidate = current
        cursor_status = "initial"
        is_delta = False
    else:
        state = _decode_cursor(cursor)
        if state.stream != stream:
            candidate = current
            cursor_status = "expired"
            is_delta = False
        elif (
            state.snapshot_size == len(current)
            and state.snapshot_hash == _hash(current)
        ):
            candidate = b""
            cursor_status = "current"
            is_delta = True
        else:
            exact_end = _find_exact_snapshot_end(current, state)
            if exact_end is not None:
                candidate = current[exact_end:]
                cursor_status = "current"
                is_delta = True
            else:
                overlap_delta = _tail_overlap_delta(current, state)
                if overlap_delta is not None:
                    candidate = overlap_delta
                    cursor_status = "current"
                    is_delta = True
                else:
                    candidate = current
                    cursor_status = "expired"
                    is_delta = False

    output, truncated = _utf8_suffix(candidate, max_bytes)
    return OutputDelta(
        output=output,
        truncated=truncated,
        cursor=next_cursor,
        cursor_status=cursor_status,
        delta=is_delta,
    )
