from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse

from orchestrator.opencode_plugin_runner import (
    DERIVED_SUMMARY_RELATIVE_PATH,
    REPO_ROOT,
    REPORT_RELATIVE_PATH,
    RUN_METADATA_RELATIVE_PATH,
    render_capture_report,
)

DEFAULT_CAPTURES_ROOT = REPO_ROOT / "artifacts" / "captures"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _read_json_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _strip_run_paths(run_metadata: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(run_metadata)
    for key in ("capture_dir", "workspace_dir", "summary_path", "report_path"):
        sanitized.pop(key, None)
    return sanitized


def _format_entry(capture_dir: Path, run_metadata: dict[str, Any], *, has_summary: bool, report_ready: bool, error: str | None) -> dict[str, Any]:
    entry = {
        "capture_id": capture_dir.name,
        "directory_name": capture_dir.name,
        "issue_id": run_metadata.get("issue_id"),
        "title": run_metadata.get("issue_title"),
        "capture_status": run_metadata.get("capture_status"),
        "started_at": run_metadata.get("started_at"),
        "finished_at": run_metadata.get("finished_at"),
        "event_count": run_metadata.get("event_count"),
        "session_id": run_metadata.get("session_id"),
        "has_summary": has_summary,
        "report_ready": report_ready,
    }
    if error:
        entry["error"] = error
    return entry


class CaptureSessionStore:
    def __init__(self, captures_root: Path = DEFAULT_CAPTURES_ROOT) -> None:
        self.captures_root = captures_root
        self._lock = RLock()
        self._sessions: list[dict[str, Any]] = []
        self._sessions_by_id: dict[str, dict[str, Any]] = {}

    def _ensure_derived_artifacts(self, capture_dir: Path) -> tuple[bool, bool, str | None]:
        summary_path = capture_dir / DERIVED_SUMMARY_RELATIVE_PATH
        report_path = capture_dir / REPORT_RELATIVE_PATH
        summary_ready = False
        report_ready = False

        if summary_path.exists():
            try:
                _read_json_file(summary_path)
                summary_ready = True
            except (OSError, ValueError, json.JSONDecodeError):
                summary_ready = False

        if report_path.exists():
            report_ready = True

        if summary_ready and report_ready:
            return True, True, None

        try:
            render_capture_report(capture_dir)
        except Exception as exc:  # pragma: no cover - defensive
            return summary_ready, report_ready, str(exc)

        return True, True, None

    def refresh(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        sessions_by_id: dict[str, dict[str, Any]] = {}
        self.captures_root.mkdir(parents=True, exist_ok=True)

        for capture_dir in sorted(
            (path for path in self.captures_root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        ):
            run_metadata_path = capture_dir / RUN_METADATA_RELATIVE_PATH
            if not run_metadata_path.exists():
                continue

            try:
                run_metadata = _read_json_file(run_metadata_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"[capture-browser] skipping invalid run metadata for {capture_dir.name}: {exc}", flush=True)
                continue

            has_summary, report_ready, error = self._ensure_derived_artifacts(capture_dir)
            entry = _format_entry(
                capture_dir,
                run_metadata,
                has_summary=has_summary,
                report_ready=report_ready,
                error=error,
            )
            sessions.append(entry)
            sessions_by_id[capture_dir.name] = entry

        with self._lock:
            self._sessions = sessions
            self._sessions_by_id = sessions_by_id
            return [dict(item) for item in self._sessions]

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._sessions]

    def get_session_detail(self, capture_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = dict(self._sessions_by_id.get(capture_id, {}))

        if not session:
            return None

        capture_dir = self.captures_root / capture_id
        run_metadata = _strip_run_paths(_read_json_file(capture_dir / RUN_METADATA_RELATIVE_PATH))

        summary: dict[str, Any] | None = None
        error = session.get("error")
        summary_path = capture_dir / DERIVED_SUMMARY_RELATIVE_PATH
        if session.get("has_summary") and summary_path.exists():
            try:
                summary = _read_json_file(summary_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                error = str(exc)
                session["has_summary"] = False

        return {
            "session": session,
            "run": run_metadata,
            "summary": summary,
            "paths": {
                "capture_dir": capture_id,
                "run_metadata": _relative_path(capture_dir / RUN_METADATA_RELATIVE_PATH, self.captures_root),
                "summary": _relative_path(summary_path, self.captures_root),
                "report": _relative_path(capture_dir / REPORT_RELATIVE_PATH, self.captures_root),
            },
            "error": error,
        }


# def _browser_app_html() -> str:
#     return """<!DOCTYPE html>
# <html lang="zh-CN">
# <head>
#   <meta charset="utf-8">
#   <meta name="viewport" content="width=device-width, initial-scale=1">
#   <title>OpenCode 会话浏览</title>
#   <style>
#     :root {
#       --bg: #f6f1e8;
#       --card: rgba(255, 252, 246, 0.92);
#       --ink: #1f1d1a;
#       --muted: #645e56;
#       --accent: #005f73;
#       --accent-soft: rgba(0, 95, 115, 0.12);
#       --success: #2d6a4f;
#       --warn: #bc6c25;
#       --error: #9b2226;
#       --border: rgba(31, 29, 26, 0.12);
#       --shadow: 0 18px 50px rgba(67, 56, 42, 0.1);
#     }
#     * { box-sizing: border-box; }
#     body {
#       margin: 0;
#       color: var(--ink);
#       background:
#         radial-gradient(circle at top left, rgba(233, 196, 106, 0.22), transparent 35%),
#         radial-gradient(circle at top right, rgba(0, 95, 115, 0.16), transparent 32%),
#         linear-gradient(180deg, #fbf7ef 0%, var(--bg) 100%);
#       font-family: "Segoe UI", "Helvetica Neue", sans-serif;
#       line-height: 1.5;
#     }
#     main {
#       width: min(1680px, calc(100vw - 64px));
#       margin: 0 auto;
#       padding: 36px 0 64px;
#     }
#     .hero, .toolbar-shell, .stat, .panel, .timeline-item, details.phase, .notice {
#       background: var(--card);
#       border: 1px solid var(--border);
#       border-radius: 20px;
#       box-shadow: var(--shadow);
#     }
#     .hero {
#       display: grid;
#       gap: 18px;
#       padding: 32px;
#       border-radius: 28px;
#       background: linear-gradient(135deg, rgba(255,255,255,0.74), rgba(255,248,236,0.92));
#     }
#     .eyebrow {
#       margin: 0;
#       font-size: 12px;
#       letter-spacing: 0.14em;
#       text-transform: uppercase;
#       color: var(--accent);
#     }
#     h1, h2, h3, p { margin: 0; }
#     h1 {
#       font-size: clamp(30px, 4vw, 48px);
#       line-height: 1.05;
#       max-width: 18ch;
#     }
#     .subhead {
#       color: var(--muted);
#       max-width: 80ch;
#     }
#     .toolbar-shell {
#       margin-top: 20px;
#       padding: 18px 20px;
#       display: flex;
#       flex-wrap: wrap;
#       align-items: end;
#       gap: 16px;
#     }
#     .toolbar-group {
#       display: grid;
#       gap: 8px;
#       min-width: 260px;
#       flex: 1 1 300px;
#     }
#     .toolbar-label {
#       color: var(--muted);
#       font-size: 12px;
#       text-transform: uppercase;
#       letter-spacing: 0.08em;
#     }
#     .toolbar-row {
#       display: flex;
#       flex-wrap: wrap;
#       gap: 12px;
#       align-items: center;
#     }
#     select, button {
#       appearance: none;
#       border: 1px solid var(--border);
#       background: rgba(255,255,255,0.8);
#       color: var(--ink);
#       border-radius: 14px;
#       padding: 12px 14px;
#       font: inherit;
#     }
#     select {
#       min-width: 340px;
#       max-width: 100%;
#       flex: 1 1 460px;
#     }
#     button {
#       cursor: pointer;
#       background: var(--accent);
#       color: white;
#       border-color: transparent;
#     }
#     button:disabled {
#       opacity: 0.6;
#       cursor: default;
#     }
#     .toolbar-meta {
#       color: var(--muted);
#       font-size: 14px;
#     }
#     .status-row, .card-grid, .panel-grid {
#       display: grid;
#       gap: 16px;
#     }
#     .status-row {
#       grid-template-columns: repeat(auto-fit, minmax(156px, 1fr));
#     }
#     .stat {
#       padding: 16px 18px;
#     }
#     .stat-label {
#       color: var(--muted);
#       font-size: 12px;
#       text-transform: uppercase;
#       letter-spacing: 0.08em;
#     }
#     .stat-value {
#       font-size: 24px;
#       font-weight: 700;
#       margin-top: 6px;
#     }
#     section {
#       margin-top: 24px;
#     }
#     .section-head {
#       display: flex;
#       justify-content: space-between;
#       align-items: end;
#       gap: 12px;
#       margin-bottom: 14px;
#     }
#     .section-head p {
#       color: var(--muted);
#       max-width: 72ch;
#     }
#     .controls {
#       display: flex;
#       flex-wrap: wrap;
#       gap: 10px;
#     }
#     .filter-pill {
#       display: inline-flex;
#       align-items: center;
#       gap: 8px;
#       padding: 9px 12px;
#       background: var(--card);
#       border: 1px solid var(--border);
#       border-radius: 999px;
#       font-size: 13px;
#     }
#     .filter-pill input {
#       accent-color: var(--accent);
#     }
#     .card-grid {
#       grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
#     }
#     details.phase {
#       overflow: hidden;
#     }
#     details.phase summary {
#       cursor: pointer;
#       list-style: none;
#       padding: 18px 20px;
#       display: flex;
#       justify-content: space-between;
#       align-items: center;
#       gap: 16px;
#     }
#     details.phase summary::-webkit-details-marker { display: none; }
#     .phase-meta {
#       display: flex;
#       flex-wrap: wrap;
#       gap: 10px;
#       color: var(--muted);
#       font-size: 13px;
#     }
#     .phase-body {
#       padding: 0 20px 20px;
#       border-top: 1px solid var(--border);
#       background: rgba(255,255,255,0.55);
#     }
#     .timeline {
#       display: grid;
#       gap: 12px;
#     }
#     .timeline-item {
#       padding: 16px 18px;
#       display: grid;
#       gap: 8px;
#     }
#     .timeline-top {
#       display: flex;
#       justify-content: space-between;
#       align-items: start;
#       gap: 12px;
#     }
#     .badge {
#       display: inline-flex;
#       align-items: center;
#       border-radius: 999px;
#       padding: 4px 10px;
#       font-size: 12px;
#       font-weight: 600;
#       background: var(--accent-soft);
#       color: var(--accent);
#     }
#     .badge.success { background: rgba(45, 106, 79, 0.12); color: var(--success); }
#     .badge.warn { background: rgba(188, 108, 37, 0.14); color: var(--warn); }
#     .badge.error { background: rgba(155, 34, 38, 0.14); color: var(--error); }
#     .muted { color: var(--muted); }
#     .panel-grid {
#       grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
#     }
#     .panel {
#       padding: 20px;
#     }
#     table {
#       width: 100%;
#       border-collapse: collapse;
#       font-size: 14px;
#     }
#     th, td {
#       text-align: left;
#       padding: 10px 0;
#       border-bottom: 1px solid var(--border);
#       vertical-align: top;
#     }
#     th {
#       color: var(--muted);
#       font-size: 12px;
#       text-transform: uppercase;
#       letter-spacing: 0.08em;
#     }
#     ul.clean {
#       list-style: none;
#       margin: 0;
#       padding: 0;
#       display: grid;
#       gap: 10px;
#     }
#     .notice {
#       padding: 14px 16px;
#     }
#     .hidden {
#       display: none !important;
#     }
#     code {
#       font-family: "SFMono-Regular", Consolas, monospace;
#       font-size: 0.95em;
#     }
#     @media (min-width: 1500px) {
#       .hero {
#         grid-template-columns: minmax(0, 1.1fr) minmax(780px, 1fr);
#         align-items: start;
#       }
#       .hero-copy {
#         padding-right: 12px;
#       }
#       .status-row {
#         grid-template-columns: repeat(6, minmax(0, 1fr));
#         align-self: end;
#       }
#       .card-grid {
#         grid-template-columns: repeat(4, minmax(0, 1fr));
#       }
#       .panel-grid {
#         grid-template-columns: repeat(2, minmax(0, 1fr));
#       }
#     }
#     @media (max-width: 720px) {
#       main { width: min(100vw - 20px, 1680px); }
#       .hero { padding: 22px; border-radius: 22px; }
#       .toolbar-shell { padding: 16px; }
#       select { min-width: 0; width: 100%; }
#       details.phase summary, .panel, .timeline-item { padding-left: 16px; padding-right: 16px; }
#     }
#   </style>
# </head>
# <body>
#   <main>
#     <section class="hero">
#       <div class="hero-copy">
#         <p class="eyebrow">OpenCode 会话浏览</p>
#         <div>
#           <h1 id="hero-title">加载会话中...</h1>
#           <p class="subhead" id="hero-subhead">服务会扫描 captures 目录，并按需补齐缺失的摘要与报告。</p>
#         </div>
#       </div>
#       <div class="status-row" id="overview-stats"></div>
#     </section>

#     <section class="toolbar-shell">
#       <div class="toolbar-group">
#         <div class="toolbar-label">会话选择</div>
#         <div class="toolbar-row">
#           <select id="session-select"></select>
#           <button id="refresh-button" type="button">刷新会话列表</button>
#         </div>
#       </div>
#       <div class="toolbar-group">
#         <div class="toolbar-label">当前会话</div>
#         <div class="toolbar-meta" id="session-meta">尚未加载会话。</div>
#       </div>
#     </section>

#     <section id="banner-section" class="hidden">
#       <div id="banner" class="notice"></div>
#     </section>

#     <section>
#       <div class="section-head">
#         <div>
#           <h2>阶段视图</h2>
#           <p>按最能解释这次运行的阶段分组，高噪声消息流只折叠进统计，不直接展开。</p>
#         </div>
#       </div>
#       <div class="card-grid" id="phases"></div>
#     </section>

#     <section>
#       <div class="section-head">
#         <div>
#           <h2>关键时间线</h2>
#           <p>这里只展示显著动作。可以用右侧分类筛选快速收窄视图。</p>
#         </div>
#         <div class="controls" id="filters"></div>
#       </div>
#       <div class="timeline" id="timeline"></div>
#     </section>

#     <section>
#       <div class="section-head">
#         <div>
#           <h2>工具与文件</h2>
#           <p>汇总这次运行调用了什么工具、改动了哪些文件。</p>
#         </div>
#       </div>
#       <div class="panel-grid">
#         <div class="panel">
#           <h3>工具调用</h3>
#           <div id="tools"></div>
#         </div>
#         <div class="panel">
#           <h3>修改文件</h3>
#           <div id="files"></div>
#         </div>
#       </div>
#     </section>

#     <section>
#       <div class="section-head">
#         <div>
#           <h2>噪声与异常</h2>
#           <p>这里展示被时间线主动折叠的事件，以及数据质量相关的提醒。</p>
#         </div>
#       </div>
#       <div class="panel-grid">
#         <div class="panel">
#           <h3>折叠事件统计</h3>
#           <div id="noise"></div>
#         </div>
#         <div class="panel">
#           <h3>异常提示</h3>
#           <div id="anomalies"></div>
#         </div>
#       </div>
#     </section>
#   </main>

#   <script>
#     const phaseLabels = {
#       session_start: "会话启动",
#       analysis: "分析空档",
#       tool_work: "工具执行",
#       file_change: "文件修改",
#       completion: "完成收尾",
#     };
#     const phaseDescriptions = {
#       session_start: "首次动作发生前的会话建立与状态变化。",
#       analysis: "会话启动后到首个关键动作之间，被折叠的消息噪声区间。",
#       tool_work: "推动任务前进的工具、权限与命令动作。",
#       file_change: "本次运行中发生的文件编辑。",
#       completion: "运行结束前后的终态事件与收尾动作。",
#     };
#     const categoryLabels = {
#       session: "会话",
#       tool: "工具",
#       permission: "权限",
#       command: "命令",
#       file: "文件",
#       system: "系统",
#     };
#     const statusLabels = {
#       success: "成功",
#       failed: "失败",
#     };
#     const levelLabels = {
#       info: "提示",
#       warning: "警告",
#       error: "错误",
#     };

#     let sessionCatalog = [];
#     let currentPayload = null;

#     function formatTimestamp(value) {
#       if (!value) return "无";
#       const date = new Date(value);
#       if (Number.isNaN(date.getTime())) return value;
#       return date.toLocaleString("zh-CN", { hour12: false });
#     }

#     function formatDuration(seconds) {
#       if (seconds === null || seconds === undefined) return "无";
#       if (seconds < 1) return `${seconds.toFixed(3)}s`;
#       if (seconds < 60) return `${seconds.toFixed(1)}s`;
#       const minutes = Math.floor(seconds / 60);
#       const remainder = seconds - minutes * 60;
#       return `${minutes}m ${remainder.toFixed(1)}s`;
#     }

#     function badgeClass(level) {
#       if (level === "success") return "badge success";
#       if (level === "error") return "badge error";
#       if (level === "warning") return "badge warn";
#       return "badge";
#     }

#     function labelForCategory(category) {
#       return categoryLabels[category] || category || "其他";
#     }

#     function labelForPhase(phase) {
#       return phaseLabels[phase] || phase;
#     }

#     function labelForStatus(status) {
#       return statusLabels[status] || status || "未知";
#     }

#     function labelForLevel(level) {
#       return levelLabels[level] || level || "提示";
#     }

#     function translateEventTitle(event) {
#       if (!event || !event.title) return "";
#       if (event.title === "Session created") return "会话已创建";
#       if (event.title === "Session idle") return "会话空闲";
#       if (event.title === "Session error") return "会话错误";
#       if (event.title.startsWith("Session ")) {
#         const status = event.title.slice("Session ".length);
#         const mapped = { busy: "忙碌", idle: "空闲", error: "错误", completed: "完成" };
#         return `会话状态：${mapped[status] || status}`;
#       }
#       if (event.title.startsWith("Tool started: ")) {
#         return `工具开始：${event.title.slice("Tool started: ".length)}`;
#       }
#       if (event.title.startsWith("Tool finished: ")) {
#         return `工具完成：${event.title.slice("Tool finished: ".length)}`;
#       }
#       if (event.title.startsWith("Edited ")) {
#         return `已编辑 ${event.title.slice("Edited ".length)}`;
#       }
#       if (event.title === "Permission requested") return "权限请求";
#       if (event.title === "Permission updated") return "权限结果";
#       if (event.title === "Command executed") return "命令执行";
#       if (event.title === "System event") return "系统事件";
#       return event.title;
#     }

#     async function fetchJson(url, options) {
#       const response = await fetch(url, options);
#       let payload = null;
#       try {
#         payload = await response.json();
#       } catch (error) {
#         payload = null;
#       }
#       if (!response.ok) {
#         const message = payload && payload.error ? payload.error : `请求失败: ${response.status}`;
#         throw new Error(message);
#       }
#       return payload;
#     }

#     function showBanner(message, level = "info") {
#       const section = document.getElementById("banner-section");
#       const banner = document.getElementById("banner");
#       if (!message) {
#         section.classList.add("hidden");
#         banner.textContent = "";
#         banner.className = "notice";
#         return;
#       }
#       section.classList.remove("hidden");
#       banner.className = `notice ${badgeClass(level)}`;
#       banner.textContent = message;
#     }

#     function sessionOptionLabel(session) {
#       const started = formatTimestamp(session.started_at);
#       return `${started} · ${session.issue_id || "unknown"} · ${labelForStatus(session.capture_status)}`;
#     }

#     function renderSessionOptions(selectedId) {
#       const select = document.getElementById("session-select");
#       select.innerHTML = sessionCatalog.map((session) => `
#         <option value="${session.capture_id}" ${session.capture_id === selectedId ? "selected" : ""}>
#           ${sessionOptionLabel(session)}
#         </option>
#       `).join("");
#       select.disabled = sessionCatalog.length === 0;
#     }

#     function renderOverview(summary, session, paths) {
#       document.getElementById("hero-title").textContent = summary.run.title || session.issue_id || session.capture_id;
#       document.getElementById("hero-subhead").textContent =
#         `问题 ${summary.run.issue_id} · 抓取状态 ${labelForStatus(summary.run.capture_status)} · 会话 ${summary.run.session_id || "缺失"}`;
#       document.getElementById("session-meta").textContent =
#         `capture: ${session.capture_id} · summary: ${paths.summary} · run: ${paths.run_metadata}`;

#       const items = [
#         ["状态", labelForStatus(summary.run.capture_status)],
#         ["时长", formatDuration(summary.run.duration_seconds)],
#         ["退出码", summary.run.exit_code ?? "无"],
#         ["事件总数", summary.run.event_count ?? 0],
#         ["关键时间线", summary.counts.significant_total],
#         ["折叠噪声", summary.counts.noise_total],
#       ];
#       document.getElementById("overview-stats").innerHTML = items.map(([label, value]) => `
#         <div class="stat">
#           <div class="stat-label">${label}</div>
#           <div class="stat-value">${value}</div>
#         </div>
#       `).join("");
#     }

#     function renderFilters(summary) {
#       const filters = new Set(summary.timeline.map((event) => event.category).filter(Boolean));
#       const ordered = Array.from(filters).sort();
#       document.getElementById("filters").innerHTML = ordered.map((category) => `
#         <label class="filter-pill">
#           <input type="checkbox" data-filter="${category}" checked>
#           <span>${labelForCategory(category)}</span>
#         </label>
#       `).join("");
#       document.getElementById("filters").onchange = () => renderTimeline(summary);
#     }

#     function activeCategories() {
#       const checked = document.querySelectorAll("#filters input:checked");
#       return new Set(Array.from(checked).map((input) => input.getAttribute("data-filter")));
#     }

#     function renderPhases(summary) {
#       const phaseEvents = new Map();
#       summary.timeline.forEach((event) => {
#         const existing = phaseEvents.get(event.phase) || [];
#         existing.push(event);
#         phaseEvents.set(event.phase, existing);
#       });
#       document.getElementById("phases").innerHTML = summary.phases.map((phase) => {
#         const events = phaseEvents.get(phase.name) || [];
#         const items = events.slice(0, 6).map((event) => `
#           <li>${formatTimestamp(event.timestamp)} · <strong>${translateEventTitle(event)}</strong>${event.detail ? ` <span class="muted">· ${event.detail}</span>` : ""}</li>
#         `).join("");
#         return `
#           <details class="phase" open>
#             <summary>
#               <div>
#                 <h3>${labelForPhase(phase.name)}</h3>
#                 <p class="muted">${phaseDescriptions[phase.name] || phase.description}</p>
#               </div>
#               <div class="phase-meta">
#                 <span>${formatDuration(phase.duration_seconds)}</span>
#                 <span>${phase.event_count} 个关键事件</span>
#                 <span>${phase.noise_count} 个已折叠</span>
#               </div>
#             </summary>
#             <div class="phase-body">
#               <p class="muted">${formatTimestamp(phase.started_at)} 至 ${formatTimestamp(phase.finished_at)}</p>
#               ${items ? `<ul class="clean">${items}</ul>` : '<p class="muted">这个阶段没有关键事件，时间范围来自被折叠的噪声活动。</p>'}
#             </div>
#           </details>
#         `;
#       }).join("");
#     }

#     function renderTimeline(summary) {
#       const active = activeCategories();
#       const events = summary.timeline.filter((event) => !event.category || active.has(event.category));
#       document.getElementById("timeline").innerHTML = events.map((event) => `
#         <article class="timeline-item">
#           <div class="timeline-top">
#             <div>
#               <div class="${badgeClass(event.phase === "completion" ? "success" : event.category === "permission" ? "warning" : "info")}">${labelForCategory(event.category)} · ${labelForPhase(event.phase)}</div>
#               <h3>${translateEventTitle(event)}</h3>
#             </div>
#             <div class="muted">${formatTimestamp(event.timestamp)}</div>
#           </div>
#           <div class="muted"><code>${event.event_type}</code></div>
#           ${event.detail ? `<div>${event.detail}</div>` : ""}
#           ${event.file_path ? `<div class="muted">文件：<code>${event.file_path}</code></div>` : ""}
#           ${event.tool_name ? `<div class="muted">工具：<code>${event.tool_name}</code></div>` : ""}
#         </article>
#       `).join("") || '<div class="timeline-item"><p class="muted">当前筛选条件下没有可展示的时间线事件。</p></div>';
#     }

#     function renderTools(summary) {
#       if (!summary.tools.length) {
#         document.getElementById("tools").innerHTML = '<p class="muted">这次运行没有捕获到工具调用。</p>';
#         return;
#       }
#       document.getElementById("tools").innerHTML = `
#         <table>
#           <thead>
#             <tr><th>工具</th><th>状态</th><th>时长</th><th>时间窗口</th></tr>
#           </thead>
#           <tbody>
#             ${summary.tools.map((tool) => `
#               <tr>
#                 <td><code>${tool.tool_name}</code></td>
#                 <td><span class="${badgeClass(tool.success ? "success" : "warning")}">${tool.success ? "完成" : "不完整"}</span></td>
#                 <td>${formatDuration(tool.duration_seconds)}</td>
#                 <td class="muted">${formatTimestamp(tool.started_at)} → ${formatTimestamp(tool.finished_at)}</td>
#               </tr>
#             `).join("")}
#           </tbody>
#         </table>
#       `;
#     }

#     function renderFiles(summary) {
#       if (!summary.files.length) {
#         document.getElementById("files").innerHTML = '<p class="muted">这次运行没有捕获到文件编辑。</p>';
#         return;
#       }
#       document.getElementById("files").innerHTML = `
#         <table>
#           <thead>
#             <tr><th>路径</th><th>编辑次数</th><th>时间窗口</th></tr>
#           </thead>
#           <tbody>
#             ${summary.files.map((file) => `
#               <tr>
#                 <td><code>${file.path}</code></td>
#                 <td>${file.edit_count}</td>
#                 <td class="muted">${formatTimestamp(file.first_edited_at)} → ${formatTimestamp(file.last_edited_at)}</td>
#               </tr>
#             `).join("")}
#           </tbody>
#         </table>
#       `;
#     }

#     function renderNoise(summary) {
#       const entries = Object.entries(summary.counts.noise_by_type);
#       if (!entries.length) {
#         document.getElementById("noise").innerHTML = '<p class="muted">没有需要折叠的噪声事件。</p>';
#         return;
#       }
#       document.getElementById("noise").innerHTML = `
#         <ul class="clean">
#           ${entries.map(([eventType, count]) => `<li class="notice"><strong>${count}</strong> <code>${eventType}</code></li>`).join("")}
#         </ul>
#       `;
#     }

#     function renderAnomalies(summary) {
#       if (!summary.anomalies.length) {
#         document.getElementById("anomalies").innerHTML = '<p class="muted">没有检测到异常。</p>';
#         return;
#       }
#       document.getElementById("anomalies").innerHTML = `
#         <ul class="clean">
#           ${summary.anomalies.map((item) => `
#             <li class="notice">
#               <div class="${badgeClass(item.level)}">${labelForLevel(item.level)}</div>
#               <p>${item.message}</p>
#             </li>
#           `).join("")}
#         </ul>
#       `;
#     }

#     function renderUnavailable(payload) {
#       const session = payload.session || {};
#       const run = payload.run || {};
#       document.getElementById("hero-title").textContent = run.issue_title || session.title || session.capture_id || "会话不可用";
#       document.getElementById("hero-subhead").textContent = "这个会话当前没有可用的 summary，可能是补齐失败或数据损坏。";
#       document.getElementById("session-meta").textContent = `capture: ${session.capture_id || "未知"} · ${payload.error || "缺少 summary"}`;
#       document.getElementById("overview-stats").innerHTML = "";
#       document.getElementById("phases").innerHTML = '<div class="notice"><p class="muted">当前会话没有可展示的阶段摘要。</p></div>';
#       document.getElementById("filters").innerHTML = "";
#       document.getElementById("timeline").innerHTML = '<div class="timeline-item"><p class="muted">当前会话没有可展示的时间线。</p></div>';
#       document.getElementById("tools").innerHTML = '<p class="muted">当前会话没有工具摘要。</p>';
#       document.getElementById("files").innerHTML = '<p class="muted">当前会话没有文件摘要。</p>';
#       document.getElementById("noise").innerHTML = '<p class="muted">当前会话没有噪声统计。</p>';
#       document.getElementById("anomalies").innerHTML = `<div class="notice"><p>${payload.error || "summary 不可用"}</p></div>`;
#     }

#     async function loadSession(captureId) {
#       const payload = await fetchJson(`/api/sessions/${encodeURIComponent(captureId)}`);
#       currentPayload = payload;
#       const session = payload.session || {};
#       if (!payload.summary) {
#         showBanner(payload.error || "这个会话没有可用的 summary。", "warning");
#         renderUnavailable(payload);
#         return;
#       }
#       showBanner(payload.error || "", payload.error ? "warning" : "info");
#       renderOverview(payload.summary, session, payload.paths || {});
#       renderFilters(payload.summary);
#       renderPhases(payload.summary);
#       renderTimeline(payload.summary);
#       renderTools(payload.summary);
#       renderFiles(payload.summary);
#       renderNoise(payload.summary);
#       renderAnomalies(payload.summary);
#     }

#     async function loadSessionList(preferredId) {
#       const sessions = await fetchJson("/api/sessions");
#       sessionCatalog = Array.isArray(sessions.sessions) ? sessions.sessions : [];
#       if (!sessionCatalog.length) {
#         renderSessionOptions("");
#         showBanner("当前 captures 目录下没有可展示的会话。", "info");
#         renderUnavailable({ session: {}, run: {}, error: "没有会话" });
#         return;
#       }
#       const selected = sessionCatalog.some((session) => session.capture_id === preferredId)
#         ? preferredId
#         : sessionCatalog[0].capture_id;
#       renderSessionOptions(selected);
#       await loadSession(selected);
#     }

#     async function refreshSessions() {
#       const button = document.getElementById("refresh-button");
#       const select = document.getElementById("session-select");
#       const currentId = select.value;
#       button.disabled = true;
#       try {
#         const payload = await fetchJson("/api/refresh", { method: "POST" });
#         sessionCatalog = Array.isArray(payload.sessions) ? payload.sessions : [];
#         const selected = sessionCatalog.some((session) => session.capture_id === currentId)
#           ? currentId
#           : (sessionCatalog[0] && sessionCatalog[0].capture_id);
#         renderSessionOptions(selected || "");
#         if (selected) {
#           await loadSession(selected);
#         } else {
#           renderUnavailable({ session: {}, run: {}, error: "没有会话" });
#         }
#         showBanner("会话列表已刷新。", "success");
#       } catch (error) {
#         showBanner(error.message, "error");
#       } finally {
#         button.disabled = false;
#       }
#     }

#     document.getElementById("session-select").addEventListener("change", async (event) => {
#       try {
#         await loadSession(event.target.value);
#       } catch (error) {
#         showBanner(error.message, "error");
#       }
#     });

#     document.getElementById("refresh-button").addEventListener("click", async () => {
#       await refreshSessions();
#     });

#     loadSessionList().catch((error) => {
#       showBanner(error.message, "error");
#       renderUnavailable({ session: {}, run: {}, error: error.message });
#     });
#   </script>
# </body>
# </html>
# """


def _browser_app_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenCode 会话浏览</title>
  <style>
    :root {
      --bg: #f6f1e8;
      --card: rgba(255, 252, 246, 0.92);
      --ink: #1f1d1a;
      --muted: #645e56;
      --accent: #005f73;
      --accent-hover: #004d5c;
      --accent-soft: rgba(0, 95, 115, 0.12);
      --success: #2d6a4f;
      --warn: #bc6c25;
      --error: #9b2226;
      --border: rgba(31, 29, 26, 0.12);
      --border-hover: rgba(31, 29, 26, 0.25);
      --shadow-sm: 0 4px 12px rgba(67, 56, 42, 0.06);
      --shadow: 0 18px 50px rgba(67, 56, 42, 0.1);
      --shadow-hover: 0 24px 60px rgba(67, 56, 42, 0.15);
    }
    * { box-sizing: border-box; }
    
    /* 滚动条美化 */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: rgba(31, 29, 26, 0.15); border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: rgba(31, 29, 26, 0.3); }

    body {
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(233, 196, 106, 0.22), transparent 35%),
        radial-gradient(circle at top right, rgba(0, 95, 115, 0.16), transparent 32%),
        linear-gradient(180deg, #fbf7ef 0%, var(--bg) 100%);
      background-attachment: fixed;
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
      line-height: 1.6;
    }
    main {
      width: min(1680px, calc(100vw - 64px));
      margin: 0 auto;
      padding: 40px 0 80px;
    }
    
    /* 通用卡片样式与动画 */
    .hero, .toolbar-shell, .stat, .panel, .timeline-item, details.phase, .notice {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: var(--shadow-sm);
      transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
    }
    .panel:hover, details.phase:hover {
      box-shadow: var(--shadow);
      border-color: var(--border-hover);
    }
    
    .hero {
      display: grid;
      gap: 24px;
      padding: 40px;
      border-radius: 24px;
      background: linear-gradient(135deg, rgba(255,255,255,0.85), rgba(255,248,236,0.95));
      box-shadow: var(--shadow);
    }
    .eyebrow {
      margin: 0 0 12px 0;
      font-size: 13px;
      font-weight: 600;
      letter-spacing: 0.15em;
      text-transform: uppercase;
      color: var(--accent);
    }
    h1, h2, h3, p { margin: 0; }
    h1 {
      font-size: clamp(32px, 4vw, 48px);
      line-height: 1.1;
      font-weight: 800;
      letter-spacing: -0.02em;
    }
    .subhead {
      color: var(--muted);
      max-width: 80ch;
      margin-top: 12px;
      font-size: 1.1rem;
    }
    
    /* 工具栏重构 */
    .toolbar-shell {
      margin-top: 24px;
      padding: 20px 24px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
    }
    .toolbar-group {
      display: flex;
      flex-direction: column;
      gap: 8px;
      flex: 1 1 auto;
    }
    .toolbar-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.1em;
    }
    .toolbar-row {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
    }
    
    /* 表单控件精修 */
    select, button {
      appearance: none;
      -webkit-appearance: none;
      font: inherit;
      font-size: 14px;
      font-weight: 500;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 16px;
      transition: all 0.2s ease;
      outline: none;
    }
    select {
      background: rgba(255,255,255,0.9) url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%23645e56' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E") no-repeat right 14px center;
      color: var(--ink);
      min-width: 340px;
      padding-right: 40px;
      cursor: pointer;
    }
    select:hover {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-soft);
    }
    select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-soft);
    }
    
    button {
      cursor: pointer;
      background: var(--accent);
      color: white;
      border-color: transparent;
      box-shadow: 0 2px 4px rgba(0, 95, 115, 0.2);
    }
    button:hover:not(:disabled) {
      background: var(--accent-hover);
      transform: translateY(-1px);
      box-shadow: 0 4px 8px rgba(0, 95, 115, 0.3);
    }
    button:active:not(:disabled) {
      transform: translateY(0);
    }
    button:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      box-shadow: none;
    }
    
    .toolbar-meta {
      color: var(--muted);
      font-size: 13px;
      background: rgba(0,0,0,0.03);
      padding: 8px 12px;
      border-radius: 8px;
      display: inline-block;
    }
    
    /* 数据卡片 */
    .status-row, .card-grid, .panel-grid {
      display: grid;
      gap: 20px;
    }
    .status-row {
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    }
    .stat {
      padding: 20px;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
    .stat-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.1em;
    }
    .stat-value {
      font-size: 28px;
      font-weight: 800;
      margin-top: 8px;
      color: var(--ink);
      letter-spacing: -0.02em;
    }
    
    /* 章节头部 */
    section { margin-top: 40px; }
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      gap: 16px;
      margin-bottom: 20px;
      padding-bottom: 12px;
      border-bottom: 1px solid rgba(0,0,0,0.06);
    }
    .section-head h2 {
      font-size: 20px;
      font-weight: 700;
      margin-bottom: 4px;
    }
    .section-head p {
      color: var(--muted);
      font-size: 14px;
      max-width: 72ch;
    }
    
    /* 过滤器药丸 */
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .filter-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      background: rgba(255,255,255,0.6);
      border: 1px solid var(--border);
      border-radius: 999px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.2s;
      user-select: none;
    }
    .filter-pill:hover {
      background: rgba(255,255,255,0.9);
      border-color: var(--accent);
    }
    .filter-pill input {
      accent-color: var(--accent);
      width: 14px; height: 14px;
      cursor: pointer;
    }
    
    /* 阶段折叠面板 */
    .card-grid {
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      align-items: stretch;
    }
    details.phase {
      overflow: hidden;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      min-height: 520px;
      height: 520px;
    }
    details.phase summary {
      cursor: pointer;
      list-style: none;
      padding: 20px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: start;
      gap: 16px;
      user-select: none;
      transition: background 0.2s;
      min-height: 116px;
    }
    details.phase summary:hover {
      background: rgba(0,0,0,0.015);
    }
    details.phase summary::-webkit-details-marker { display: none; }
    
    /* 自定义右侧指示箭头 */
    details.phase summary::after {
      content: '';
      flex-shrink: 0;
      width: 20px; height: 20px;
      background: url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23645e56' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E") no-repeat center;
      transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    details.phase[open] summary::after {
      transform: rotate(180deg);
    }
    .phase-summary-main {
      display: grid;
      gap: 10px;
      min-height: 76px;
      align-content: start;
    }
    .phase-summary-copy {
      min-height: 48px;
      display: grid;
      align-content: start;
      gap: 6px;
    }
    .phase-summary-title {
      font-size: 15px;
      color: #36302a;
      line-height: 1.45;
    }
    .phase-meta {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      align-content: start;
      gap: 12px;
      color: #4d453d;
      font-size: 13px;
      line-height: 1.45;
      min-width: 118px;
    }
    .phase-body {
      padding: 0 20px 12px;
      border-top: 1px solid var(--border);
      background: rgba(255,255,255,0.4);
      animation: slideDown 0.3s ease-out;
      display: flex;
      flex-direction: column;
      min-height: 0;
      height: 100%;
    }
    .phase-window {
      margin-bottom: 12px;
      font-size: 12px;
      color: #6f665d;
    }
    .phase-event-frame {
      flex: 1 1 auto;
      min-height: 0;
      width: 100%;
      background: rgba(255,255,255,0.62);
      border: 1px solid rgba(31, 29, 26, 0.08);
      border-radius: 16px;
      overflow: hidden;
    }
    .phase-event-list {
      height: 100%;
      min-height: 0;
      overflow-y: auto;
      overflow-x: hidden;
      padding: 10px 12px 8px 12px;
      scrollbar-width: thin;
      scrollbar-color: rgba(100, 94, 86, 0.35) transparent;
      scrollbar-gutter: stable;
      overscroll-behavior: contain;
    }
    .phase-event-list::-webkit-scrollbar {
      width: 8px;
    }
    .phase-event-list::-webkit-scrollbar-thumb {
      background: rgba(100, 94, 86, 0.35);
      border-radius: 999px;
    }
    .phase-event-list::-webkit-scrollbar-track {
      background: transparent;
    }
    .phase-event-list ul.clean {
      gap: 8px;
    }
    .phase-event-item {
      padding: 8px 10px;
      border-radius: 12px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(31, 29, 26, 0.06);
      color: #2f2a25;
      line-height: 1.55;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .phase-event-time {
      color: #5f564e;
      margin-right: 4px;
      white-space: normal;
    }
    .phase-event-text {
      color: #201d1a;
      font-weight: 600;
    }
    .phase-event-detail {
      color: #4c443d;
    }
    @keyframes slideDown {
      from { opacity: 0; transform: translateY(-4px); }
      to { opacity: 1; transform: translateY(0); }
    }
    
    /* 时间线视觉强化 */
    .timeline {
      display: flex;
      flex-direction: column;
      gap: 16px;
      position: relative;
      padding-left: 12px;
    }
    /* 时间线主轴 */
    .timeline::before {
      content: '';
      position: absolute;
      top: 10px; left: 24px; bottom: 10px;
      width: 2px;
      background: var(--border);
      border-radius: 2px;
    }
    .timeline-item {
      padding: 20px;
      margin-left: 36px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      position: relative;
    }
    /* 时间线节点圆点 */
    .timeline-item::before {
      content: '';
      position: absolute;
      left: -29px;
      top: 28px;
      width: 12px; height: 12px;
      border-radius: 50%;
      background: var(--bg);
      border: 3px solid var(--accent);
      box-shadow: 0 0 0 3px var(--bg);
      z-index: 1;
      transition: transform 0.2s ease, background 0.2s ease;
    }
    .timeline-item:hover::before {
      transform: scale(1.2);
      background: var(--accent);
    }
    .timeline-item:hover {
      transform: translateX(4px);
    }
    
    .timeline-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
    }
    
    /* 徽标 */
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 6px;
      padding: 4px 8px;
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.05em;
      background: var(--accent-soft);
      color: var(--accent);
      border: 1px solid rgba(0, 95, 115, 0.1);
    }
    .badge.success { background: rgba(45, 106, 79, 0.1); color: var(--success); border-color: rgba(45, 106, 79, 0.2); }
    .badge.warn { background: rgba(188, 108, 37, 0.1); color: var(--warn); border-color: rgba(188, 108, 37, 0.2); }
    .badge.error { background: rgba(155, 34, 38, 0.1); color: var(--error); border-color: rgba(155, 34, 38, 0.2); }
    
    .muted { color: var(--muted); }
    
    /* 数据面板与表格 */
    .panel-grid {
      grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
    }
    .panel {
      padding: 24px;
      overflow-x: auto;
    }
    .panel h3 { margin-bottom: 16px; font-size: 16px; }
    
    table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      font-size: 14px;
    }
    th, td {
      text-align: left;
      padding: 12px 16px;
      border-bottom: 1px solid var(--border);
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      background: rgba(0,0,0,0.02);
    }
    th:first-child { border-top-left-radius: 8px; }
    th:last-child { border-top-right-radius: 8px; }
    tbody tr {
      transition: background 0.15s ease;
    }
    tbody tr:hover {
      background: rgba(0, 95, 115, 0.03);
    }
    tbody tr:last-child td { border-bottom: none; }
    
    ul.clean {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 12px;
    }
    .notice {
      padding: 16px 20px;
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }
    .hidden { display: none !important; }
    
    code {
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", monospace;
      font-size: 0.9em;
      background: rgba(0,0,0,0.04);
      padding: 2px 6px;
      border-radius: 4px;
      color: #3b362f;
    }
    
    /* 响应式调整 */
    @media (min-width: 1500px) {
      .hero {
        grid-template-columns: minmax(0, 1.2fr) minmax(800px, 1fr);
        align-items: center;
      }
      .status-row {
        grid-template-columns: repeat(3, 1fr);
        grid-auto-rows: 1fr;
      }
      .card-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
      .panel-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 768px) {
      main { width: min(100vw - 32px, 1680px); padding: 24px 0; }
      .hero { grid-template-columns: 1fr; padding: 24px; border-radius: 20px; }
      .toolbar-shell { flex-direction: column; align-items: stretch; padding: 16px; }
      select { min-width: 0; width: 100%; }
      .timeline::before { left: 16px; }
      .timeline-item { margin-left: 20px; padding: 16px; }
      .timeline-item::before { left: -21px; width: 10px; height: 10px; }
    }
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">OpenCode 会话浏览</p>
        <div>
          <h1 id="hero-title">加载会话中...</h1>
          <p class="subhead" id="hero-subhead">服务会扫描 captures 目录，并按需补齐缺失的摘要与报告。</p>
        </div>
      </div>
      <div class="status-row" id="overview-stats"></div>
    </section>

    <section class="toolbar-shell">
      <div class="toolbar-group">
        <div class="toolbar-label">会话选择</div>
        <div class="toolbar-row">
          <select id="session-select"></select>
          <button id="refresh-button" type="button">刷新列表</button>
        </div>
      </div>
      <div class="toolbar-group" style="align-items: flex-end; text-align: right;">
        <div class="toolbar-label">当前定位</div>
        <div class="toolbar-meta" id="session-meta">尚未加载会话。</div>
      </div>
    </section>

    <section id="banner-section" class="hidden">
      <div id="banner" class="notice"></div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>阶段视图</h2>
          <p>按最能解释这次运行的阶段分组，高噪声消息流只折叠进统计，不直接展开。</p>
        </div>
      </div>
      <div class="card-grid" id="phases"></div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>关键时间线</h2>
          <p>这里只展示显著动作。可以用右侧分类筛选快速收窄视图。</p>
        </div>
        <div class="controls" id="filters"></div>
      </div>
      <div class="timeline" id="timeline"></div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>工具与文件</h2>
          <p>汇总这次运行调用了什么工具、改动了哪些文件。</p>
        </div>
      </div>
      <div class="panel-grid">
        <div class="panel">
          <h3>工具调用</h3>
          <div id="tools"></div>
        </div>
        <div class="panel">
          <h3>修改文件</h3>
          <div id="files"></div>
        </div>
      </div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>噪声与异常</h2>
          <p>这里展示被时间线主动折叠的事件，以及数据质量相关的提醒。</p>
        </div>
      </div>
      <div class="panel-grid">
        <div class="panel">
          <h3>折叠事件统计</h3>
          <div id="noise"></div>
        </div>
        <div class="panel">
          <h3>异常提示</h3>
          <div id="anomalies"></div>
        </div>
      </div>
    </section>
  </main>

  <script>
    const phaseLabels = {
      session_start: "会话启动",
      analysis: "分析空档",
      tool_work: "工具执行",
      file_change: "文件修改",
      completion: "完成收尾",
    };
    const phaseDescriptions = {
      session_start: "首次动作发生前的会话建立与状态变化。",
      analysis: "会话启动后到首个关键动作之间，被折叠的消息噪声区间。",
      tool_work: "推动任务前进的工具、权限与命令动作。",
      file_change: "本次运行中发生的文件编辑。",
      completion: "运行结束前后的终态事件与收尾动作。",
    };
    const categoryLabels = {
      session: "会话",
      tool: "工具",
      permission: "权限",
      command: "命令",
      file: "文件",
      system: "系统",
    };
    const statusLabels = {
      success: "成功",
      failed: "失败",
    };
    const levelLabels = {
      info: "提示",
      warning: "警告",
      error: "错误",
    };

    let sessionCatalog = [];
    let currentPayload = null;

    function formatTimestamp(value) {
      if (!value) return "无";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString("zh-CN", { hour12: false });
    }

    function formatDuration(seconds) {
      if (seconds === null || seconds === undefined) return "无";
      if (seconds < 1) return `${seconds.toFixed(3)}s`;
      if (seconds < 60) return `${seconds.toFixed(1)}s`;
      const minutes = Math.floor(seconds / 60);
      const remainder = seconds - minutes * 60;
      return `${minutes}m ${remainder.toFixed(1)}s`;
    }

    function badgeClass(level) {
      if (level === "success") return "badge success";
      if (level === "error") return "badge error";
      if (level === "warning") return "badge warn";
      return "badge";
    }

    function labelForCategory(category) {
      return categoryLabels[category] || category || "其他";
    }

    function labelForPhase(phase) {
      return phaseLabels[phase] || phase;
    }

    function labelForStatus(status) {
      return statusLabels[status] || status || "未知";
    }

    function labelForLevel(level) {
      return levelLabels[level] || level || "提示";
    }

    function translateEventTitle(event) {
      if (!event || !event.title) return "";
      if (event.title === "Session created") return "会话已创建";
      if (event.title === "Session idle") return "会话空闲";
      if (event.title === "Session error") return "会话错误";
      if (event.title.startsWith("Session ")) {
        const status = event.title.slice("Session ".length);
        const mapped = { busy: "忙碌", idle: "空闲", error: "错误", completed: "完成" };
        return `会话状态：${mapped[status] || status}`;
      }
      if (event.title.startsWith("Tool started: ")) {
        return `工具开始：${event.title.slice("Tool started: ".length)}`;
      }
      if (event.title.startsWith("Tool finished: ")) {
        return `工具完成：${event.title.slice("Tool finished: ".length)}`;
      }
      if (event.title.startsWith("Edited ")) {
        return `已编辑 ${event.title.slice("Edited ".length)}`;
      }
      if (event.title === "Permission requested") return "权限请求";
      if (event.title === "Permission updated") return "权限结果";
      if (event.title === "Command executed") return "命令执行";
      if (event.title === "System event") return "系统事件";
      return event.title;
    }

    async function fetchJson(url, options) {
      const response = await fetch(url, options);
      let payload = null;
      try {
        payload = await response.json();
      } catch (error) {
        payload = null;
      }
      if (!response.ok) {
        const message = payload && payload.error ? payload.error : `请求失败: ${response.status}`;
        throw new Error(message);
      }
      return payload;
    }

    function showBanner(message, level = "info") {
      const section = document.getElementById("banner-section");
      const banner = document.getElementById("banner");
      if (!message) {
        section.classList.add("hidden");
        banner.textContent = "";
        banner.className = "notice";
        return;
      }
      section.classList.remove("hidden");
      banner.className = `notice ${badgeClass(level)}`;
      banner.textContent = message;
    }

    function sessionOptionLabel(session) {
      const started = formatTimestamp(session.started_at);
      return `${started} · ${session.issue_id || "unknown"} · ${labelForStatus(session.capture_status)}`;
    }

    function renderSessionOptions(selectedId) {
      const select = document.getElementById("session-select");
      select.innerHTML = sessionCatalog.map((session) => `
        <option value="${session.capture_id}" ${session.capture_id === selectedId ? "selected" : ""}>
          ${sessionOptionLabel(session)}
        </option>
      `).join("");
      select.disabled = sessionCatalog.length === 0;
    }

    function renderOverview(summary, session, paths) {
      document.getElementById("hero-title").textContent = summary.run.title || session.issue_id || session.capture_id;
      document.getElementById("hero-subhead").textContent =
        `问题 ${summary.run.issue_id} · 抓取状态 ${labelForStatus(summary.run.capture_status)} · 会话 ${summary.run.session_id || "缺失"}`;
      document.getElementById("session-meta").innerHTML =
        `capture: <code>${session.capture_id}</code><br>summary: <code>${paths.summary}</code>`;

      const items = [
        ["状态", labelForStatus(summary.run.capture_status)],
        ["时长", formatDuration(summary.run.duration_seconds)],
        ["退出码", summary.run.exit_code ?? "无"],
        ["事件总数", summary.run.event_count ?? 0],
        ["关键时间线", summary.counts.significant_total],
        ["折叠噪声", summary.counts.noise_total],
      ];
      document.getElementById("overview-stats").innerHTML = items.map(([label, value]) => `
        <div class="stat">
          <div class="stat-label">${label}</div>
          <div class="stat-value">${value}</div>
        </div>
      `).join("");
    }

    function renderFilters(summary) {
      const filters = new Set(summary.timeline.map((event) => event.category).filter(Boolean));
      const ordered = Array.from(filters).sort();
      document.getElementById("filters").innerHTML = ordered.map((category) => `
        <label class="filter-pill">
          <input type="checkbox" data-filter="${category}" checked>
          <span>${labelForCategory(category)}</span>
        </label>
      `).join("");
      document.getElementById("filters").onchange = () => renderTimeline(summary);
    }

    function activeCategories() {
      const checked = document.querySelectorAll("#filters input:checked");
      return new Set(Array.from(checked).map((input) => input.getAttribute("data-filter")));
    }

    function renderPhases(summary) {
      const phaseEvents = new Map();
      summary.timeline.forEach((event) => {
        const existing = phaseEvents.get(event.phase) || [];
        existing.push(event);
        phaseEvents.set(event.phase, existing);
      });
      document.getElementById("phases").innerHTML = summary.phases.map((phase) => {
        const events = phaseEvents.get(phase.name) || [];
        const items = events.map((event) => `
          <li class="phase-event-item">
            <span class="phase-event-time">${formatTimestamp(event.timestamp)} ·</span>
            <span class="phase-event-text">${translateEventTitle(event)}</span>
            ${event.detail ? `<span class="phase-event-detail"> · ${event.detail}</span>` : ""}
          </li>
        `).join("");
        return `
          <details class="phase" open>
            <summary>
              <div class="phase-summary-main">
                <div class="phase-summary-copy">
                  <h3>${labelForPhase(phase.name)}</h3>
                  <p class="phase-summary-title">${phaseDescriptions[phase.name] || phase.description}</p>
                </div>
              </div>
              <div class="phase-meta">
                <span>${formatDuration(phase.duration_seconds)}</span>
                <span>${phase.event_count} 关键</span>
                <span>${phase.noise_count} 折叠</span>
              </div>
            </summary>
            <div class="phase-body">
              <p class="phase-window">${formatTimestamp(phase.started_at)} 至 ${formatTimestamp(phase.finished_at)}</p>
              <div class="phase-event-frame">
                <div class="phase-event-list">
                  ${items ? `<ul class="clean">${items}</ul>` : '<p class="muted">这个阶段没有关键事件，时间范围来自被折叠的噪声活动。</p>'}
                </div>
              </div>
            </div>
          </details>
        `;
      }).join("");
    }

    function renderTimeline(summary) {
      const active = activeCategories();
      const events = summary.timeline.filter((event) => !event.category || active.has(event.category));
      document.getElementById("timeline").innerHTML = events.map((event) => `
        <article class="timeline-item">
          <div class="timeline-top">
            <div>
              <div class="${badgeClass(event.phase === "completion" ? "success" : event.category === "permission" ? "warning" : "info")}">${labelForCategory(event.category)} · ${labelForPhase(event.phase)}</div>
              <h3 style="margin-top: 8px;">${translateEventTitle(event)}</h3>
            </div>
            <div class="muted" style="font-size: 13px;">${formatTimestamp(event.timestamp)}</div>
          </div>
          <div class="muted" style="margin-top: 4px;">类型: <code>${event.event_type}</code></div>
          ${event.detail ? `<div style="margin-top: 6px;">${event.detail}</div>` : ""}
          ${event.file_path ? `<div class="muted" style="margin-top: 6px;">文件: <code>${event.file_path}</code></div>` : ""}
          ${event.tool_name ? `<div class="muted" style="margin-top: 6px;">工具: <code>${event.tool_name}</code></div>` : ""}
        </article>
      `).join("") || '<div class="timeline-item"><p class="muted">当前筛选条件下没有可展示的时间线事件。</p></div>';
    }

    function renderTools(summary) {
      if (!summary.tools.length) {
        document.getElementById("tools").innerHTML = '<p class="muted">这次运行没有捕获到工具调用。</p>';
        return;
      }
      document.getElementById("tools").innerHTML = `
        <table>
          <thead>
            <tr><th>工具</th><th>状态</th><th>时长</th><th>时间窗口</th></tr>
          </thead>
          <tbody>
            ${summary.tools.map((tool) => `
              <tr>
                <td><code>${tool.tool_name}</code></td>
                <td><span class="${badgeClass(tool.success ? "success" : "warning")}">${tool.success ? "完成" : "不完整"}</span></td>
                <td>${formatDuration(tool.duration_seconds)}</td>
                <td class="muted">${formatTimestamp(tool.started_at)}<br>至 ${formatTimestamp(tool.finished_at)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    }

    function renderFiles(summary) {
      if (!summary.files.length) {
        document.getElementById("files").innerHTML = '<p class="muted">这次运行没有捕获到文件编辑。</p>';
        return;
      }
      document.getElementById("files").innerHTML = `
        <table>
          <thead>
            <tr><th>路径</th><th>编辑次数</th><th>时间窗口</th></tr>
          </thead>
          <tbody>
            ${summary.files.map((file) => `
              <tr>
                <td><code>${file.path}</code></td>
                <td>${file.edit_count} 次</td>
                <td class="muted">${formatTimestamp(file.first_edited_at)}<br>至 ${formatTimestamp(file.last_edited_at)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    }

    function renderNoise(summary) {
      const entries = Object.entries(summary.counts.noise_by_type);
      if (!entries.length) {
        document.getElementById("noise").innerHTML = '<p class="muted">没有需要折叠的噪声事件。</p>';
        return;
      }
      document.getElementById("noise").innerHTML = `
        <ul class="clean">
          ${entries.map(([eventType, count]) => `<li class="notice"><strong>${count} 次</strong> <code>${eventType}</code></li>`).join("")}
        </ul>
      `;
    }

    function renderAnomalies(summary) {
      if (!summary.anomalies.length) {
        document.getElementById("anomalies").innerHTML = '<p class="muted">没有检测到异常。</p>';
        return;
      }
      document.getElementById("anomalies").innerHTML = `
        <ul class="clean">
          ${summary.anomalies.map((item) => `
            <li class="notice">
              <div class="${badgeClass(item.level)}">${labelForLevel(item.level)}</div>
              <p>${item.message}</p>
            </li>
          `).join("")}
        </ul>
      `;
    }

    function renderUnavailable(payload) {
      const session = payload.session || {};
      const run = payload.run || {};
      document.getElementById("hero-title").textContent = run.issue_title || session.title || session.capture_id || "会话不可用";
      document.getElementById("hero-subhead").textContent = "这个会话当前没有可用的 summary，可能是补齐失败或数据损坏。";
      document.getElementById("session-meta").textContent = `capture: ${session.capture_id || "未知"} · ${payload.error || "缺少 summary"}`;
      document.getElementById("overview-stats").innerHTML = "";
      document.getElementById("phases").innerHTML = '<div class="notice"><p class="muted">当前会话没有可展示的阶段摘要。</p></div>';
      document.getElementById("filters").innerHTML = "";
      document.getElementById("timeline").innerHTML = '<div class="timeline-item"><p class="muted">当前会话没有可展示的时间线。</p></div>';
      document.getElementById("tools").innerHTML = '<p class="muted">当前会话没有工具摘要。</p>';
      document.getElementById("files").innerHTML = '<p class="muted">当前会话没有文件摘要。</p>';
      document.getElementById("noise").innerHTML = '<p class="muted">当前会话没有噪声统计。</p>';
      document.getElementById("anomalies").innerHTML = `<div class="notice"><p>${payload.error || "summary 不可用"}</p></div>`;
    }

    async function loadSession(captureId) {
      const payload = await fetchJson(`/api/sessions/${encodeURIComponent(captureId)}`);
      currentPayload = payload;
      const session = payload.session || {};
      if (!payload.summary) {
        showBanner(payload.error || "这个会话没有可用的 summary。", "warning");
        renderUnavailable(payload);
        return;
      }
      showBanner(payload.error || "", payload.error ? "warning" : "info");
      renderOverview(payload.summary, session, payload.paths || {});
      renderFilters(payload.summary);
      renderPhases(payload.summary);
      renderTimeline(payload.summary);
      renderTools(payload.summary);
      renderFiles(payload.summary);
      renderNoise(payload.summary);
      renderAnomalies(payload.summary);
    }

    async function loadSessionList(preferredId) {
      const sessions = await fetchJson("/api/sessions");
      sessionCatalog = Array.isArray(sessions.sessions) ? sessions.sessions : [];
      if (!sessionCatalog.length) {
        renderSessionOptions("");
        showBanner("当前 captures 目录下没有可展示的会话。", "info");
        renderUnavailable({ session: {}, run: {}, error: "没有会话" });
        return;
      }
      const selected = sessionCatalog.some((session) => session.capture_id === preferredId)
        ? preferredId
        : sessionCatalog[0].capture_id;
      renderSessionOptions(selected);
      await loadSession(selected);
    }

    async function refreshSessions() {
      const button = document.getElementById("refresh-button");
      const select = document.getElementById("session-select");
      const currentId = select.value;
      const originalText = button.textContent;
      button.disabled = true;
      button.textContent = "刷新中...";
      try {
        const payload = await fetchJson("/api/refresh", { method: "POST" });
        sessionCatalog = Array.isArray(payload.sessions) ? payload.sessions : [];
        const selected = sessionCatalog.some((session) => session.capture_id === currentId)
          ? currentId
          : (sessionCatalog[0] && sessionCatalog[0].capture_id);
        renderSessionOptions(selected || "");
        if (selected) {
          await loadSession(selected);
        } else {
          renderUnavailable({ session: {}, run: {}, error: "没有会话" });
        }
        showBanner("会话列表已刷新。", "success");
      } catch (error) {
        showBanner(error.message, "error");
      } finally {
        button.disabled = false;
        button.textContent = originalText;
      }
    }

    document.getElementById("session-select").addEventListener("change", async (event) => {
      try {
        await loadSession(event.target.value);
      } catch (error) {
        showBanner(error.message, "error");
      }
    });

    document.getElementById("refresh-button").addEventListener("click", async () => {
      await refreshSessions();
    });

    loadSessionList().catch((error) => {
      showBanner(error.message, "error");
      renderUnavailable({ session: {}, run: {}, error: error.message });
    });
  </script>
</body>
</html>
"""

class CaptureBrowserRequestHandler(BaseHTTPRequestHandler):
    server: "CaptureBrowserHTTPServer"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[capture-browser] {self.address_string()} - {format % args}", flush=True)

    def do_GET(self) -> None:
        status, content_type, payload = dispatch_capture_browser_request(self.server.store, "GET", self.path)
        if status == HTTPStatus.NO_CONTENT:
            self.send_response(status)
            self.end_headers()
            return
        if content_type.startswith("text/html"):
            self._send_html(str(payload), status=status)
            return
        self._send_json(dict(payload), status=status)

    def do_POST(self) -> None:
        status, _, payload = dispatch_capture_browser_request(self.server.store, "POST", self.path)
        self._send_json(dict(payload), status=status)

    def _send_html(self, payload: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class CaptureBrowserHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], store: CaptureSessionStore) -> None:
        super().__init__(server_address, CaptureBrowserRequestHandler)
        self.store = store


def dispatch_capture_browser_request(
    store: CaptureSessionStore,
    method: str,
    raw_path: str,
) -> tuple[HTTPStatus, str, dict[str, Any] | str]:
    parsed = urlparse(raw_path)
    path = parsed.path

    if method == "GET":
        if path in {"/", "/index.html"}:
            return HTTPStatus.OK, "text/html; charset=utf-8", _browser_app_html()
        if path == "/api/sessions":
            return HTTPStatus.OK, "application/json; charset=utf-8", {"sessions": store.list_sessions()}
        if path.startswith("/api/sessions/"):
            capture_id = path.removeprefix("/api/sessions/").strip("/")
            detail = store.get_session_detail(capture_id)
            if detail is None:
                return (
                    HTTPStatus.NOT_FOUND,
                    "application/json; charset=utf-8",
                    {"error": f"unknown capture_id: {capture_id}"},
                )
            return HTTPStatus.OK, "application/json; charset=utf-8", detail
        if path == "/favicon.ico":
            return HTTPStatus.NO_CONTENT, "text/plain; charset=utf-8", ""
        return HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", {"error": f"unknown path: {path}"}

    if method == "POST":
        if path != "/api/refresh":
            return HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", {"error": f"unknown path: {path}"}
        return HTTPStatus.OK, "application/json; charset=utf-8", {"sessions": store.refresh()}

    return (
        HTTPStatus.METHOD_NOT_ALLOWED,
        "application/json; charset=utf-8",
        {"error": f"unsupported method: {method}"},
    )


def create_capture_browser_server(
    *,
    captures_root: Path = DEFAULT_CAPTURES_ROOT,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> CaptureBrowserHTTPServer:
    store = CaptureSessionStore(captures_root=captures_root)
    store.refresh()
    return CaptureBrowserHTTPServer((host, port), store)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a browser UI for OpenCode captures.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host for the local browser service")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port for the local browser service")
    parser.add_argument("--captures-root", type=Path, default=DEFAULT_CAPTURES_ROOT, help="Override artifacts/captures directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    server = create_capture_browser_server(
        captures_root=args.captures_root,
        host=args.host,
        port=args.port,
    )
    host, port = server.server_address
    print(f"http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
