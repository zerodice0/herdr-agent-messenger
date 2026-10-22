import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import base64
import json
import unittest
import zlib

from ham.agents import output as agent_output


# Captured from the token audit's real omp request and recent-unwrapped read.
OMP_PROMPT = "Message from winmini/purple-horse:\n\nNo tools. Reply exactly: OK"
OMP_REPLY = ' The user is simply asking me to reply with "OK" exactly, no tools needed.'
OMP_RESPONSE = "\n".join(
    [
        " Message from winmini/purple-horse:",
        "",
        " No tools. Reply exactly: OK",
        "",
        "",
        OMP_REPLY,
        "",
        " OK",
        "",
        "─" * 322,
        "",
        "─" * 322,
        " 󰵗 · 󰪣 Haiku 5.5 ·  ham-token-audit ·  25.4%/100K 󰁨 · 󰙺 0.00",
    ]
)
OMP_READ_PREFIX = "\n".join(
    [
        "",
        " " * 145 + "████████████",
        " " * 148 + "██  ██       ▄▀▀▄ █▀▄▀▄ █▀▀▄",
        " " * 148 + "██  ██       ▀▄▄▀ █ █ █ █▄▄▀",
        " " * 148 + "▒▒  ██                  █",
        " " * 152 + "██       v18.11.1",
        "",
        " " * 127 + "Tip: `/force read` pins the next turn to one specific tool when the",
        " " * 142 + "model keeps reaching for the wrong one",
        "",
        " Model scope: claude-3-5-sonnet-20240620, claude-3-5-sonnet-20241022, "
        "claude-3-haiku-20240307, claude-fable-5, claude-fable-5-1, claude-haiku-4-5, "
        "claude-haiku-4-5-20251001, claude-haiku-5-5, claude-mythos-5, "
        "claude-mythos-5-1, claude-opus-4-0, claude-opus-4-1, "
        "claude-opus-4-1-20250805, claude-opus-4-20250514,",
        " claude-opus-4-5, claude-opus-4-5-20251101, claude-opus-4-6, "
        "claude-opus-4-7, claude-opus-4-8, claude-opus-5, claude-opus-5-5, "
        "claude-sonnet-4-0, claude-sonnet-4-20250514, claude-sonnet-4-5, "
        "claude-sonnet-4-5-20250929, claude-sonnet-4-6, claude-sonnet-5, "
        "claude-sonnet-5-5, gpt-5.6-luna, gpt-5.6-sol, gpt-5.6-terra,",
        " gpt-6-astra, gpt-6-luna, gpt-6-sol, gpt-6.1-sol (Ctrl+P to cycle)",
        "",
        "",
    ]
)

# Sanitized stored response from the live compact-output pwd smoke: labels,
# paths and card width changed, but line order and tool/footer framing retained.
OMP_TOOL_PROMPT = "Message from local/sender:\n\npwd를 실행해서 현재 작업 디렉터리를 알려줘."
OMP_TOOL_RESPONSE = "\n".join(
    [
        " Message from local/sender:",
        "",
        " pwd를 실행해서 현재 작업 디렉터리를 알려줘.",
        "",
        "",
        " I'll just run pwd directly and report back the current directory from its output.",
        "",
        "╭────────────────────────────────────────────╮",
        "│ $ pwd                                      │",
        "├─── Output ─────────────────────────────────┤",
        "│ /tmp/ham-work                              │",
        "│ ⟨Wall: 0.03s | Timeout: 300s⟩                │",
        "╰────────────────────────────────────────────╯",
        "",
        " pwd 실행 결과: /tmp/ham-work",
        "",
        "─" * 46,
        "",
        "─" * 46,
        " 󰵗 · 󰪣 Haiku 5.5 ·  ham-work ·  25.8%/100K 󰁨 · 󰙺 0.00",
    ]
)


