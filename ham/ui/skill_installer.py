#!/usr/bin/env python3
"""Interactive installer for the bundled Herdr Agent Messenger skill."""

from __future__ import annotations

import argparse
import curses
from dataclasses import dataclass
import filecmp
import os
from pathlib import Path
import shutil
import shlex
import sys
import tempfile
from typing import Mapping, Sequence

from ham.ui.messenger import (
    bundled_skill_path,
    decode_json_object,
    launch_plugin_overlay,
    SKILL_INSTALLER_ENTRYPOINT,
    SKILL_PROJECT_ROOT_ENV,
    show_notification,
)
from ham.ui.messenger_i18n import detect_language
from ham.ui.terminal_text import (
    display_width,
    pad_display_text,
    truncate_display_text,
    wrap_display_text,
)
from ham.ui.tui_style import Theme, Hint, content_rect, draw_footer, draw_status, draw_title, footer_lines
from ham.ledger.store import LedgerError
from ham.ops import runtime_installer as runtime_installer


SKILL_NAME = "herdr-agent-messenger"
STATUS_CURRENT = "current"
STATUS_OUTDATED = "outdated"
STATUS_MISSING = "missing"
STATUS_CONFLICT = "conflict"
STATUS_UNAVAILABLE = "unavailable"
STATUS_BADGES = {
    STATUS_CURRENT: "✓",
    STATUS_OUTDATED: "↑",
    STATUS_MISSING: "○",
    STATUS_CONFLICT: "!",
    STATUS_UNAVAILABLE: "–",
}


@dataclass(frozen=True)
class InstallTarget:
    scope: str
    agent: str
    destination: Path | None

