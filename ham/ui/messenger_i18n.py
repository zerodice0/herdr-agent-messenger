"""Localized strings for the Agent Messenger popup."""

from __future__ import annotations

import locale
import os
import platform
import re
import subprocess
from typing import Mapping


SUPPORTED_LANGUAGES = ("en", "ja", "ko")

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "title": "Agent Messenger",
        "held_title": "HAM: Held Messages",
        "held_empty": "No held messages.",
        "held_warning": "Pushing combines this message with any unsent text in the recipient pane.",
        "held_help": "↑↓ Select  P Push  C Cancel  Q / Esc Close",
        "held_item": "{sender} → {recipient} [{state}]",
        "held_since": "Held since {since}; deadline {deadline}",
        "held_deferred": "Waiting for focus to leave",
        "held_awaiting_decision": "Awaiting your decision",
        "held_error": "Could not update held messages: {error}",
        "held_push_not_applied": "Not submitted ({state}).",
        "held_cancel_not_applied": "Not cancelled ({state}).",
        "send_prompt": "Send Prompt",
        "from": "From",
        "coordinator": "Coordinator",
        "recipients": "Recipients",
        "search": "Search",
        "message": "Message",
        "message_placeholder": "Type a message...",
        "selected": "selected",
        "unknown_workspace": "unknown workspace",
        "local": "LOCAL",
        "remote": "REMOTE AGENTS",
        "discover_question": "Discover agents on SSH hosts?",
        "discover_choice": "D Discover   L Local only   Esc Cancel",
        "discover_remote_option": "Discover SSH agents",
        "local_only_option": "Use local agents only",
        "mode_question": "How should this request be sent?",
        "delivery_mode": "Delivery mode",
        "delegate_option": "Delegate through coordinator (Recommended)",
        "delegate_privacy": (
            "Full request goes only to the coordinator; workers receive tailored "
            "instructions."
        ),
        "direct_option": "Send directly (Advanced)",
        "direct_privacy": (
            "Full message is copied to every selected agent, including agents on SSH "
            "hosts."
        ),
        "no_ssh_hosts": "No concrete SSH hosts were found.",
        "discovering": "Discovering remote agents",
        "discovery_cancelled": "Remote discovery cancelled.",
        "discovery_complete": "Remote discovery complete.",
        "hosts_skipped": "{count} host(s) skipped",
        "cached": "cached",
        "stale": "stale",
        "refreshing": "refreshing",
        "unavailable": "unavailable",
        "available": "available",
        "remote_summary": "Remote: {available} available",
        "remote_warning_summary": "⚠ {unavailable} remote unavailable · {details} · {available} available",
        "remote_refreshing_count": "· {count} refreshing",
        "remote_details_hint": "Ctrl+U Details",
        "remote_details_title": "Unavailable remote hosts",
        "remote_details_help": "↑↓ Scroll  Ctrl+U Back  Esc Back",
        "status_blocked": "blocked",
        "status_working": "working",
        "status_done": "done",
        "status_idle": "idle",
        "status_unknown": "unknown",
        "current_agent": "Current agent",
        "no_focused_agent": "No agent is running in the focused pane.",
        "popup_open_failed": "Could not open Agent Messenger.",
        "sender_unavailable": "The sending agent is no longer available.",
        "no_recipients": "Select at least one recipient.",
        "empty_message": "Enter a message before sending.",
        "sending": "Sending prompt...",
        "delegating": "Sending orchestration request to coordinator...",
        "sending_in_progress": "Prompt submission is already in progress.",
        "cancelling_send": "Cancelling pending prompt submissions...",
        "send_cancelled": "Pending prompt submissions were cancelled.",
        "delivery_unknown": "Delivery to {count} agent(s) is unconfirmed. Check status before retrying.",
        "sent": "Prompt sent to {count} agent(s).",
        "delegated": "Orchestration request sent to the coordinator.",
        "delegate_failed": "The orchestration request could not be sent to the coordinator.",
        "partial_failure": "Sent to {sent}; failed for {failed} agent(s).",
        "all_failed": "Prompt could not be sent to the selected agents.",
        "agent_unavailable": "agent unavailable",
        "host_unavailable": "host unavailable",
        "interactive_required": "Agent Messenger requires an interactive terminal.",
        "skill_guide_title": "Agent Skill Guide",
        "skill_guide_intro": (
            "Use the bundled skill to route requests by host and agent label without "
            "opening this popup."
        ),
        "skill_guide_install": (
            "Run 'HAM Skill' from the command palette, or load this file:"
        ),
        "skill_guide_target": "Target format: host/label",
        "skill_guide_example": (
            "Example: Use the agent messenger skill to ask macbook-pro/purple-koala "
            "for a status report."
        ),
        "skill_guide_close": "Ctrl+G / Esc Back",
        "help_recipients": (
            "↑↓ Move  Space Toggle  Ctrl+A All  Ctrl+D Clear  Tab Message  "
            "Ctrl+O Mode  Ctrl+R Refresh  Ctrl+G Skill  Esc Close"
        ),
        "help_message": (
            "Enter New line  Tab Recipients  Ctrl+O Mode  Ctrl+S Send  Ctrl+G Skill  Esc Close"
        ),
        "help_sending": "Sending...  Esc Cancel pending submissions",
        "help_discovery": (
            "↑↓ Move  Enter Select  D Discover  L Local only  Ctrl+G Skill  Esc Cancel"
        ),
        "help_mode": "↑↓ Move  Enter Select  C Coordinator  D Direct  Ctrl+G Skill  Esc Back",
    },
    "ko": {
        "title": "에이전트 메신저",
        "held_title": "HAM: 보류된 메시지",
        "held_empty": "보류된 메시지가 없습니다.",
        "held_warning": "지금 보내면 수신 창에 작성 중인 미전송 내용과 이 메시지가 합쳐집니다.",
        "held_help": "↑↓ 선택하세요  P 보내세요  C 취소하세요  Q / Esc 닫으세요",
        "held_item": "{sender} → {recipient} [{state}]",
        "held_since": "{since}부터 보류 중이며, 결정 대기 전환 시각은 {deadline}입니다.",
        "held_deferred": "수신 창에서 포커스가 이동하기를 기다립니다",
        "held_awaiting_decision": "사용자 결정을 기다립니다",
        "held_error": "보류된 메시지를 갱신하지 못했습니다: {error}",
        "held_push_not_applied": "메시지가 제출되지 않았습니다 ({state}).",
        "held_cancel_not_applied": "메시지가 취소되지 않았습니다 ({state}).",
        "send_prompt": "프롬프트 보내기",
        "from": "보내는 에이전트",
        "coordinator": "코디네이터",
        "recipients": "수신자",
        "search": "검색",
        "message": "메시지",
        "message_placeholder": "메시지를 입력하세요...",
        "selected": "선택됨",
        "unknown_workspace": "알 수 없는 작업공간",
        "local": "현재 시스템",
        "remote": "원격 에이전트",
        "discover_question": "SSH 호스트의 에이전트를 탐색할까요?",
        "discover_choice": "D 탐색   L 현재 시스템만   Esc 취소",
        "discover_remote_option": "SSH 에이전트 탐색",
        "local_only_option": "현재 시스템의 에이전트만 사용",
        "mode_question": "이 요청을 어떤 방식으로 보낼까요?",
        "delivery_mode": "전송 모드",
        "delegate_option": "코디네이터에게 위임 (기본·권장)",
        "delegate_privacy": "전체 요청은 코디네이터만 받고 작업자는 맞춤 지시만 받습니다.",
        "direct_option": "선택한 에이전트에게 직접 전송 (고급)",
        "direct_privacy": "전체 메시지를 SSH 원격을 포함한 모든 선택 에이전트에게 복사합니다.",
        "no_ssh_hosts": "구체적인 SSH 호스트를 찾지 못했습니다.",
        "discovering": "원격 에이전트를 탐색하는 중",
        "discovery_cancelled": "원격 탐색을 취소했습니다.",
        "discovery_complete": "원격 탐색을 완료했습니다.",
        "hosts_skipped": "호스트 {count}개 건너뜀",
        "cached": "캐시",
        "stale": "오래됨",
        "refreshing": "새로 고치는 중",
        "unavailable": "사용할 수 없음",
        "available": "사용 가능",
        "remote_summary": "원격: {available}개 사용 가능",
        "remote_warning_summary": "⚠ 원격 {unavailable}개 연결 불가 · {details} · {available}개 사용 가능",
        "remote_refreshing_count": "· {count}개 확인 중",
        "remote_details_hint": "Ctrl+U 상세",
        "remote_details_title": "연결할 수 없는 원격 호스트",
        "remote_details_help": "↑↓ 스크롤  Ctrl+U 뒤로  Esc 뒤로",
        "status_blocked": "차단됨",
        "status_working": "작업 중",
        "status_done": "완료",
        "status_idle": "대기 중",
        "status_unknown": "알 수 없음",
        "current_agent": "현재 에이전트",
        "no_focused_agent": "현재 포커스된 창에서 실행 중인 에이전트가 없습니다.",
        "popup_open_failed": "에이전트 메신저를 열 수 없습니다.",
        "sender_unavailable": "메시지를 보내는 에이전트를 더 이상 사용할 수 없습니다.",
        "no_recipients": "수신자를 한 명 이상 선택하세요.",
        "empty_message": "전송할 메시지를 입력하세요.",
        "sending": "프롬프트를 전송하는 중...",
        "delegating": "코디네이터에게 오케스트레이션 요청을 전송하는 중...",
        "sending_in_progress": "이미 프롬프트를 전송하고 있습니다.",
        "cancelling_send": "대기 중인 프롬프트 전송을 취소하는 중...",
        "send_cancelled": "대기 중인 프롬프트 전송을 취소했습니다.",
        "delivery_unknown": "에이전트 {count}명의 수신 여부가 불확실합니다. 재전송 전에 상태를 확인하세요.",
        "sent": "에이전트 {count}명에게 프롬프트를 전송했습니다.",
        "delegated": "코디네이터에게 오케스트레이션 요청을 전송했습니다.",
        "delegate_failed": "코디네이터에게 오케스트레이션 요청을 전송하지 못했습니다.",
        "partial_failure": "{sent}명에게 전송했고 {failed}명은 실패했습니다.",
        "all_failed": "선택한 에이전트에게 프롬프트를 전송하지 못했습니다.",
        "agent_unavailable": "에이전트를 사용할 수 없음",
        "host_unavailable": "호스트에 연결할 수 없음",
        "interactive_required": "에이전트 메신저에는 대화형 터미널이 필요합니다.",
        "skill_guide_title": "에이전트 스킬 안내",
        "skill_guide_intro": (
            "팝업을 열지 않고 호스트와 에이전트 라벨로 요청을 전달하려면 내장 스킬을 사용하세요."
        ),
        "skill_guide_install": (
            "명령 팔레트에서 'HAM Skill'을 실행하거나 다음 파일을 불러오세요:"
        ),
        "skill_guide_target": "대상 형식: host/label",
        "skill_guide_example": (
            "예: 에이전트 메신저 스킬로 macbook-pro/purple-koala에게 상태 보고를 요청해줘."
        ),
        "skill_guide_close": "Ctrl+G / Esc 뒤로",
        "help_recipients": (
            "↑↓ 이동  Space 선택  Ctrl+A 전체  Ctrl+D 해제  Tab 메시지  "
            "Ctrl+O 모드  Ctrl+R 새로고침  Ctrl+G 스킬  Esc 닫기"
        ),
        "help_message": (
            "Enter 줄바꿈  Tab 수신자  Ctrl+O 모드  Ctrl+S 전송  Ctrl+G 스킬  Esc 닫기"
        ),
        "help_sending": "전송 중...  Esc 대기 중인 전송 취소",
        "help_discovery": (
            "↑↓ 이동  Enter 선택  D 탐색  L 현재 시스템만  Ctrl+G 스킬  Esc 취소"
        ),
        "help_mode": "↑↓ 이동  Enter 선택  C 코디네이터  D 직접 전송  Ctrl+G 스킬  Esc 뒤로",
    },
    "ja": {
        "title": "エージェントメッセンジャー",
        "held_title": "HAM: 保留中のメッセージ",
        "held_empty": "保留中のメッセージはありません。",
        "held_warning": "送信すると、宛先ペインの未送信テキストとこのメッセージが結合されます。",
        "held_help": "↑↓ 選択  P 送信  C キャンセル  Q / Esc 閉じる",
        "held_item": "{sender} → {recipient} [{state}]",
        "held_since": "{since} から保留中、期限は {deadline}",
        "held_deferred": "宛先からフォーカスが移るのを待機中",
        "held_awaiting_decision": "操作を待機中",
        "held_error": "保留中のメッセージを更新できませんでした: {error}",
        "held_push_not_applied": "メッセージは送信されませんでした（{state}）。",
        "held_cancel_not_applied": "メッセージはキャンセルされませんでした（{state}）。",
        "send_prompt": "プロンプトを送信",
        "from": "送信元",
        "coordinator": "コーディネーター",
        "recipients": "送信先",
        "search": "検索",
        "message": "メッセージ",
        "message_placeholder": "メッセージを入力してください...",
        "selected": "選択済み",
        "unknown_workspace": "不明なワークスペース",
        "local": "ローカル",
        "remote": "リモートエージェント",
        "discover_question": "SSH ホストのエージェントを検索しますか？",
        "discover_choice": "D 検索   L ローカルのみ   Esc キャンセル",
        "discover_remote_option": "SSH エージェントを検索",
        "local_only_option": "ローカルエージェントのみ使用",
        "mode_question": "このリクエストをどのように送信しますか？",
        "delivery_mode": "送信モード",
        "delegate_option": "コーディネーター経由で委任（既定・推奨）",
        "delegate_privacy": "全文はコーディネーターだけが受け取り、作業者には個別指示のみ送られます。",
        "direct_option": "選択したエージェントへ直接送信（上級）",
        "direct_privacy": "SSH 接続先を含む選択した全エージェントへメッセージ全文を複製します。",
        "no_ssh_hosts": "具体的な SSH ホストが見つかりません。",
        "discovering": "リモートエージェントを検索中",
        "discovery_cancelled": "リモート検索をキャンセルしました。",
        "discovery_complete": "リモート検索が完了しました。",
        "hosts_skipped": "{count}件のホストをスキップ",
        "cached": "キャッシュ",
        "stale": "古い情報",
        "refreshing": "更新中",
        "unavailable": "利用不可",
        "available": "利用可能",
        "remote_summary": "リモート: {available}件利用可能",
        "remote_warning_summary": "⚠ リモート{unavailable}件利用不可 · {details} · {available}件利用可能",
        "remote_refreshing_count": "· {count}件確認中",
        "remote_details_hint": "Ctrl+U 詳細",
        "remote_details_title": "利用できないリモートホスト",
        "remote_details_help": "↑↓ スクロール  Ctrl+U 戻る  Esc 戻る",
        "status_blocked": "ブロック中",
        "status_working": "作業中",
        "status_done": "完了",
        "status_idle": "待機中",
        "status_unknown": "不明",
        "current_agent": "現在のエージェント",
        "no_focused_agent": "フォーカス中のペインで実行中のエージェントがありません。",
        "popup_open_failed": "エージェントメッセンジャーを開けませんでした。",
        "sender_unavailable": "送信元のエージェントは利用できなくなりました。",
        "no_recipients": "送信先を1件以上選択してください。",
        "empty_message": "送信するメッセージを入力してください。",
        "sending": "プロンプトを送信中...",
        "delegating": "コーディネーターへオーケストレーション依頼を送信中...",
        "sending_in_progress": "プロンプトはすでに送信中です。",
        "cancelling_send": "保留中のプロンプト送信をキャンセル中...",
        "send_cancelled": "保留中のプロンプト送信をキャンセルしました。",
        "delivery_unknown": "{count}件の受信を確認できません。再送前に状態を確認してください。",
        "sent": "{count}件のエージェントにプロンプトを送信しました。",
        "delegated": "コーディネーターへオーケストレーション依頼を送信しました。",
        "delegate_failed": "コーディネーターへオーケストレーション依頼を送信できませんでした。",
        "partial_failure": "{sent}件に送信し、{failed}件は失敗しました。",
        "all_failed": "選択したエージェントにプロンプトを送信できませんでした。",
        "agent_unavailable": "エージェントを利用できません",
        "host_unavailable": "ホストに接続できません",
        "interactive_required": "エージェントメッセンジャーには対話型ターミナルが必要です。",
        "skill_guide_title": "エージェントスキルガイド",
        "skill_guide_intro": (
            "ポップアップを開かず、ホストとエージェントラベルで依頼を送るには同梱スキルを使用します。"
        ),
        "skill_guide_install": (
            "コマンドパレットで 'HAM Skill' を実行するか、このファイルを読み込みます:"
        ),
        "skill_guide_target": "宛先形式: host/label",
        "skill_guide_example": (
            "例: エージェントメッセンジャースキルで macbook-pro/purple-koala に状況報告を依頼。"
        ),
        "skill_guide_close": "Ctrl+G / Esc 戻る",
        "help_recipients": (
            "↑↓ 移動  Space 選択  Ctrl+A 全選択  Ctrl+D 解除  Tab メッセージ  "
            "Ctrl+O モード  Ctrl+R 更新  Ctrl+G スキル  Esc 閉じる"
        ),
        "help_message": (
            "Enter 改行  Tab 送信先  Ctrl+O モード  Ctrl+S 送信  Ctrl+G スキル  Esc 閉じる"
        ),
        "help_sending": "送信中...  Esc 保留中の送信をキャンセル",
        "help_discovery": (
            "↑↓ 移動  Enter 選択  D 検索  L ローカルのみ  Ctrl+G スキル  Esc キャンセル"
        ),
        "help_mode": "↑↓ 移動  Enter 選択  C コーディネーター  D 直接送信  Ctrl+G スキル  Esc 戻る",
    },
}


def normalize_language(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(?:^|[^a-z])(en|ja|ko)(?:[-_.@]|$)", value.lower())
    return match.group(1) if match else None


def _macos_language() -> str | None:
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleLanguages"],
            capture_output=True,
            check=False,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return normalize_language(result.stdout)


def detect_language(environment: Mapping[str, str] | None = None) -> str:
    values = os.environ if environment is None else environment
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = values.get(name)
        if value:
            return normalize_language(value) or "en"

    try:
        language = normalize_language(locale.getlocale()[0])
    except (ValueError, TypeError):
        language = None
    return language or _macos_language() or "en"


def messages(language: str | None = None) -> dict[str, str]:
    selected = language if language in SUPPORTED_LANGUAGES else detect_language()
    return MESSAGES.get(selected, MESSAGES["en"])