class LedgerProjectionTest(unittest.TestCase):
    def test_board_keeps_selection_and_stale_resource_findings_without_full_rows(self):
        row = {"host": "peer", "alias": "", "terminal_id": "term", "workspace_id": "w1",
               "repo_id": "repo", "turn_state": "unknown", "role": None, "unread": "?",
               "phase": "review", "summary": "Saved status", "stale": True,
               "extension_present": 0, "devices": [{"device_id": "phone"}],
               "servers": [{"port": 4000}], "agent_id": "private", "generation": 9}
        error = {"code": "transport_unavailable", "message": "Offline"}
        payload = {"repo_id": None, "agents": [row], "hosts": [
            {"host": "peer", "uuid": "private", "agents": [row], "stale": True,
             "unread": "?", "error": error, "resources": {
                 "servers_unowned": [{"port": 5000}], "dead_serve": [{"port": 6000}],
                 "stale_devices": [{"device_id": "old"}], "error": "Scan unavailable"}}]}
        compact = agent_output.compact_board_payload(payload)
        agent = compact["agents"][0]
        self.assertEqual((agent["address"], agent["workspace"], agent["unread"], agent["stale"]),
                         ("peer/term", "w1", "?", True))
        self.assertEqual((agent["devices"], agent["servers"], agent["extension_present"]), (1, 1, 0))
        self.assertTrue({"agent_id", "generation", "repo_id"}.isdisjoint(agent))
        self.assertEqual(agent["repo"], "repo")
        host = compact["hosts"][0]
        self.assertNotIn("agents", host)
        self.assertEqual(host["error"], error)
        self.assertEqual(host["resources"], {"servers_unowned": 1, "dead_serve": 1,
                                             "stale_devices": 1, "error": "Scan unavailable"})
        self.assertIs(payload["hosts"][0]["agents"][0], row)
        self.assertEqual(row["devices"], [{"device_id": "phone"}])

    def test_board_and_status_list_show_short_repo_for_every_agent_including_one_repo(self):
        rows = [{"host": "alpha", "alias": "owl", "terminal_id": "one", "workspace_id": "w1",
                 "repo_id": "git:example.test/team/monitor_mobile"},
                {"host": "beta", "alias": "deer", "terminal_id": "two", "workspace_id": "w2",
                 "repo_id": "git:example.test/services/dxp-api"}]
        for count in (1, 2):
            with self.subTest(count=count):
                selected = rows[:count]
                payload = {"repo_id": None, "agents": selected}
                compact = agent_output.compact_board_payload(payload)
                self.assertEqual([row["repo"] for row in compact["agents"]],
                                 ["monitor_mobile", "dxp-api"][:count])
                self.assertEqual([row.get("repo_id") for row in compact["agents"]],
                                 [row["repo_id"] if count > 1 else None for row in selected])
                self.assertTrue(all("repo" not in row for row in selected))

    def test_board_omits_workspace_metadata_without_touching_the_source_payload(self):
        workspaces = [{"host": "peer", "workspace_id": "w1", "label": "Workspace"}]
        payload = {"repo_id": None, "agents": [], "hosts": [], "workspaces": workspaces}
        compact = agent_output.compact_board_payload(payload)
        self.assertEqual(set(compact), {"repo_id", "agents", "hosts"})
        self.assertIs(payload["workspaces"], workspaces)

    def test_status_set_reports_display_only_when_it_failed(self):
        base = {"agent_id": "agent", "phase": "review", "summary": "Working"}
        self.assertEqual(agent_output.compact_status_set_payload(
            {**base, "display": {"updated": ["p1"], "failed": []}}), base)
        self.assertEqual(agent_output.compact_status_set_payload(
            {**base, "display": {"updated": [], "failed": ["p1"]}, "warnings": [{"code": "w"}]}),
            {**base, "display": {"failed": ["p1"]}, "warnings": [{"code": "w"}]})

    def test_receipts_keep_identity_state_and_actionable_advisories(self):
        receipt = {"message_id": "m", "thread_id": "t", "origin_host": "alpha", "payload_hash": "h",
                   "recipient_address": "alpha/owl", "recipient_host": "alpha",
                   "recipient_terminal_id": "term", "state": "queued", "hold_reason": None,
                   "via": None, "delivered_at": None, "updated_at": 1.0, "withdraw_requested": 0,
                   "authoritative": 1, "evidence": None, "fyi_turn_end": False}
        self.assertEqual(agent_output.compact_send_payload(receipt),
                         {"message_id": "m", "thread_id": "t", "recipient_address": "alpha/owl",
                          "state": "queued"})
        advised = {**receipt, "state": "pending", "hold_reason": "held", "fyi_turn_end": True,
                   "fyi_wake_after_seconds": 600, "warnings": [{"code": "recipient_no_receiver"}]}
        self.assertEqual(agent_output.compact_send_payload(advised), {
            "message_id": "m", "thread_id": "t", "recipient_address": "alpha/owl", "state": "pending",
            "hold_reason": "held", "fyi_turn_end": True, "fyi_wake_after_seconds": 600,
            "warnings": [{"code": "recipient_no_receiver"}]})
        self.assertEqual(agent_output.compact_handle_payload(
            {"receipts": [receipt, {**receipt, "message_id": "n", "state": "handled"}]}),
            {"receipts": [{"message_id": "m", "state": "queued"}, {"message_id": "n", "state": "handled"}]})

    def test_dismissals_keep_only_documented_audit_fields(self):
        audit = {"message_id": "m", "actor_alias": "owl", "actor_terminal_id": "term",
                 "reason": "Closed", "dismissed_at": 1.5, "actor_agent_id": "private",
                 "failure": {"state": "undeliverable"}}
        expected = {key: audit[key] for key in agent_output.DISMISSAL_FIELDS}
        self.assertEqual(agent_output.compact_dismiss_payload(
            {"dismissals": [audit], "warnings": [{"code": "w"}]}),
            {"dismissals": [expected], "warnings": [{"code": "w"}]})
        row = {"message_id": "m", "origin_host": "alpha", "sender_label": "owl",
               "state": "undeliverable", "dismissal": audit}
        self.assertEqual(agent_output.compact_inbox_payload({"messages": [row]})["messages"][0]["dismissal"],
                         expected)

    def test_repo_show_keeps_participation_and_agent_addresses_without_setting_stamps(self):
        stamp = {"repo_id": "repo", "field": "thresholds", "value": {"idle_minutes": 3},
                 "version": 2, "origin_host": "alpha", "changed_by": "user", "changed_at": 1.0}
        payload = {"repo_id": "repo", "host": "alpha", "workspace_id": "w1", "requested_workspace_id": "w2",
                   "home_host": "alpha", "mode": "auto", "settings": {
                       "home": {**stamp, "field": "home", "value": "alpha"},
                       "mode": {**stamp, "field": "mode", "value": "auto"}, "thresholds": stamp},
                   "main": {"host": "alpha", "terminal_id": "t1"}, "main_source": "auto", "members": [],
                   "computed_main": None, "conflicts": [], "propagation_pending": 0, "agents": [
                       {"agent_id": "private", "host": "alpha", "terminal_id": "t1", "workspace_id": "w1",
                        "alias": "owl", "client": "omp", "role": "main", "turn_state": "idle",
                        "extension_present": 1, "phase": "", "summary": "Reviewing", "generation": 4}]}
        self.assertEqual(agent_output.compact_repo_payload(payload), {
            "repo_id": "repo", "host": "alpha", "workspace_id": "w1", "mode": "auto",
            "requested_workspace_id": "w2", "home_host": "alpha",
            "main": {"host": "alpha", "terminal_id": "t1"}, "main_source": "auto",
            "settings": {"thresholds": {"idle_minutes": 3}},
            "agents": [{"address": "alpha/owl", "workspace": "w1", "client": "omp", "role": "main",
                        "turn_state": "idle", "extension_present": 1, "summary": "Reviewing"}]})

    def test_inbox_keeps_priority_reply_and_truncation_without_delivery_internals(self):
        row = {"message_id": "message", "thread_id": "thread", "reply_to": "parent", "hop": 2,
               "origin_host": "peer", "sender_label": "main", "sender": "private",
               "kind": "action", "priority": "high", "body": "Partial", "state": "delivering",
               "truncated": True, "body_omitted": False, "payload_hash": "hash",
               "claim_id": "claim", "delivered_at": None, "hold_reason": "recipient_changed",
               "withdraw_requested": 1}
        payload = {"terminal_id": "term", "messages": [row],
                   "warnings": [{"code": "diagnostic_warning"}]}
        compact = agent_output.compact_inbox_payload(payload)
        message = compact["messages"][0]
        self.assertEqual(message["sender"], "peer/main")
        for key in ("priority", "reply_to", "hop", "truncated", "body_omitted",
                    "hold_reason", "withdraw_requested"):
            self.assertEqual(message[key], row[key])
        self.assertTrue({"payload_hash", "claim_id", "delivered_at"}.isdisjoint(message))
        self.assertNotIn("terminal_id", compact)
        self.assertEqual(compact["warnings"], payload["warnings"])
        self.assertEqual(row["sender"], "private")

    def test_inbox_keeps_dismissal_audit_separate_from_original_failure(self):
        audit = {"message_id": "message", "actor_alias": "sender", "actor_terminal_id": "term",
                 "reason": "Reviewed closed session", "dismissed_at": 1_800_000_000}
        row = {"message_id": "message", "origin_host": "local", "sender_label": "sender",
               "state": "undeliverable", "hold_reason": "terminal_closed", "body": "Request"}
        for dismissal in (None, audit):
            with self.subTest(dismissal=dismissal):
                compact = agent_output.compact_inbox_payload(
                    {"messages": [{**row, "dismissal": dismissal}]})["messages"][0]
                self.assertEqual(compact.get("dismissal"), dismissal)
                self.assertEqual(compact["state"], "undeliverable")
                self.assertEqual(compact["hold_reason"], "terminal_closed")