COPY = {
    "en": {
        "intro": "Select where HAM (Herdr Agent Messenger) should be available.",
        "project": "Project",
        "system": "System",
        "codex": "Codex",
        "claude": "Claude",
        "omp": "omp",
        "current": "Current",
        "outdated": "Update available",
        "missing": "Not installed",
        "conflict": "Conflicting files",
        "unavailable": "Project unavailable",
        "ready": "Select a target and press Enter.",
        "already_current": "Already current.",
        "installed": "Skill installed. Start a new agent session to discover it.",
        "runtime_installed": "Installed",
        "runtime_intro": "Plugin and skill do not install a receiver. Codex uses skill-guided pull.",
        "runtime_not_now": "Not now (default)",
        "runtime_install": "Install {client} receiver",
        "runtime_files": "Writes: {files}",
        "runtime_consent": "Enter installs only the selected runtime. Foreign or modified files are never replaced.",
        "runtime_help": "↑↓ Choose  Enter Confirm  PgUp/PgDn Scroll  Esc Not now",
        "runtime_activation": "Only sessions you choose to restart pick up the receiver; confirm extension_present=1, otherwise installed, activation pending.",
        "runtime_report": "Receiving runtimes",
        "failed": "Installation failed: {error}",
        "usage_intro": "Address agents as host/label; use local for this Herdr server.",
        "usage_codex": "Codex: type $ham, then select HAM — Herdr Agent Messenger.",
        "usage_claude": "Claude Code: invoke /herdr-agent-messenger.",
        "usage_omp": 'omp: ask in natural language (e.g. "Use HAM to ask local/blue-otter for a status report") or reference skill://herdr-agent-messenger.',
        "usage_target": "Example target: local/yellow-falcon",
        "usage_example": "Prompt: Ask local/yellow-falcon for a status report, wait, and report back.",
        "usage_scope": "Project affects this workspace; System affects every workspace.",
        "usage_standalone": "Without the plugin, use the source-backed HAM setup in README and name agents manually.",
        "help": "↑↓ Move  Enter Install/Update  ? Usage  Esc Close",
        "usage_help": "? Back  Esc Back",
        "open_failed": "Could not open the skill installer.",
    },
    "ko": {
        "intro": "HAM(Herdr Agent Messenger)을 사용할 범위를 선택하세요.",
        "project": "프로젝트",
        "system": "시스템",
        "codex": "Codex",
        "claude": "Claude",
        "omp": "omp",
        "current": "최신",
        "outdated": "업데이트 가능",
        "missing": "설치되지 않음",
        "conflict": "다른 파일과 충돌",
        "unavailable": "프로젝트를 확인할 수 없음",
        "ready": "대상을 선택하고 Enter를 누르세요.",
        "already_current": "이미 최신 상태입니다.",
        "installed": "스킬을 설치했습니다. 새 에이전트 세션을 시작하면 스킬을 찾을 수 있습니다.",
        "runtime_installed": "설치됨",
        "runtime_intro": "플러그인과 스킬은 수신기를 설치하지 않습니다. Codex는 스킬 안내에 따라 메일을 가져옵니다.",
        "runtime_not_now": "나중에 (기본값)",
        "runtime_install": "{client} 수신기 설치",
        "runtime_files": "기록할 파일: {files}",
        "runtime_consent": "Enter를 누르면 선택한 런타임만 설치합니다. 소유하지 않거나 수정된 파일은 덮어쓰지 않습니다.",
        "runtime_help": "↑↓ 선택  Enter 확인  PgUp/PgDn 스크롤  Esc 나중에",
        "runtime_activation": "직접 선택해 재시작한 세션에만 수신기가 적용됩니다. extension_present=1을 확인하지 못하면 설치됨, 활성화 대기 상태입니다.",
        "runtime_report": "수신 런타임",
        "failed": "설치 실패: {error}",
        "usage_intro": "에이전트는 host/label로 지정하며 현재 Herdr 서버는 local을 사용합니다.",
        "usage_codex": "Codex: $ham을 입력한 뒤 HAM — Herdr Agent Messenger를 선택하세요.",
        "usage_claude": "Claude Code: /herdr-agent-messenger를 호출하세요.",
        "usage_omp": 'omp: "HAM으로 local/blue-otter에게 상태를 물어봐"처럼 자연어로 요청하거나 skill://herdr-agent-messenger를 참조하세요.',
        "usage_target": "대상 예: local/yellow-falcon",
        "usage_example": "프롬프트: local/yellow-falcon에게 상태를 묻고 응답을 기다린 뒤 보고해줘.",
        "usage_scope": "프로젝트는 현재 작업공간, 시스템은 모든 작업공간에 적용됩니다.",
        "usage_standalone": "플러그인 없이 쓰려면 README의 소스 연결 설치를 사용하고 에이전트 이름을 직접 지정하세요.",
        "help": "↑↓ 이동  Enter 설치/업데이트  ? 사용법  Esc 닫기",
        "usage_help": "? 뒤로  Esc 뒤로",
        "open_failed": "스킬 설치 창을 열 수 없습니다.",
    },
    "ja": {
        "intro": "HAM (Herdr Agent Messenger) を使用する範囲を選択してください。",
        "project": "プロジェクト",
        "system": "システム",
        "codex": "Codex",
        "claude": "Claude",
        "omp": "omp",
        "current": "最新",
        "outdated": "更新可能",
        "missing": "未インストール",
        "conflict": "別のファイルと競合",
        "unavailable": "プロジェクトを確認できません",
        "ready": "対象を選び Enter を押してください。",
        "already_current": "すでに最新です。",
        "installed": "スキルをインストールしました。新しいエージェントセッションでスキルを検出できます。",
        "runtime_installed": "インストール済み",
        "runtime_intro": "プラグインとスキルは受信機をインストールしません。Codex はスキルに従い取得します。",
        "runtime_not_now": "今はしない (既定)",
        "runtime_install": "{client} の受信機をインストール",
        "runtime_files": "書き込み: {files}",
        "runtime_consent": "Enter で選択したランタイムだけをインストールします。他者所有・変更済みのファイルは置換しません。",
        "runtime_help": "↑↓ 選択  Enter 確認  PgUp/PgDn スクロール  Esc 今はしない",
        "runtime_activation": "選択して再起動したセッションだけが受信機を読み込みます。extension_present=1 を確認できなければインストール済み・有効化待ちです。",
        "runtime_report": "受信ランタイム",
        "failed": "インストール失敗: {error}",
        "usage_intro": "エージェントは host/label で指定し、現在の Herdr サーバーには local を使います。",
        "usage_codex": "Codex: $ham と入力し、HAM — Herdr Agent Messenger を選択します。",
        "usage_claude": "Claude Code: /herdr-agent-messenger を呼び出します。",
        "usage_omp": 'omp: 「HAM で local/blue-otter に状態を聞いて」のように自然言語で依頼するか、skill://herdr-agent-messenger を参照します。',
        "usage_target": "宛先の例: local/yellow-falcon",
        "usage_example": "プロンプト: local/yellow-falcon に状態を尋ね、応答を待って報告する。",
        "usage_scope": "プロジェクトは現在のワークスペース、システムはすべてに適用されます。",
        "usage_standalone": "プラグインなしでは README のソース連携設定を使い、エージェント名を手動で付けます。",
        "help": "↑↓ 移動  Enter インストール/更新  ? 使い方  Esc 閉じる",
        "usage_help": "? 戻る  Esc 戻る",
        "open_failed": "スキルインストーラーを開けませんでした。",
    },
}