class AgentTextTest(unittest.TestCase):
    def test_real_omp_request_keeps_reply_without_echo_input_box_or_footer(self):
        self.assertEqual(
            agent_output.clean_agent_text(OMP_RESPONSE, prompt=OMP_PROMPT),
            OMP_REPLY.strip() + "\n\n OK",
        )

    def test_real_omp_read_keeps_startup_content_before_last_prompt_echo(self):
        self.assertEqual(
            agent_output.clean_agent_text(
                OMP_READ_PREFIX + "\n" + OMP_RESPONSE, prompt=OMP_PROMPT
            ),
            OMP_READ_PREFIX.strip() + "\n\n" + OMP_REPLY + "\n\n OK",
        )

    def test_read_without_prompt_does_not_guess_which_lines_are_echoes(self):
        self.assertEqual(
            agent_output.clean_agent_text(OMP_RESPONSE),
            "Message from winmini/purple-horse:\n\n No tools. Reply exactly: OK"
            "\n\n" + OMP_REPLY + "\n\n OK",
        )

    def test_multiline_echo_skips_blanks_and_removes_only_last_occurrence(self):
        prompt = "Message from local/sender:\n\nFirst task\nSecond task\n\nThird task"
        earlier = prompt + "\nEarlier reply\n"
        echoed = (
            " Message from local/sender: \n\n First task \n\nSecond task\n"
            "\n\n Third task\n\nNew reply"
        )
        self.assertEqual(
            agent_output.clean_agent_text(earlier + echoed, prompt=prompt),
            prompt + "\nEarlier reply\n\nNew reply",
        )

    def test_echo_matching_stops_before_nonmatching_reply(self):
        prompt = "Message from local/sender:\nfirst\nsecond"
        self.assertEqual(
            agent_output.clean_agent_text(
                "Message from local/sender:\nfirst\nThe reply\nsecond", prompt=prompt
            ),
            "The reply\nsecond",
        )

    def test_absent_anchor_keeps_lines_that_happen_to_match_prompt(self):
        self.assertEqual(
            agent_output.clean_agent_text("first\nsecond", prompt="missing\nfirst\nsecond"),
            "first\nsecond",
        )

    def test_empty_and_plain_text_only_trim_and_collapse_blank_runs(self):
        for text in ("", "\n \n\t\n"):
            with self.subTest(text=text):
                self.assertEqual(agent_output.clean_agent_text(text), "")
        self.assertEqual(
            agent_output.clean_agent_text("\nHello\n\n \n\n    indented code\n\n"),
            "Hello\n\n    indented code",
        )

    def test_markdown_tables_ascii_rules_and_box_characters_in_text_are_kept(self):
        reply = (
            "Results\n\n| key | value |\n| --- | --- |\n| count | 3 |\n"
            "\n---\n\n```text\n│ > text │\nname ─ value\n╭─ label ─╮\n```"
        )
        self.assertEqual(agent_output.clean_agent_text(reply), reply)

    def test_claude_codex_box_borders_do_not_remove_boxed_text_before_reply(self):
        rest = "\n".join(f"Reply line {index}" for index in range(8))
        text = "╭────────╮\n│ > text │\n╰────────╯\n" + rest
        self.assertEqual(agent_output.clean_agent_text(text), "│ > text │\n" + rest)

    def test_only_unicode_box_rules_of_at_least_three_characters_are_dropped(self):
        rest = "\n".join(f"Reply line {index}" for index in range(8))
        text = "─\n──\n───\n╭─ ─╮\n" + rest
        self.assertEqual(agent_output.clean_agent_text(text), "─\n──\n" + rest)

    def test_trailing_block_starts_at_earliest_border_in_last_eight_lines(self):
        self.assertEqual(
            agent_output.clean_agent_text(
                "Reply\n───\ninput\n\n╰──╯\nmodel\ncwd\ncontext\nstatus"
            ),
            "Reply",
        )

    def test_table_junctions_never_start_the_trailing_footer(self):
        for separator in ("├──┼──┤", "┬──┴──┬", "┣━━╋━━┫", "╠══╬══╣"):
            with self.subTest(separator=separator):
                self.assertEqual(
                    agent_output.clean_agent_text(
                        "Results\n│ api │ PASS │\n" + separator
                        + "\n│ ui │ FAIL │\n└────┴──────┘\n\n"
                        + "───\n\n───\nmodel status"
                    ),
                    "Results\n│ api │ PASS │\n│ ui │ FAIL │",
                )

    def test_real_omp_tool_card_keeps_final_answer_after_closing_border(self):
        self.assertEqual(
            agent_output.clean_agent_text(OMP_TOOL_RESPONSE, prompt=OMP_TOOL_PROMPT),
            "I'll just run pwd directly and report back the current directory from its output."
            "\n\n│ $ pwd │"
            "\n├─── Output ───┤"
            "\n│ /tmp/ham-work │"
            "\n│ ⟨Wall: 0.03s | Timeout: 300s⟩ │"
            "\n\n pwd 실행 결과: /tmp/ham-work",
        )

    def test_short_tool_card_top_corner_does_not_start_footer_either(self):
        self.assertEqual(
            agent_output.clean_agent_text(
                "╭────────────╮\n│ $ pwd      │\n├── Output ──┤\n│ /tmp/work  │"
                "\n╰────────────╯\n\nAnswer: /tmp/work"
            ),
            "│ $ pwd │\n├── Output ──┤\n│ /tmp/work │\n\nAnswer: /tmp/work",
        )

    def test_only_framed_lines_have_padding_and_long_strokes_compacted(self):
        reply = (
            "  │ $ pwd                  │\n"
            "├────── Output ━━━━━━ ══════┤\n"
            "Plain ────── text\n"
            "| Markdown ━━━━━━ | column    |\n"
            "    │ indented code         │\n"
            "```text\n│ fenced code          │\n├────── code ──────┤\n```\n"
            "~~~text\n│ more code            │\n~~~\n"
            "│ final framed line        │"
        )
        self.assertEqual(
            agent_output.clean_agent_text(reply),
            "│ $ pwd │\n"
            "├─── Output ━━━ ═══┤\n"
            "Plain ────── text\n"
            "| Markdown ━━━━━━ | column    |\n"
            "    │ indented code         │\n"
            "```text\n│ fenced code          │\n├────── code ──────┤\n```\n"
            "~~~text\n│ more code            │\n~~~\n"
            "│ final framed line │",
        )

    def test_border_outside_last_eight_lines_does_not_start_trailing_block(self):
        footer = "\n".join(f"Reply line {index}" for index in range(8))
        self.assertEqual(
            agent_output.clean_agent_text("Reply\n───\n" + footer),
            "Reply\n" + footer,
        )

    def test_spinner_and_working_line_above_input_box_are_removed(self):
        self.assertEqual(
            agent_output.clean_agent_text(
                "Reply\n⠋ Thinking about a reply\nWorking…\n\n╭────╮\n╰────╯\nfooter"
            ),
            "Reply",
        )

    def test_braille_reply_text_and_unanchored_footer_glyphs_are_kept(self):
        reply = "Braille example\n⠓⠑⠇⠇⠕\nModel information:  context"
        self.assertEqual(agent_output.clean_agent_text(reply), reply)

    def test_working_word_in_reply_is_not_removed_without_input_box(self):
        self.assertEqual(
            agent_output.clean_agent_text("Working…\nThe explanation"),
            "Working…\nThe explanation",
        )

    def test_private_use_spinners_and_status_words_are_removed(self):
        for glyph in ("\ue000", "\uf8ff", "\U000f0000", "\U000f12b7", "\U000ffffd"):
            for status in ("Working…", "Thinking...", "Processing", "Running… (esc to cancel)"):
                with self.subTest(glyph=ascii(glyph), status=status):
                    self.assertEqual(
                        agent_output.clean_agent_text(f"{glyph} {status}\n\nActual answer"),
                        "Actual answer",
                    )
                    self.assertEqual(agent_output.clean_agent_text(f"{glyph} {status}"), "")

    def test_private_use_glyphs_in_real_answer_text_are_preserved(self):
        reply = (
            "The spinner was \U000f12b7 Working… before completion.\n"
            "\U000f12b7 Working with tables is supported.\n"
            "\ue000 A useful answer\nWorking…\nAnswer: ready"
        )
        self.assertEqual(agent_output.clean_agent_text(reply), reply)


class CompactPayloadTest(unittest.TestCase):
    def request_record(self, **overrides):
        record = {
            "request_id": "a" * 32,
            "state": "submitted_settled",
            "terminal": True,
            "submitted": True,
            "error_code": None,
            "error": None,
            "phase": "settled",
            "recipient": {"host": "desktop", "name": "blue-heron", "route": "opaque"},
            "response": {
                "output": OMP_RESPONSE,
                "truncated": True,
                "correlated": True,
                "anchored": True,
                "error": None,
            },
            "sender": {"host": "winmini", "name": "purple-horse"},
            "route": "opaque",
            "_prompt": OMP_PROMPT,
            "_baseline": {"text": "old output"},
            "_context": {"host": "wrong", "label": "wrong"},
        }
        record.update(overrides)
        return record

    def test_request_compact_order_and_response_metadata(self):
        record = self.request_record(error_code="read_failed", error="Read failed", changed=True)
        before = json.dumps(record)
        payload = agent_output.compact_request_payload(record, response_text="OK")
        self.assertEqual(
            list(payload),
            [
                "request_id", "state", "terminal", "submitted", "error_code", "error",
                "phase", "address", "changed", "response",
            ],
        )
        self.assertEqual(
            payload,
            {
                "request_id": "a" * 32,
                "state": "submitted_settled",
                "terminal": True,
                "submitted": True,
                "error_code": "read_failed",
                "error": "Read failed",
                "phase": "settled",
                "address": "desktop/blue-heron",
                "changed": True,
                "response": {
                    "text": "OK", "truncated": True, "correlated": True, "anchored": True,
                },
            },
        )
        self.assertEqual(list(payload["response"]), ["text", "truncated", "correlated", "anchored"])
        self.assertEqual(json.dumps(record), before)

    def test_request_none_response_text_cleans_stored_output_with_saved_prompt(self):
        payload = agent_output.compact_request_payload(self.request_record(), response_text=None)
        self.assertEqual(payload["response"]["text"], OMP_REPLY.strip() + "\n\n OK")

    def test_request_unknown_recipient_uses_context_address_and_omits_empty_response(self):
        record = self.request_record(
            recipient=None,
            _context={"host": "remote", "label": "missing", "route": None},
            state="submission_failed",
            phase="resolve_failed",
            submitted=False,
            error_code="agent_not_found",
            error="No such agent",
        )
        payload = agent_output.compact_request_payload(record, response_text="")
        self.assertEqual(payload["address"], "remote/missing")
        self.assertNotIn("response", payload)

    def test_request_address_prefers_recipient_address_and_normalizes_local_records(self):
        for recipient, expected in (
            ({"address": "local/blue", "host": "winmini", "name": "other"}, "local/blue"),
            ({"local": True, "host": "winmini", "name": "blue"}, "local/blue"),
            ({"host": "remote", "label": "blue"}, "remote/blue"),
        ):
            with self.subTest(recipient=recipient):
                payload = agent_output.compact_request_payload(
                    self.request_record(recipient=recipient), response_text=""
                )
                self.assertEqual(payload["address"], expected)

    def test_request_deferred_metadata_is_small_and_before_changed_and_response(self):
        for state in ("deferred", "awaiting_decision"):
            with self.subTest(state=state):
                payload = agent_output.compact_request_payload(
                    self.request_record(
                        state=state,
                        terminal=False,
                        submitted=False,
                        phase=state,
                        defer={
                            "reason": "recipient_focused",
                            "since": "2026-10-16T00:00:00Z",
                            "deadline": "2026-10-16T00:03:00Z",
                            "notified": {"local": "shown"},
                        },
                        changed=False,
                    ),
                    response_text="partial reply",
                )
                self.assertEqual(
                    list(payload),
                    [
                        "request_id", "state", "terminal", "submitted", "phase", "address",
                        "defer", "changed", "response",
                    ],
                )
                self.assertEqual(
                    payload["defer"],
                    {"reason": "recipient_focused", "deadline": "2026-10-16T00:03:00Z"},
                )
                self.assertEqual(list(payload["defer"]), ["reason", "deadline"])
                self.assertIs(payload["submitted"], False)
                self.assertIs(payload["changed"], False)

    def test_request_omits_stale_defer_and_null_optionals_but_keeps_empty_settled_response(self):
        payload = agent_output.compact_request_payload(
            self.request_record(
                changed=None,
                defer={"reason": "recipient_focused", "deadline": "old"},
                response={"output": "", "anchored": False},
            ),
            response_text="",
        )
        self.assertEqual(
            list(payload),
            ["request_id", "state", "terminal", "submitted", "phase", "address", "response"],
        )
        self.assertEqual(
            payload["response"], {"text": "", "truncated": False, "correlated": False}
        )

    def test_request_empty_response_omitted_for_unsettled_states(self):
        for state in ("submitted_working", "submitted_unknown", "deferred", "awaiting_decision"):
            with self.subTest(state=state):
                payload = agent_output.compact_request_payload(
                    self.request_record(state=state, terminal=False, submitted=None),
                    response_text="",
                )
                self.assertNotIn("response", payload)
                self.assertIsNone(payload["submitted"])

    def test_spawn_compact_order_and_only_false_reporting_flags(self):
        payload = {
            "address": "local/blue",
            "marker": "blue marker",
            "created": "tab",
            "pane_id": "w1:p2",
            "tab_id": "w1:t2",
            "workspace_id": "w1",
            "route": "opaque",
            "agent": {"status": "idle"},
            "kind": "omp",
            "cwd": "/tmp",
            "label_metadata_reported": False,
            "placement_label_reported": True,
        }
        compact = agent_output.compact_spawn_payload(payload)
        self.assertEqual(
            list(compact),
            [
                "address", "marker", "created", "pane_id", "tab_id", "workspace_id",
                "label_metadata_reported",
            ],
        )
        self.assertIs(compact["label_metadata_reported"], False)
        del payload["tab_id"]
        payload["label_metadata_reported"] = True
        payload["placement_label_reported"] = False
        self.assertEqual(
            list(agent_output.compact_spawn_payload(payload)),
            [
                "address", "marker", "created", "pane_id", "workspace_id",
                "placement_label_reported",
            ],
        )

    def test_spawn_initial_prompt_keeps_acceptance_and_location_without_private_body(self):
        for acceptance in ("accepted", "unknown", "failed"):
            with self.subTest(acceptance=acceptance):
                initial = {
                    "operation_id": "operation", "submission_id": "submission",
                    "acceptance": acceptance, "evidence": "transport observation",
                    "error_code": "prompt_failed" if acceptance == "failed" else None,
                    "body": "Private initial assignment", "body_hash": "private-hash",
                    "sender": {"identity": "private-sender"},
                }
                original = json.dumps(initial)
                payload = {
                    "address": "remote/blue", "marker": "", "created": "tab",
                    "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
                    "initial_prompt": initial,
                }
                compact = agent_output.compact_spawn_payload(payload)
                self.assertEqual(compact["pane_id"], "w1:p2")
                self.assertEqual(compact["tab_id"], "w1:t2")
                self.assertEqual(compact["workspace_id"], "w1")
                self.assertEqual(compact["address"], "remote/blue")
                self.assertEqual(compact["initial_prompt"], {
                    key: initial[key] for key in
                    ("operation_id", "submission_id", "acceptance", "evidence", "error_code")
                })
                self.assertNotIn("Private initial assignment", json.dumps(compact))
                self.assertEqual(json.dumps(initial), original)

    def test_status_only_keeps_address_status_and_true_stale_warning(self):
        payload = {
            "agent": {"address": "remote/blue", "status": "working", "stale": False},
            "route": "opaque",
            "route_refreshed": True,
        }
        self.assertEqual(
            agent_output.compact_status_payload(payload),
            {"address": "remote/blue", "status": "working"},
        )
        payload["agent"]["stale"] = True
        compact = agent_output.compact_status_payload(payload)
        self.assertEqual(list(compact), ["address", "status", "stale"])
        self.assertIs(compact["stale"], True)

    def test_read_keeps_cleaned_output_and_delta_cursor_in_contract_order(self):
        payload = {
            "address": "local/blue",
            "output": OMP_RESPONSE,
            "delta": False,
            "truncated": True,
            "cursor": "next-cursor",
            "cursor_status": "expired",
            "route": "opaque",
            "route_refreshed": True,
        }
        compact = agent_output.compact_read_payload(payload, cleaned_output="OK")
        self.assertEqual(
            list(compact), ["address", "output", "delta", "truncated", "cursor"]
        )
        self.assertEqual(
            compact,
            {
                "address": "local/blue", "output": "OK", "delta": False,
                "truncated": True, "cursor": "next-cursor",
            },
        )

    def test_batch_item_normalizes_dispatcher_error_detail_and_cleans_response(self):
        item = {
            "address": "remote/blue",
            "status": "failed",
            "route": "opaque",
            "request_id": "a" * 32,
            "state": "submission_failed",
            "error": "read_failed",
            "detail": "Read failed",
            "response": {"output": OMP_RESPONSE, "truncated": True, "correlated": True},
            "_prompt": OMP_PROMPT,
        }
        compact = agent_output.compact_batch_item(item)
        self.assertEqual(
            list(compact),
            ["address", "status", "request_id", "state", "error_code", "error", "response"],
        )
        self.assertEqual(compact["error_code"], "read_failed")
        self.assertEqual(compact["error"], "Read failed")
        self.assertEqual(
            compact["response"],
            {"text": OMP_REPLY.strip() + "\n\n OK", "truncated": True},
        )
        self.assertEqual(list(compact["response"]), ["text", "truncated"])

    def test_batch_item_preserves_explicit_error_fields(self):
        self.assertEqual(
            agent_output.compact_batch_item(
                {
                    "address": "local/blue",
                    "status": "failed",
                    "error_code": "delivery_cancelled",
                    "error": "Cancelled by user",
                    "detail": "unused dispatcher detail",
                }
            ),
            {
                "address": "local/blue", "status": "failed",
                "error_code": "delivery_cancelled", "error": "Cancelled by user",
            },
        )

    def test_batch_item_omits_absent_optional_fields(self):
        self.assertEqual(
            agent_output.compact_batch_item(
                {
                    "address": "local/blue", "status": "submitted",
                    "request_id": None, "state": None, "error": "", "detail": "",
                }
            ),
            {"address": "local/blue", "status": "submitted"},
        )