def install_destinations(
    environment: Mapping[str, str] | None = None,
) -> dict[str, Path]:
    values = os.environ if environment is None else environment
    home = Path(values.get("HOME") or Path.home()).expanduser()
    return {
        "codex": home / ".agents" / "skills" / SKILL_NAME,
        "claude": home / ".claude" / "skills" / SKILL_NAME,
        "omp": home / ".omp" / "agent" / "skills" / SKILL_NAME,
    }


def project_root(environment: Mapping[str, str] | None = None) -> Path | None:
    values = os.environ if environment is None else environment
    explicit = values.get(SKILL_PROJECT_ROOT_ENV)
    context = decode_json_object(values.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    workspace = context.get("workspace")
    nested_cwd = workspace.get("cwd") if isinstance(workspace, dict) else None
    raw_path = explicit or context.get("workspace_cwd") or nested_cwd
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    return Path(raw_path).expanduser().resolve()


def installation_targets(
    environment: Mapping[str, str] | None = None,
) -> tuple[InstallTarget, ...]:
    values = os.environ if environment is None else environment
    root = project_root(values)
    system = install_destinations(values)
    project = {
        "codex": root / ".agents" / "skills" / SKILL_NAME if root else None,
        "claude": root / ".claude" / "skills" / SKILL_NAME if root else None,
        "omp": root / ".omp" / "skills" / SKILL_NAME if root else None,
    }
    return tuple(
        InstallTarget(scope, agent, destinations[agent])
        for scope, destinations in (("project", project), ("system", system))
        for agent in ("codex", "claude", "omp")
    )


def _bundled_claude_skill_path() -> Path:
    return bundled_skill_path().parents[3] / ".claude" / "skills" / SKILL_NAME


def _source_files(source: Path) -> list[Path]:
    return sorted(
        path
        for path in source.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
        and path.name != ".DS_Store"
    )


def _matches_source(destination: Path, source: Path) -> bool:
    try:
        if destination.resolve() == source.resolve():
            return True
        source_files = _source_files(source)
        return bool(source_files) and all(
            (destination / path.relative_to(source)).is_file()
            and filecmp.cmp(
                path,
                destination / path.relative_to(source),
                shallow=False,
            )
            for path in source_files
        )
    except (OSError, UnicodeError):
        return False


def _is_managed_destination(destination: Path) -> bool:
    skill_file = destination / "SKILL.md"
    script_file = destination / "scripts" / "herdr_agent_messenger.py"
    try:
        contents = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    canonical = "name: herdr-agent-messenger" in contents and script_file.is_file()
    claude_bridge = (
        "name: herdr-agent-messenger" in contents
        and "../../../.agents/skills/herdr-agent-messenger/SKILL.md" in contents
    )
    return canonical or claude_bridge


def installation_status(destination: Path | None) -> str:
    if destination is None:
        return STATUS_UNAVAILABLE
    if destination.is_symlink():
        return STATUS_CONFLICT
    if not destination.exists():
        return STATUS_MISSING
    if not destination.is_dir():
        return STATUS_CONFLICT
    if not _is_managed_destination(destination):
        return STATUS_CONFLICT
    sources = (bundled_skill_path().parent, _bundled_claude_skill_path())
    try:
        _check_destination_paths(destination, sources[0])
    except OSError:
        return STATUS_CONFLICT
    if any(source.is_dir() and _matches_source(destination, source) for source in sources):
        return STATUS_CURRENT
    return STATUS_OUTDATED


def _check_destination_paths(destination: Path, source: Path) -> None:
    for source_file in _source_files(source):
        target = destination
        for component in source_file.relative_to(source).parts:
            target /= component
            if target.is_symlink():
                raise OSError(f"refusing to overwrite a symlink: {target}")


def install_skill(destination: Path, source: Path | None = None) -> None:
    """Install the complete skill bundle, including its on-demand reference."""
    source_directory = (
        bundled_skill_path().parent if source is None else source
    ).resolve()
    if destination.resolve() == source_directory:
        return
    if destination.is_symlink() or (
        destination.exists() and not destination.is_dir()
    ):
        raise OSError(f"destination is not a managed skill directory: {destination}")
    if destination.exists() and not _is_managed_destination(destination):
        raise OSError(f"refusing to overwrite an unrelated skill: {destination}")
    _check_destination_paths(destination, source_directory)
    if source_directory in destination.resolve().parents:
        raise OSError("destination must not be inside the source skill directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    staged = staging_root / "skill"
    backup = staging_root / "previous"
    retain_backup = False
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store")
    try:
        # Copy first so a failed read/write cannot leave a mixed installation.
        # Preserve user-added files and links without following those links.
        if destination.exists():
            shutil.copytree(destination, staged, symlinks=True, ignore=ignored)
        shutil.copytree(source_directory, staged, dirs_exist_ok=True, ignore=ignored)
        try:
            if destination.exists():
                os.replace(destination, backup)
            os.replace(staged, destination)
        except BaseException:
            # Ctrl+C must restore the old directory just like an I/O failure.
            if backup.exists():
                try:
                    os.replace(backup, destination)
                except OSError as error:
                    retain_backup = True
                    raise OSError(
                        f"could not restore the previous skill; backup retained at {backup}"
                    ) from error
            raise
    finally:
        if not retain_backup:
            shutil.rmtree(staging_root, ignore_errors=True)


def install_selected(
    selection: str,
    environment: Mapping[str, str] | None = None,
) -> list[Path]:
    destinations = install_destinations(environment)
    if selection not in {"codex", "claude", "omp", "all"}:
        raise ValueError(f"unknown installation target: {selection}")
    names = ("codex", "claude", "omp") if selection == "all" else (selection,)
    installed: list[Path] = []
    for name in names:
        destination = destinations[name]
        install_skill(destination)
        installed.append(destination)
    return installed


def install_target(target: InstallTarget) -> Path:
    if target.destination is None:
        raise OSError("project workspace is unavailable")
    install_skill(target.destination)
    return target.destination


def runtime_report(environment: Mapping[str, str] | None, text: Mapping[str, str]) -> str:
    """Keep install stdout clean while disclosing missing receivers and conflicts."""
    entries = []
    for runtime in runtime_installer.summary(environment)["clients"]:
        client = runtime["client"]
        if runtime["error"]:
            state = runtime["error"]["code"] + ": " + runtime["error"]["message"]
        elif runtime["installed"]:
            state = text["runtime_installed"]
        else:
            launcher = bundled_skill_path().parent / "scripts" / "herdr_agent_messenger.py"
            command = shlex.join((sys.executable, str(launcher), "runtime", "install", client))
            state = text["missing"] + " — " + command
        entries.append(f"{client}: {state}")
    return text["runtime_report"] + ": " + "; ".join(entries) + ". " + text["runtime_activation"]


def _footer_hints(value: str) -> list[Hint]:
    hints = []
    for item in value.split("  "):
        key, separator, action = item.partition(" ")
        if separator:
            priority = 0 if key in ("Esc", "Enter", "?") else 100
            hints.append(Hint(key, action, priority))
    return hints


def offer_runtime_install(screen: curses.window, environment: Mapping[str, str],
                          text: Mapping[str, str], status: str = "") -> str:
    """Display both runtimes; installing requires selecting a non-default choice."""
    theme = Theme(environment)
    hints = _footer_hints(text["runtime_help"])
    status_kind = "success" if status in (text["installed"], text["already_current"]) else "info"
    cursor = 0
    scroll = 0
    root = project_root(environment)
    title_context = f"project {root.name if root is not None else '—'}"
    while True:
        runtimes = runtime_installer.summary(environment)["clients"]
        missing = [runtime for runtime in runtimes if not runtime["installed"] and not runtime["error"]]
        options = [text["runtime_not_now"]] + [
            text["runtime_install"].format(client=runtime["client"]) for runtime in missing]
        cursor = min(cursor, len(options) - 1)
        lines = [(text["runtime_intro"], theme.style()), ("", 0)]
        for runtime in runtimes:
            error = runtime["error"]
            state = (error["code"] + ": " + error["message"] if error else
                     text["runtime_installed"] if runtime["installed"] else text["missing"])
            role = "error" if error else "success" if runtime["installed"] else "text"
            lines.append((f'{runtime["client"]}: {state}', theme.style(role)))
            lines.append((runtime["destination"], theme.style("muted")))
        lines.append(("", 0))
        selected_option_row = len(lines) + cursor
        lines += [("  " + label, theme.style()) for label in options]
        if cursor:
            runtime = missing[cursor - 1]
            files = (("ham-mailbox.ts", "ham-mailbox.install.json") if runtime["client"] == "omp" else
                     ("hooks/ham-mailbox.py", "ham-mailbox.install.json", "settings.json (SessionStart, UserPromptSubmit)"))
            lines += [("", 0), (runtime["destination"], theme.style("muted")),
                      (text["runtime_files"].format(files=", ".join(files)), theme.style()),
                      (text["runtime_consent"], theme.style())]
        screen.erase()
        view = content_rect(*screen.getmaxyx(), 80).window(screen)
        height, width = view.getmaxyx()
        usable_width = max(1, width - 1)
        draw_title(view, theme, "Skill installer", title_context, usable_width)
        wrapped = []
        for index, (value, attribute) in enumerate(lines):
            selected = index == selected_option_row
            if selected:
                value = value[2:]
            line_width = max(1, min(76, usable_width) - (2 if selected else 0))
            for line_index, line in enumerate(wrap_display_text(value, line_width) if value else [""]):
                wrapped.append((line, attribute, selected, line_index))
        status_lines = wrap_display_text(status, usable_width) if status else []
        footer_height = len(footer_lines(hints, usable_width))
        status_height = min(len(status_lines), max(0, height - footer_height - 3))
        body_height = max(1, height - footer_height - status_height - 3)
        if scroll < 0:
            scroll = max(0, len(wrapped) - body_height)
        scroll = max(0, min(scroll, len(wrapped) - body_height))
        visible = wrapped[scroll:scroll + body_height]
        for row, (value, attribute, selected, line_index) in enumerate(visible, 2):
            if not 0 <= row < height:
                continue
            if selected:
                theme.draw_selected_row(
                    view, row, value, usable_width,
                    marker="› " if line_index == 0 else "  ",
                )
                continue
            try:
                view.addnstr(row, 0, truncate_display_text(value, usable_width),
                             usable_width, attribute)
            except curses.error:
                pass
        row = 2 + len(visible)
        if status_height:
            row += 1
            for line in status_lines[:status_height]:
                draw_status(view, row, 0, usable_width, line, theme, kind=status_kind)
                row += 1
        draw_footer(view, row, 0, usable_width, hints, theme)
        screen.refresh()
        key = screen.get_wch()
        if key in ("\x1b", "q", "Q"):
            return status
        if key in (curses.KEY_UP, curses.KEY_DOWN):
            cursor = (cursor + (1 if key == curses.KEY_DOWN else -1)) % len(options)
            scroll = -1 if cursor else 0
        elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
            scroll += body_height if key == curses.KEY_NPAGE else -body_height
        elif key in ("\n", "\r", curses.KEY_ENTER, " "):
            if cursor == 0:
                return status
            runtime = missing[cursor - 1]
            try:
                runtime_installer.manage("install", runtime["destination"], client=runtime["client"])
                status = text["runtime_activation"]
                status_kind = "warning"
            except LedgerError as error:
                status = error.code + ": " + error.message
                status_kind = "error"
            except OSError as error:
                status = str(error)
                status_kind = "error"
            cursor = 0
            scroll = 0


def launch(environment: Mapping[str, str] | None = None) -> int:
    language = detect_language(environment)
    text = COPY[language]
    root = project_root(environment)
    extra_arguments: tuple[str, ...] = ()
    if root is not None:
        extra_arguments = ("--env", f"{SKILL_PROJECT_ROOT_ENV}={root}")
    if launch_plugin_overlay(
        SKILL_INSTALLER_ENTRYPOINT,
        environment=environment,
        extra_arguments=extra_arguments,
    ):
        return 0
    if not show_notification(text["open_failed"], environment):
        print(text["open_failed"], file=sys.stderr)
    return 1


def overlay(environment: Mapping[str, str] | None = None) -> int:
    values = dict(os.environ if environment is None else environment)
    text = COPY[detect_language(values)]
    targets = installation_targets(values)
    root = project_root(values)
    title_context = f"project {root.name if root is not None else '—'}"

    def run(screen: curses.window) -> int:
        screen.keypad(True)
        theme = Theme(values)
        cursor = 0
        status = text["ready"]
        status_kind = "info"
        usage_visible = False
        hints = _footer_hints(text["help"])
        usage_hints = _footer_hints(text["usage_help"])
        status_roles = {
            STATUS_CURRENT: "success",
            STATUS_OUTDATED: "warning",
            STATUS_CONFLICT: "error",
            STATUS_UNAVAILABLE: "warning",
        }
        name_width = max(display_width(text[target.agent]) for target in targets)
        status_column = 2 + name_width + 2

        while True:
            screen.erase()
            view = content_rect(*screen.getmaxyx(), 80).window(screen)
            height, width = view.getmaxyx()
            usable_width = max(1, width - 1)
            draw_title(view, theme, "Skill installer", title_context, usable_width)

            def add(row: int, column: int, value: str, attribute: int = 0) -> None:
                if not (0 <= row < height and 0 <= column < width):
                    return
                try:
                    view.addnstr(
                        row,
                        column,
                        truncate_display_text(value, max(0, usable_width - column)),
                        max(0, usable_width - column),
                        attribute,
                    )
                except curses.error:
                    pass

            if usage_visible:
                row = 2
                footer_height = len(footer_lines(usage_hints, usable_width))
                body_end = max(2, height - footer_height)
                for value in (
                    text["usage_intro"],
                    "",
                    text["usage_codex"],
                    text["usage_claude"],
                    text["usage_omp"],
                    "",
                    text["usage_target"],
                    text["usage_example"],
                    "",
                    text["usage_scope"],
                    text["usage_standalone"],
                ):
                    for line in wrap_display_text(value, max(1, min(76, width - 4))) if value else [""]:
                        if row < body_end:
                            add(row, 2, line)
                            row += 1
                draw_footer(view, min(row + 1, body_end), 0, usable_width, usage_hints, theme)
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
                screen.refresh()
                key = screen.get_wch()
                if key in ("?", "\x1b", "q", "Q", "\n", "\r", curses.KEY_ENTER):
                    usage_visible = False
                continue

            row = 2
            for line in wrap_display_text(text["intro"], max(1, width - 2)):
                add(row, 0, line)
                row += 1
            row += 1
            states = [installation_status(target.destination) for target in targets]
            menu: list[tuple[str, int | None]] = []
            for scope in ("project", "system"):
                scope_label = text[scope]
                if scope == "project":
                    root = project_root(values)
                    if root is not None:
                        scope_label += f" · {root.name}"
                menu.append((scope_label, None))
                for index, target in enumerate(targets):
                    if target.scope == scope:
                        name = pad_display_text(text[target.agent], name_width)
                        label = f"{STATUS_BADGES[states[index]]} {text[states[index]]}"
                        menu.append((f"  {name}  {label}", index))
                menu.append(("", None))

            destination = targets[cursor].destination
            detail = str(destination) if destination is not None else text["unavailable"]
            detail_lines = wrap_display_text(detail, max(1, usable_width - 2))
            status_lines = wrap_display_text(status, usable_width)
            footer_height = len(footer_lines(hints, usable_width))
            # Keep feedback and controls together; only overflowing menus scroll.
            tail_space = len(detail_lines) + len(status_lines) + footer_height + 1
            menu_height = max(1, height - row - tail_space)
            selected_row = next(index for index, (_, target) in enumerate(menu) if target == cursor)
            menu_start = max(0, min(selected_row - menu_height + 1, len(menu) - menu_height))
            for value, index in menu[menu_start:menu_start + menu_height]:
                if index is None:
                    add(row, 0, value, theme.style("accent", bold=True))
                elif index == cursor:
                    theme.draw_selected_row(
                        view, row, value[2:], usable_width - 2, x=2,
                    )
                else:
                    add(row, 2, value[:2] + pad_display_text(text[targets[index].agent], name_width))
                    add(row, 2 + status_column,
                        f"{STATUS_BADGES[states[index]]} {text[states[index]]}",
                        theme.style(status_roles.get(states[index], "text")))
                row += 1
            tail_end = max(0, height - footer_height)
            for line in detail_lines:
                if row >= tail_end - len(status_lines) - 1:
                    break
                add(row, 2, line)
                row += 1
            row = min(row + 1, max(0, tail_end - len(status_lines)))
            for line in status_lines:
                if row >= tail_end:
                    break
                draw_status(view, row, 0, usable_width, line, theme, kind=status_kind)
                row += 1
            draw_footer(view, row, 0, usable_width, hints, theme)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            screen.refresh()

            key = screen.get_wch()
            if key == "?":
                usage_visible = True
            elif key == curses.KEY_UP:
                cursor = (cursor - 1) % len(targets)
            elif key == curses.KEY_DOWN:
                cursor = (cursor + 1) % len(targets)
            elif key in ("\n", "\r", curses.KEY_ENTER, " "):
                target = targets[cursor]
                try:
                    if installation_status(target.destination) == STATUS_CURRENT:
                        status = text["already_current"]
                    else:
                        install_target(target)
                        status = text["installed"]
                    status = offer_runtime_install(screen, values, text, status)
                    status_kind = (
                        "warning" if status == text["runtime_activation"] else
                        "success" if status in (text["installed"], text["already_current"]) else "error"
                    )
                except OSError as error:
                    status = text["failed"].format(error=error)
                    status_kind = "error"
            elif key in ("\x1b", "q", "Q"):
                return 0

    return curses.wrapper(run)


def parse_cli_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install the Agent Messenger skill.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("launch")
    subparsers.add_parser("overlay")
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("target", choices=("codex", "claude", "omp", "all"))
    install_parser.add_argument("--runtime", choices=("omp", "claude", "all"),
                                help="Explicit consent to also install selected ownership-checked receiving runtimes.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_cli_arguments(argv)
    if arguments.command == "launch":
        return launch()
    if arguments.command == "overlay":
        return overlay()
    text = COPY[detect_language()]
    try:
        install_selected(arguments.target)
    except OSError as error:
        print(error, file=sys.stderr)
        return 1
    result = 0
    if arguments.runtime:
        selected = ("omp", "claude") if arguments.runtime == "all" else (arguments.runtime,)
        for client in selected:
            try:
                runtime_installer.manage("install", runtime_installer.destinations()[client], client=client)
            except (LedgerError, OSError) as error:
                print((error.code + ": " + error.message) if isinstance(error, LedgerError) else str(error),
                      file=sys.stderr)
                result = 1
    print(runtime_report(None, text), file=sys.stderr)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