class AgentOutputTest(unittest.TestCase):
    def test_invalid_cursor_versions_and_deep_json_are_rejected(self):
        cursor = self.read("before").cursor
        data = zlib.decompress(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        payload = json.loads(data)
        invalid_states = [json.dumps({**payload, "v": version}) for version in (True, 1.0)]
        invalid_states.append("[" * 1100 + "]" * 1100)
        for state in invalid_states:
            with self.subTest(state=state[:60]):
                malformed = base64.urlsafe_b64encode(zlib.compress(state.encode())).decode()
                with self.assertRaises(agent_output.InvalidOutputCursor):
                    self.read("after", cursor=malformed)

    def read(
        self,
        snapshot: str,
        *,
        cursor: str | None = None,
        stream: str = "a" * 64,
        max_bytes: int = 64 * 1024,
    ) -> agent_output.OutputDelta:
        return agent_output.compact_output(
            snapshot,
            stream=stream,
            max_bytes=max_bytes,
            cursor=cursor,
        )

    def test_exact_append_returns_only_new_output(self):
        initial = self.read("first\n")
        delta = self.read("first\nsecond\n", cursor=initial.cursor)

        self.assertEqual(delta.output, "second\n")
        self.assertTrue(delta.delta)
        self.assertEqual(delta.cursor_status, "current")

    def test_unchanged_snapshot_removes_duplicate_output(self):
        initial = self.read("same output\n")
        delta = self.read("same output\n", cursor=initial.cursor)

        self.assertEqual(delta.output, "")
        self.assertTrue(delta.delta)
        self.assertFalse(delta.truncated)

    def test_rightmost_complete_snapshot_avoids_repeated_block(self):
        initial = self.read("old block\n")
        delta = self.read(
            "old block\nold block\nnew output\n",
            cursor=initial.cursor,
        )

        self.assertEqual(delta.output, "new output\n")

    def test_rolling_window_uses_partial_tail_overlap(self):
        previous = "x" * 5000
        initial = self.read(previous)
        current = previous[-1000:] + "new output\n"
        delta = self.read(current, cursor=initial.cursor)

        self.assertEqual(delta.output, "new output\n")
        self.assertTrue(delta.delta)

    def test_screen_rewrite_safely_returns_current_snapshot(self):
        initial = self.read("progress 10%\n")
        delta = self.read("progress 20%\n", cursor=initial.cursor)

        self.assertEqual(delta.output, "progress 20%\n")
        self.assertFalse(delta.delta)
        self.assertEqual(delta.cursor_status, "expired")

    def test_lost_overlap_expires_cursor_and_falls_back(self):
        initial = self.read("a" * 5000)
        delta = self.read("entirely replaced\n", cursor=initial.cursor)

        self.assertEqual(delta.output, "entirely replaced\n")
        self.assertFalse(delta.delta)
        self.assertEqual(delta.cursor_status, "expired")

    def test_cursor_for_another_occupant_expires(self):
        initial = self.read("old agent\n")
        delta = self.read(
            "new agent\n",
            cursor=initial.cursor,
            stream="b" * 64,
        )

        self.assertEqual(delta.output, "new agent\n")
        self.assertFalse(delta.delta)
        self.assertEqual(delta.cursor_status, "expired")

    def test_utf8_byte_cap_never_splits_a_character(self):
        result = self.read("prefix 가나다", max_bytes=4)

        self.assertEqual(result.output, "다")
        self.assertLessEqual(len(result.output.encode("utf-8")), 4)
        self.assertTrue(result.truncated)

    def test_cap_smaller_than_one_character_returns_empty_text(self):
        result = self.read("가", max_bytes=2)

        self.assertEqual(result.output, "")
        self.assertTrue(result.truncated)

    def test_cursor_is_deterministic_for_the_same_snapshot(self):
        first = self.read("stable\n")
        second = self.read("stable\n")

        self.assertEqual(first.cursor, second.cursor)

    def test_invalid_cursor_is_rejected(self):
        with self.assertRaises(agent_output.InvalidOutputCursor):
            self.read("output\n", cursor="not-a-cursor")


if __name__ == "__main__":
    unittest.main()
