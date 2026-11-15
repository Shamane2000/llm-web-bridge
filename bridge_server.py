#!/usr/bin/env python3
"""
Gemini Local Agent Bridge & Live-Debate Server v3.0
===================================================
Verbindet Google Gemini im Browser über Tampermonkey mit dem lokalen Linux-Terminal
und bietet ein vollständiges Mission-Control Command-Center.

Port: 8765
Autor: Jarvis & Frank
"""

import base64
import hashlib
import http.server
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import threading
from datetime import datetime
from urllib.parse import urlparse, parse_qs

HOST = "0.0.0.0"
PORT = 8765
DOWNLOADS_DIR = os.path.expanduser("~/Downloads")
IMAGES_DIR = os.path.join(DOWNLOADS_DIR, "gemini_images")
SCRATCH_DIR = os.path.join(tempfile.gettempdir(), "gemini_bridge_scratch")
LOG_MD_PATH = os.path.join(DOWNLOADS_DIR, "gemini_debate_live.md")
LOG_JSON_PATH = os.path.join(DOWNLOADS_DIR, "gemini_debate_live.json")
STATS_DB_PATH = os.path.join(DOWNLOADS_DIR, "bridge_stats.db")

os.makedirs(SCRATCH_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

# Event für synchrone OpenAI / LiteLLM Antworten
MISSION_COMPLETED_EVENT = threading.Event()
# Mutex-Lock für strikte Serialisierung (Queueing) aller parallelen Anfragen
MISSION_LOCK = threading.Lock()
STATS_LOCK = threading.Lock()
TABS_LOCK = threading.Lock()
ACTIVE_TABS = {}  # tab_id -> {"role": str, "last_seen": float}
CACHE_LOCK = threading.Lock()
RESPONSE_CACHE = {}  # sha256 -> {"ts": float, "content": str}
CACHE_TTL_SEC = 300


def cleanup_and_get_tabs():
    now_ts = time.time()
    with TABS_LOCK:
        stale = [tid for tid, info in ACTIVE_TABS.items() if now_ts - info.get("last_seen", 0) > 20]
        for tid in stale:
            del ACTIVE_TABS[tid]
        return dict(ACTIVE_TABS)


def register_tab(tab_id, requested_role=""):
    now_ts = time.time()
    with TABS_LOCK:
        stale = [tid for tid, info in ACTIVE_TABS.items() if now_ts - info.get("last_seen", 0) > 20]
        for tid in stale:
            del ACTIVE_TABS[tid]

        if requested_role:
            ACTIVE_TABS[tab_id] = {"role": requested_role, "last_seen": now_ts}
            return requested_role

        if tab_id in ACTIVE_TABS and ACTIVE_TABS[tab_id].get("role"):
            ACTIVE_TABS[tab_id]["last_seen"] = now_ts
            return ACTIVE_TABS[tab_id]["role"]

        roles = [info.get("role") for info in ACTIVE_TABS.values()]
        if "worker" not in roles and "coder" not in roles:
            assigned = "worker"
        elif "image" not in roles:
            assigned = "image"
        else:
            assigned = "solo"

        ACTIVE_TABS[tab_id] = {"role": assigned, "last_seen": now_ts}
        return assigned

# In-Memory Event Store
DEBATE_STATE = {
    "active_task": "",
    "started_at": "",
    "final_result": "",
    "pending_mission": None,
    "browser_provider": "gemini",
    "browser_model": "Erkennung läuft...",
    "browser_capability": "chat",
    "available_models": [],
    "desired_model": "",
    "limit_warning": "",
    "model_cooldowns": {},
    "mission_started_ts": 0.0,
    "mission_source": "dashboard",
    "events": []
}


def init_stats_db():
    with STATS_LOCK:
        conn = sqlite3.connect(STATS_DB_PATH)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    prompt_chars INTEGER NOT NULL,
                    completion_chars INTEGER NOT NULL,
                    task_preview TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_cooldowns (
                    model TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    limited_at REAL NOT NULL,
                    retry_after REAL NOT NULL,
                    retry_after_str TEXT NOT NULL,
                    reason TEXT NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()


def set_model_cooldown(provider, model, retry_after_ts, retry_after_str, reason=""):
    if not model:
        return
    now_ts = time.time()
    if not retry_after_ts or retry_after_ts <= now_ts:
        retry_after_ts = now_ts + 3600
    if not retry_after_str:
        retry_after_str = datetime.fromtimestamp(retry_after_ts).strftime("%H:%M")
    with STATS_LOCK:
        conn = sqlite3.connect(STATS_DB_PATH)
        try:
            conn.execute(
                """
                INSERT INTO model_cooldowns (model, provider, limited_at, retry_after, retry_after_str, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(model) DO UPDATE SET
                    provider=excluded.provider,
                    limited_at=excluded.limited_at,
                    retry_after=excluded.retry_after,
                    retry_after_str=excluded.retry_after_str,
                    reason=excluded.reason
                """,
                (model, provider or "gemini", now_ts, float(retry_after_ts), retry_after_str, reason or ""),
            )
            conn.commit()
        finally:
            conn.close()
    DEBATE_STATE["model_cooldowns"] = get_active_cooldowns()


def clear_model_cooldown(model):
    if not model:
        return
    with STATS_LOCK:
        conn = sqlite3.connect(STATS_DB_PATH)
        try:
            conn.execute("DELETE FROM model_cooldowns WHERE LOWER(model) = LOWER(?)", (model,))
            conn.commit()
        finally:
            conn.close()
    DEBATE_STATE["model_cooldowns"] = get_active_cooldowns()


def get_active_cooldowns():
    now_ts = time.time()
    result = {}
    with STATS_LOCK:
        conn = sqlite3.connect(STATS_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM model_cooldowns").fetchall()
            for r in rows:
                rem_sec = max(0, int(r["retry_after"] - now_ts))
                result[r["model"]] = {
                    "model": r["model"],
                    "provider": r["provider"],
                    "limited_at": r["limited_at"],
                    "retry_after": r["retry_after"],
                    "retry_after_str": r["retry_after_str"],
                    "remaining_min": round(rem_sec / 60),
                    "expired": now_ts >= r["retry_after"],
                    "reason": r["reason"],
                }
        finally:
            conn.close()
    return result


def estimate_tokens(text):
    if not text:
        return 0
    # Praxisnahe Näherung für Deutsch/Englisch/Code (~4 Zeichen pro Token, mind. Wortanzahl)
    return max(1, max(len(text.split()), len(text) // 4))


def record_model_run(provider, model, capability, source, status, latency_ms, prompt_text, completion_text):
    p_tok = estimate_tokens(prompt_text)
    c_tok = estimate_tokens(completion_text)
    t_tok = p_tok + c_tok
    preview = (prompt_text or "").replace("\n", " ").strip()[:140]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with STATS_LOCK:
        conn = sqlite3.connect(STATS_DB_PATH)
        try:
            conn.execute(
                """
                INSERT INTO model_runs (
                    created_at, provider, model, capability, source, status,
                    latency_ms, prompt_tokens, completion_tokens, total_tokens,
                    prompt_chars, completion_chars, task_preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    provider or "gemini",
                    model or "unknown",
                    capability or "chat",
                    source or "dashboard",
                    status or "ok",
                    int(latency_ms),
                    p_tok,
                    c_tok,
                    t_tok,
                    len(prompt_text or ""),
                    len(completion_text or ""),
                    preview,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_model_stats_summary():
    with STATS_LOCK:
        conn = sqlite3.connect(STATS_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            summary_rows = conn.execute("""
                SELECT
                    provider,
                    model,
                    capability,
                    COUNT(*) AS runs,
                    SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_runs,
                    SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS err_runs,
                    ROUND(AVG(latency_ms) / 1000.0, 2) AS avg_latency_s,
                    ROUND(MIN(latency_ms) / 1000.0, 2) AS min_latency_s,
                    ROUND(MAX(latency_ms) / 1000.0, 2) AS max_latency_s,
                    SUM(prompt_tokens) AS sum_prompt_tokens,
                    SUM(completion_tokens) AS sum_completion_tokens,
                    SUM(total_tokens) AS sum_total_tokens,
                    MAX(created_at) AS last_used
                FROM model_runs
                GROUP BY provider, model, capability
                ORDER BY last_used DESC
            """).fetchall()
            recent_rows = conn.execute("""
                SELECT created_at, provider, model, capability, source, status,
                       ROUND(latency_ms / 1000.0, 2) AS latency_s,
                       prompt_tokens, completion_tokens, total_tokens, task_preview
                FROM model_runs
                ORDER BY id DESC
                LIMIT 20
            """).fetchall()
            return {
                "summary": [dict(r) for r in summary_rows],
                "recent": [dict(r) for r in recent_rows],
            }
        finally:
            conn.close()


init_stats_db()
DEBATE_STATE["model_cooldowns"] = get_active_cooldowns()

def append_to_markdown_log(event):
    timestamp = event.get("time", datetime.now().strftime("%H:%M:%S"))
    role = event.get("role", "info").upper()
    title = event.get("title", "")
    content = event.get("content", "").strip()
    round_no = event.get("round", "")

    header_icon = {
        "CODER": "💻",
        "REVIEWER": "🧐",
        "TERMINAL": "🧪",
        "SYSTEM": "⚙️",
        "FINAL": "🏆"
    }.get(role, "📌")

    md_entry = f"\n### {header_icon} [{timestamp}] {role} (Runde {round_no}): {title}\n\n"
    if content.startswith("```"):
        md_entry += f"{content}\n\n"
    elif "def " in content or "class " in content or "#!" in content:
        md_entry += f"```python\n{content}\n```\n\n"
    else:
        md_entry += f"{content}\n\n"
    md_entry += "---\n"

    try:
        with open(LOG_MD_PATH, "a", encoding="utf-8") as f:
            f.write(md_entry)
    except Exception as e:
        print(f"Fehler beim Schreiben von MD-Log: {e}")

    try:
        with open(LOG_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(DEBATE_STATE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        pass

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🎯 Gemini Multi-Agent Mission Control</title>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        :root {
            --bg: #090d16;
            --card-bg: #131926;
            --border: #232f45;
            --border-hover: #3b82f6;
            --text: #e2e8f0;
            --text-muted: #94a3b8;
            --coder-color: #60a5fa;
            --reviewer-color: #c084fc;
            --terminal-color: #4ade80;
            --final-color: #fbbf24;
            --accent: #2563eb;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            padding: 24px;
            min-height: 100vh;
        }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 18px;
            border-bottom: 1px solid var(--border);
            margin-bottom: 20px;
        }
        .header-title {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .header-title h1 {
            font-size: 22px;
            font-weight: 800;
            background: linear-gradient(135deg, #60a5fa 0%, #c084fc 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .badge {
            font-size: 11px;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 14px;
            background: rgba(34, 197, 94, 0.15);
            color: #4ade80;
            border: 1px solid rgba(74, 222, 128, 0.3);
        }
        .controls {
            display: flex;
            gap: 10px;
        }
        button.btn {
            background: #1e293b;
            border: 1px solid var(--border);
            color: var(--text);
            padding: 7px 14px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        button.btn:hover {
            background: #283548;
            border-color: #64748b;
        }

        /* Mission Control Box */
        .mission-box {
            background: #111726;
            border: 1.5px solid #1e293b;
            border-radius: 14px;
            padding: 18px 20px;
            margin-bottom: 20px;
            box-shadow: 0 8px 25px rgba(0,0,0,0.4);
        }
        .mission-header {
            font-size: 13px;
            font-weight: 700;
            color: #93c5fd;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .mission-input-row {
            display: flex;
            gap: 12px;
            align-items: center;
        }
        #mission-text {
            flex: 1;
            background: #090d16;
            border: 1px solid var(--border);
            color: #f8fafc;
            border-radius: 10px;
            padding: 12px 16px;
            font-size: 14px;
            outline: none;
            transition: border-color 0.2s;
        }
        #mission-text:focus {
            border-color: #3b82f6;
            box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
        }
        .btn-launch {
            background: linear-gradient(135deg, #1d4ed8 0%, #3b82f6 100%);
            color: #fff;
            border: none;
            border-radius: 10px;
            padding: 12px 22px;
            font-size: 14px;
            font-weight: 700;
            cursor: pointer;
            white-space: nowrap;
            transition: transform 0.1s, opacity 0.2s;
        }
        .btn-launch:hover {
            opacity: 0.95;
            transform: translateY(-1px);
        }
        .mission-options {
            display: flex;
            gap: 16px;
            align-items: center;
            margin-top: 10px;
            font-size: 12px;
            color: var(--text-muted);
        }

        /* Final Result Banner */
        .final-box {
            background: rgba(251, 191, 36, 0.05);
            border: 2px solid rgba(251, 191, 36, 0.4);
            border-radius: 14px;
            padding: 20px;
            margin-bottom: 24px;
            display: none;
            animation: fadeIn 0.4s ease;
        }
        .final-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            border-bottom: 1px solid rgba(251, 191, 36, 0.2);
            padding-bottom: 8px;
        }
        .final-title {
            font-size: 16px;
            font-weight: 800;
            color: #fbbf24;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .final-content {
            font-size: 14px;
            line-height: 1.6;
            color: #f1f5f9;
            max-height: 520px;
            overflow-y: auto;
            background: rgba(0,0,0,0.4);
            padding: 18px;
            border-radius: 8px;
            font-family: inherit;
        }
        .final-content table {
            width: 100%;
            border-collapse: collapse;
            margin: 14px 0;
            font-size: 13px;
        }
        .final-content th, .final-content td {
            border: 1px solid rgba(255, 255, 255, 0.15);
            padding: 8px 12px;
            text-align: left;
        }
        .final-content th {
            background: rgba(255, 255, 255, 0.08);
            color: #60a5fa;
            font-weight: 700;
        }
        .final-content tr:nth-child(even) {
            background: rgba(255, 255, 255, 0.03);
        }
        .final-content code {
            background: rgba(0, 0, 0, 0.5);
            color: #4ade80;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: monospace;
        }
        .final-content pre {
            background: rgba(0, 0, 0, 0.6);
            padding: 12px;
            border-radius: 6px;
            overflow-x: auto;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        .final-content pre code {
            padding: 0;
            background: transparent;
        }
        .final-content h1, .final-content h2, .final-content h3 {
            color: #fbbf24;
            margin-top: 18px;
            margin-bottom: 8px;
        }
        .final-content ul, .final-content ol {
            padding-left: 20px;
            margin: 8px 0;
        }

        /* Grid */
        .grid {
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 16px;
            margin-bottom: 24px;
        }
        @media (max-width: 1100px) {
            .grid { grid-template-columns: 1fr; }
        }
        .column {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            height: calc(100vh - 360px);
            min-height: 400px;
        }
        .column-header {
            padding: 12px 16px;
            font-size: 13px;
            font-weight: 700;
            display: flex;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid var(--border);
            background: rgba(255,255,255,0.02);
        }
        .col-coder .column-header { border-top: 3px solid var(--coder-color); color: var(--coder-color); }
        .col-terminal .column-header { border-top: 3px solid var(--terminal-color); color: var(--terminal-color); }
        .col-reviewer .column-header { border-top: 3px solid var(--reviewer-color); color: var(--reviewer-color); }
        .events-stream {
            padding: 12px;
            overflow-y: auto;
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .event-card {
            background: #090d16;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px;
            font-size: 12px;
            animation: fadeIn 0.3s ease;
        }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
        .event-card.coder { border-left: 3px solid var(--coder-color); }
        .event-card.reviewer { border-left: 3px solid var(--reviewer-color); }
        .event-card.terminal { border-left: 3px solid var(--terminal-color); }
        .event-card.final { border-left: 3px solid var(--final-color); background: rgba(251, 191, 36, 0.05); }
        .event-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
            font-size: 11px;
            color: var(--text-muted);
        }
        .event-title {
            font-weight: 700;
            color: #f8fafc;
            font-size: 13px;
            margin-bottom: 6px;
        }
        .event-body {
            line-height: 1.5;
            white-space: pre-wrap;
            word-break: break-word;
            color: var(--text);
            max-height: 380px;
            overflow-y: auto;
            font-family: ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, monospace;
            background: rgba(0,0,0,0.3);
            padding: 10px;
            border-radius: 6px;
            border: 1px solid rgba(255,255,255,0.05);
        }
        .tag-perfect {
            background: rgba(34, 197, 94, 0.2);
            color: #4ade80;
            border: 1px solid #4ade80;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: bold;
        }
        .empty-hint {
            color: var(--text-muted);
            text-align: center;
            padding: 40px 10px;
            font-size: 12px;
            font-style: italic;
        }
    </style>
</head>
<body>
    <header>
        <div class="header-title">
            <h1>🎯 Gemini Multi-Agent Mission Control</h1>
            <span class="badge">v3.0 Live-Bridge</span>
        </div>
        <div class="controls">
            <button class="btn" onclick="copyMarkdownLink()">📋 ~/Downloads/gemini_debate_live.md</button>
            <button class="btn" onclick="clearDebate()">🗑️ Verlauf leeren</button>
        </div>
    </header>

    <!-- Mission Control Eingabe -->
    <div class="mission-box">
        <div class="mission-header">
            <span>🚀 Neue Mission an das Gemini Multi-Agent-Tandem übergeben:</span>
        </div>
        <div class="mission-input-row">
            <input type="text" id="mission-text" placeholder="Z. B.: Ich möchte eine Übersicht meines Systems (RAM, CPU, Festplatten, NVidia-GPU)..." autofocus>
            <button class="btn-launch" id="btn-submit" onclick="submitMission()">▶️ Mission starten</button>
        </div>
        <div class="mission-options">
            <label style="display: flex; align-items: center; gap: 6px; cursor: pointer;">
                <input type="checkbox" id="opt-execute" checked style="accent-color: #3b82f6;"> 
                <strong>⚡ ReAct-Action:</strong> Code auf Linux-Terminal ausführen & echte Messwerte auswerten
            </label>
            <span>•</span>
            <label style="display: flex; align-items: center; gap: 6px;">
                Debatten-Runden:
                <select id="opt-rounds" style="background: #090d16; border: 1px solid var(--border); color: #fff; border-radius: 6px; padding: 2px 6px; font-size: 12px;">
                    <option value="1">1 Runde</option>
                    <option value="2" selected>2 Runden</option>
                    <option value="3">3 Runden</option>
                </select>
            </label>
            <span>•</span>
            <span id="mission-status-label" style="color: #60a5fa; font-weight: 600;">Bereit für neue Mission</span>
        </div>
        <div class="mission-options" style="margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.08); justify-content: space-between; flex-wrap: wrap;">
            <div style="display: flex; align-items: center; gap: 10px;">
                <span>🤖 Browser-Modell: <strong id="browser-model-badge" style="color: #34d399;">Erkennung läuft...</strong></span>
                <span id="limit-warning-badge" style="display: none; background: rgba(239,68,68,0.2); color: #f87171; border: 1px solid #ef4444; padding: 2px 8px; border-radius: 6px; font-weight: 600;"></span>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
                <span>Modell im Browser wechseln:</span>
                <select id="opt-browser-model" style="background: #090d16; border: 1px solid var(--border); color: #fff; border-radius: 6px; padding: 3px 8px; font-size: 12px;">
                    <option value="Flash">⚡ Flash (Schnell / Hohes Limit)</option>
                    <option value="Pro">🧠 Pro / Thinking (Deep Reasoning)</option>
                </select>
                <button class="btn" onclick="switchBrowserModel()" style="padding: 4px 10px; font-size: 12px;">🔄 Umschalten</button>
            </div>
        </div>
    </div>

    <!-- Finale Antwort / Ergebnis-Box -->
    <div class="final-box" id="final-box">
        <div class="final-header">
            <div class="final-title">🏆 FINALE ANTWORT DES MULTI-AGENT-TANDEMS</div>
            <button class="btn" onclick="copyFinalResult()">📋 Antwort kopieren</button>
        </div>
        <div class="final-content" id="final-content"></div>
    </div>

    <div class="grid">
        <!-- Spalte 1: Coder -->
        <div class="column col-coder">
            <div class="column-header">
                <span>💻 Tab 1: Coder (Entwurf & Nachbesserung)</span>
                <span id="count-coder">0</span>
            </div>
            <div class="events-stream" id="stream-coder">
                <div class="empty-hint">Noch keine Coder-Ereignisse</div>
            </div>
        </div>

        <!-- Spalte 2: Terminal-Bridge -->
        <div class="column col-terminal">
            <div class="column-header">
                <span>🧪 Terminal-Bridge (Echte Linux-Ausführung & Daten)</span>
                <span id="count-terminal">0</span>
            </div>
            <div class="events-stream" id="stream-terminal">
                <div class="empty-hint">Keine Terminal-Aktivitäten</div>
            </div>
        </div>

        <!-- Spalte 3: Reviewer -->
        <div class="column col-reviewer">
            <div class="column-header">
                <span>🧐 Tab 2: Reviewer (QA, Bugs & Freigabe)</span>
                <span id="count-reviewer">0</span>
            </div>
            <div class="events-stream" id="stream-reviewer">
                <div class="empty-hint">Wartet auf Code-Empfang</div>
            </div>
        </div>
    </div>

    <!-- Modell-Statistik & Vergleich (Gemini / Claude / GPT) -->
    <div style="margin-top: 16px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 8px;">
            <span style="font-weight: 700; font-size: 14px; color: #93c5fd;">📊 Modell-Statistik &amp; Benchmark (Latenz, Tokens, Rollen)</span>
            <button class="btn" onclick="fetchStats()" style="padding: 4px 10px; font-size: 12px;">🔄 Aktualisieren</button>
        </div>
        <div style="overflow-x: auto;">
            <table style="width: 100%; border-collapse: collapse; font-size: 12px;">
                <thead>
                    <tr style="border-bottom: 1px solid var(--border); color: var(--muted); text-align: left;">
                        <th style="padding: 6px 8px;">Provider</th>
                        <th style="padding: 6px 8px;">Modell</th>
                        <th style="padding: 6px 8px;">Rolle</th>
                        <th style="padding: 6px 8px;">Runs</th>
                        <th style="padding: 6px 8px;">OK / Err</th>
                        <th style="padding: 6px 8px;">Ø Latenz</th>
                        <th style="padding: 6px 8px;">Min / Max</th>
                        <th style="padding: 6px 8px;">Prompt-Tok</th>
                        <th style="padding: 6px 8px;">Output-Tok</th>
                        <th style="padding: 6px 8px;">Gesamt-Tok</th>
                        <th style="padding: 6px 8px;">Zuletzt</th>
                    </tr>
                </thead>
                <tbody id="stats-summary-body">
                    <tr><td colspan="11" style="padding: 10px; color: var(--muted); text-align: center;">Noch keine Läufe aufgezeichnet</td></tr>
                </tbody>
            </table>
        </div>
        <div style="margin-top: 10px; font-size: 11px; color: var(--muted);" id="stats-recent-list"></div>
    </div>

    <script>
        let lastEventCount = 0;

        async function fetchEvents() {
            try {
                const res = await fetch('/events');
                if (!res.ok) return;
                const data = await res.json();

                // Modell-Status & Limit-Warnung aktualisieren
                const modelBadge = document.getElementById('browser-model-badge');
                if (modelBadge && data.browser_model) {
                    modelBadge.textContent = data.desired_model ? `${data.browser_model} ➔ Wechsle zu ${data.desired_model}...` : data.browser_model;
                }
                const limitBadge = document.getElementById('limit-warning-badge');
                if (limitBadge) {
                    const cds = Object.values(data.model_cooldowns || {}).filter(c => !c.expired);
                    if (cds.length > 0) {
                        const cdText = cds.map(c => `⏳ ${c.model} bis ${c.retry_after_str} (noch ${c.remaining_min}m)`).join(' | ');
                        limitBadge.textContent = data.limit_warning ? `⚠️ ${data.limit_warning} (${cdText})` : cdText;
                        limitBadge.style.display = 'inline-block';
                    } else if (data.limit_warning) {
                        limitBadge.textContent = `⚠️ ${data.limit_warning}`;
                        limitBadge.style.display = 'inline-block';
                    } else {
                        limitBadge.style.display = 'none';
                    }
                }
                if (data.available_models && data.available_models.length > 0) {
                    const sel = document.getElementById('opt-browser-model');
                    if (sel && sel.dataset.loaded !== 'true') {
                        sel.innerHTML = '';
                        data.available_models.forEach(m => {
                            const opt = document.createElement('option');
                            opt.value = m;
                            opt.textContent = m;
                            sel.appendChild(opt);
                        });
                        sel.dataset.loaded = 'true';
                    }
                }

                // Status & Finale Antwort prüfen
                if (data.final_result) {
                    const finalBox = document.getElementById('final-box');
                    const finalContent = document.getElementById('final-content');
                    if (window.marked && typeof marked.parse === 'function') {
                        finalContent.innerHTML = marked.parse(data.final_result);
                    } else {
                        finalContent.textContent = data.final_result;
                    }
                    finalBox.style.display = 'block';
                    document.getElementById('mission-status-label').textContent = '✅ Mission erfolgreich beendet!';
                } else if (data.pending_mission) {
                    document.getElementById('mission-status-label').textContent = '⏳ Agenten bearbeiten Mission...';
                }

                const events = data.events || [];
                if (events.length === lastEventCount) return;
                lastEventCount = events.length;

                renderEvents(events);
            } catch (e) {
                console.error(e);
            }
        }

        function renderEvents(events) {
            const streamCoder = document.getElementById('stream-coder');
            const streamTerminal = document.getElementById('stream-terminal');
            const streamReviewer = document.getElementById('stream-reviewer');

            streamCoder.innerHTML = '';
            streamTerminal.innerHTML = '';
            streamReviewer.innerHTML = '';

            let cCoder = 0, cTerm = 0, cRev = 0;

            events.forEach(ev => {
                const card = document.createElement('div');
                card.className = `event-card ${ev.role || 'coder'}`;

                const isPerfect = (ev.content && ev.content.includes('VERIFIED_PERFECT'));
                const badgeHtml = isPerfect ? '<span class="tag-perfect">VERIFIED_PERFECT</span>' : '';

                card.innerHTML = `
                    <div class="event-header">
                        <span>Runde ${ev.round || '1'} • ${ev.time || ''}</span>
                        ${badgeHtml}
                    </div>
                    <div class="event-title">${escapeHtml(ev.title || '')}</div>
                    <div class="event-body">${escapeHtml(ev.content || '')}</div>
                `;

                if (ev.role === 'coder' || ev.role === 'final') {
                    streamCoder.appendChild(card);
                    cCoder++;
                } else if (ev.role === 'terminal') {
                    streamTerminal.appendChild(card);
                    cTerm++;
                } else if (ev.role === 'reviewer') {
                    streamReviewer.appendChild(card);
                    cRev++;
                }
            });

            document.getElementById('count-coder').textContent = cCoder;
            document.getElementById('count-terminal').textContent = cTerm;
            document.getElementById('count-reviewer').textContent = cRev;

            streamCoder.scrollTop = streamCoder.scrollHeight;
            streamTerminal.scrollTop = streamTerminal.scrollHeight;
            streamReviewer.scrollTop = streamReviewer.scrollHeight;
        }

        async function submitMission() {
            const input = document.getElementById('mission-text');
            const task = input.value.trim();
            if (!task) return;

            const rounds = parseInt(document.getElementById('opt-rounds').value, 10) || 2;
            const execute = document.getElementById('opt-execute').checked;

            document.getElementById('final-box').style.display = 'none';
            document.getElementById('final-content').textContent = '';
            document.getElementById('mission-status-label').textContent = '📡 Mission an Tab 1 (Coder) gesendet...';

            await fetch('/submit_mission', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task: task, rounds: rounds, execute: execute })
            });

            fetchEvents();
        }

        async function switchBrowserModel() {
            const sel = document.getElementById('opt-browser-model');
            const model = sel ? sel.value : '';
            if (!model) return;
            await fetch('/set_model', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model: model })
            });
            fetchEvents();
        }

        document.getElementById('mission-text').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') submitMission();
        });

        function escapeHtml(str) {
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;');
        }

        async function clearDebate() {
            if (!confirm('Gesamten Debatten-Verlauf leeren?')) return;
            await fetch('/clear_events', { method: 'POST' });
            document.getElementById('final-box').style.display = 'none';
            document.getElementById('final-content').textContent = '';
            document.getElementById('mission-status-label').textContent = 'Bereit für neue Mission';
            lastEventCount = -1;
            fetchEvents();
        }

        function copyMarkdownLink() {
            navigator.clipboard.writeText('~/Downloads/gemini_debate_live.md');
            alert('Pfad in Zwischenablage kopiert:\\n~/Downloads/gemini_debate_live.md');
        }

        function copyFinalResult() {
            const text = document.getElementById('final-content').textContent;
            navigator.clipboard.writeText(text);
            alert('Finale Antwort in die Zwischenablage kopiert!');
        }

        async function fetchStats() {
            try {
                const res = await fetch('/stats');
                if (!res.ok) return;
                const data = await res.json();
                const tbody = document.getElementById('stats-summary-body');
                if (tbody) {
                    const rows = data.summary || [];
                    if (rows.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="11" style="padding: 10px; color: var(--muted); text-align: center;">Noch keine Läufe aufgezeichnet</td></tr>';
                    } else {
                        tbody.innerHTML = rows.map(r => `
                            <tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
                                <td style="padding: 6px 8px; font-weight: 600;">${escapeHtml(r.provider)}</td>
                                <td style="padding: 6px 8px; color: #34d399;">${escapeHtml(r.model)}</td>
                                <td style="padding: 6px 8px;">${escapeHtml(r.capability)}</td>
                                <td style="padding: 6px 8px;">${r.runs}</td>
                                <td style="padding: 6px 8px;"><span style="color:#34d399;">${r.ok_runs}</span> / <span style="color:#f87171;">${r.err_runs}</span></td>
                                <td style="padding: 6px 8px; font-weight: 600;">${r.avg_latency_s}s</td>
                                <td style="padding: 6px 8px; color: var(--muted);">${r.min_latency_s}s / ${r.max_latency_s}s</td>
                                <td style="padding: 6px 8px;">${r.sum_prompt_tokens}</td>
                                <td style="padding: 6px 8px;">${r.sum_completion_tokens}</td>
                                <td style="padding: 6px 8px; font-weight: 600; color: #93c5fd;">${r.sum_total_tokens}</td>
                                <td style="padding: 6px 8px; color: var(--muted);">${escapeHtml(r.last_used)}</td>
                            </tr>
                        `).join('');
                    }
                }
                const recentEl = document.getElementById('stats-recent-list');
                if (recentEl) {
                    const rec = (data.recent || []).slice(0, 5);
                    if (rec.length > 0) {
                        recentEl.innerHTML = '<strong>Letzte 5 Läufe:</strong> ' + rec.map(x =>
                            `[${escapeHtml(x.created_at.slice(11))}] ${escapeHtml(x.provider)}/${escapeHtml(x.model)} (${escapeHtml(x.capability)}, ${escapeHtml(x.source)}): ${x.latency_s}s, ~${x.total_tokens} Tok (${escapeHtml(x.status)})`
                        ).join(' &nbsp;|&nbsp; ');
                    } else {
                        recentEl.textContent = '';
                    }
                }
            } catch (e) {
                console.error(e);
            }
        }

        fetchEvents();
        fetchStats();
        setInterval(fetchEvents, 1000);
        setInterval(fetchStats, 5000);
    </script>
</body>
</html>
"""

class GeminiBridgeHandler(http.server.BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def _send_json(self, status_code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_openai_completion(self, completion_id, model, is_stream, res_content, prompt_tokens, completion_tokens, tool_call=None):
        if is_stream:
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            if tool_call:
                t_name, t_args = tool_call
                args_str = json.dumps(t_args, ensure_ascii=False)
                call_id = f"call_{int(time.time() * 1000)}"
                chunks = [
                    ({"role": "assistant", "tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": t_name, "arguments": ""}}]}, None),
                    ({"tool_calls": [{"index": 0, "function": {"arguments": args_str}}]}, None),
                    ({}, "tool_calls"),
                ]
            else:
                chunks = [({"role": "assistant"}, None), ({"content": res_content}, None), ({}, "stop")]
            for delta, reason in chunks:
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            if tool_call:
                t_name, t_args = tool_call
                call_id = f"call_{int(time.time() * 1000)}"
                msg_obj = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": t_name, "arguments": json.dumps(t_args, ensure_ascii=False)}
                        }
                    ]
                }
                f_reason = "tool_calls"
            else:
                msg_obj = {"role": "assistant", "content": res_content}
                f_reason = "stop"
            self._send_json(200, {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": msg_obj, "finish_reason": f_reason}],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens
                }
            })

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ["/", "/dashboard"]:
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/events":
            DEBATE_STATE["model_cooldowns"] = get_active_cooldowns()
            DEBATE_STATE["active_tabs"] = cleanup_and_get_tabs()
            self._send_json(200, DEBATE_STATE)

        elif path == "/stats":
            self._send_json(200, get_model_stats_summary())

        elif path == "/register_tab":
            tab_id = (qs.get("tab_id") or [""])[0].strip()
            req_role = (qs.get("role") or [""])[0].strip()
            if not tab_id:
                self._send_json(400, {"error": "tab_id required"})
                return
            assigned = register_tab(tab_id, req_role)
            print(f"[\033[96mTAB-AUTO\033[0m] Tab {tab_id[:8]} -> Rolle '{assigned}'")
            self._send_json(200, {"assigned_role": assigned, "active_tabs": cleanup_and_get_tabs()})

        elif path in ["/v1/models", "/models"]:
            self._send_json(200, {
                "object": "list",
                "data": [
                    {
                        "id": "gemini-browser",
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "gemini-tandem"
                    }
                ]
            })

        elif path == "/pending_mission":
            tab_id = (qs.get("tab_id") or [""])[0].strip()
            tab_role = (qs.get("role") or [""])[0].strip()
            if tab_id and tab_role:
                register_tab(tab_id, tab_role)
            cooldowns = get_active_cooldowns()
            DEBATE_STATE["model_cooldowns"] = cooldowns
            if not DEBATE_STATE.get("desired_model") and not DEBATE_STATE.get("pending_mission"):
                cur_m = (DEBATE_STATE.get("browser_model") or "").lower()
                for m_name, c in cooldowns.items():
                    if c.get("expired") and m_name.lower() not in cur_m:
                        DEBATE_STATE["desired_model"] = m_name
                        # Bei Fehlschlag 10 Min warten bis zum nächsten Versuch
                        set_model_cooldown(c.get("provider", "gemini"), m_name, time.time() + 600, "", "Auto-Recovery-Probe läuft")
                        print(f"[\033[96mAUTO-RECOVERY\033[0m] Cooldown für '{m_name}' abgelaufen – versuche Rückwechsel im Browser...")
                        break
            mission = DEBATE_STATE.get("pending_mission")
            active_roles = [info.get("role") for info in cleanup_and_get_tabs().values()]
            # Capability-Filter: Bild-Tab nimmt nur Bild-Aufträge; Worker übernimmt Bild nur wenn kein Bild-Tab aktiv ist
            if mission and tab_role:
                m_cap = mission.get("capability", "chat")
                if tab_role == "image" and m_cap != "image":
                    mission = None
                elif tab_role in ("worker", "coder") and m_cap == "image" and "image" in active_roles:
                    mission = None
            self._send_json(200, {
                "mission": mission,
                "desired_model": DEBATE_STATE.get("desired_model", ""),
                "model_cooldowns": cooldowns,
                "has_reviewer": "reviewer" in active_roles
            })

        elif path.startswith("/images/"):
            fname = os.path.basename(path[len("/images/"):])
            fpath = os.path.join(IMAGES_DIR, fname)
            if not fname or not os.path.isfile(fpath):
                self._send_json(404, {"error": "Image not found"})
                return
            ext = os.path.splitext(fname)[1].lower()
            ctype = "image/png"
            if ext in (".jpg", ".jpeg"):
                ctype = "image/jpeg"
            elif ext == ".webp":
                ctype = "image/webp"
            with open(fpath, "rb") as f:
                data_bytes = f.read()
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data_bytes)))
            self.end_headers()
            self.wfile.write(data_bytes)

        elif path in ["/health", "/status"]:
            self._send_json(200, {
                "status": "ok",
                "service": "Gemini-Terminal-Bridge",
                "version": "3.0",
                "user": os.getenv("USER", "user"),
                "scratch_dir": SCRATCH_DIR,
                "events_count": len(DEBATE_STATE["events"]),
                "timestamp": time.time()
            })
        else:
            self._send_json(404, {"error": "Endpoint not found"})

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        try:
            payload = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception as e:
            self._send_json(400, {"error": "Invalid JSON", "details": str(e)})
            return

        # ------------------------------------------------------------------
        # 0. /v1/chat/completions - OpenAI API Endpunkt für LiteLLM & Hermes
        # ------------------------------------------------------------------
        if self.path in ["/v1/chat/completions", "/chat/completions"]:
            messages = payload.get("messages", [])
            tools = payload.get("tools", []) or []
            model = payload.get("model", "gemini-browser")
            execute = payload.get("execute", True)
            timeout = payload.get("timeout", 180)
            is_stream = payload.get("stream", False)

            # Extrahiere verfügbare Tool-Namen + kompakte Signatur
            tool_names = set()
            tool_sigs = []
            for t in tools[:15]:
                fn = t.get("function", {}) if isinstance(t, dict) else {}
                t_name = fn.get("name", "")
                if t_name:
                    tool_names.add(t_name)
                    props = list((fn.get("parameters") or {}).get("properties", {}).keys())
                    tool_sigs.append(f"- {t_name}({', '.join(props[:5])})")

            # Prüfe ob die letzten Nachrichten Tool-Ergebnisse von Hermes sind
            last_user_idx = -1
            for idx, m in enumerate(messages):
                if m.get("role") == "user":
                    last_user_idx = idx
            tool_results = [
                m for m in messages[last_user_idx + 1:]
                if m.get("role") == "tool"
            ] if last_user_idx >= 0 else []

            user_texts = [m.get("content", "") for m in messages if m.get("role") == "user"]
            system_texts = [m.get("content", "") for m in messages if m.get("role") == "system"]

            raw_user_msg = user_texts[-1] if user_texts else ""
            if not raw_user_msg.strip() and system_texts:
                raw_user_msg = system_texts[0][:500]

            if not raw_user_msg.strip():
                self._send_json(400, {"error": "Keine Nutzernachricht in messages gefunden"})
                return

            if tool_results:
                tr_text = "\n".join(
                    f"[Tool-Ergebnis '{m.get('name', 'tool')}']:\n{str(m.get('content', ''))[:3000]}"
                    for m in tool_results
                )
                effective_input = (
                    f"Ursprüngliche Frage: {raw_user_msg}\n\n"
                    f"{tr_text}\n\n"
                    "Fasse dieses echte Tool-Ergebnis nun klar und präzise für den Nutzer zusammen (oder rufe bei Bedarf ein weiteres Tool auf)."
                )
                use_cache = False
            else:
                effective_input = raw_user_msg
                use_cache = True

            tools_hint = ""
            if tool_sigs:
                tools_hint = (
                    "\n[Verfügbare Tools]:\n"
                    + "\n".join(tool_sigs)
                    + '\nWenn du zur Beantwortung ein Tool ausführen musst, antworte AUSSCHLIESSLICH mit genau einem JSON-Block ohne weiteren Text:\n'
                    '```json\n{"tool_call": {"name": "TOOL_NAME", "arguments": {"param": "wert"}}}\n```\n'
                    "Wenn du kein Tool brauchst oder schon das Tool-Ergebnis vorliegt, antworte normal als Text."
                )

            # Configurable system context (override via BRIDGE_SYSTEM_PROMPT env var)
            default_sys = (
                "[System-Kontext: Du bist ein technischer Assistent über die lokale Browser-Bridge. "
                "Antworte direkt, praxisnah und ohne Standard-Floskeln wie 'Als Cloud-KI kann ich nicht...'. "
                "ERFINDE NIEMALS Dateipfade, Skripte oder Messwerte! Wenn lokale System-/Netzwerkdaten fehlen und noch kein [Tool-Ergebnis] vorliegt, rufe zwingend ein Tool auf.]"
            )
            sys_ctx = os.getenv("BRIDGE_SYSTEM_PROMPT", default_sys)
            task_prompt = f"{sys_ctx}{tools_hint}\n\n{effective_input}"
            cache_key = hashlib.sha256(effective_input.encode("utf-8")).hexdigest()

            # 0. Cache-Hit prüfen (nur für normale User-Anfragen, nicht bei Tool-Loops)
            if use_cache:
                with CACHE_LOCK:
                    cached = RESPONSE_CACHE.get(cache_key)
                    if cached and (time.time() - cached["ts"] < CACHE_TTL_SEC):
                        res_content = cached["content"]
                        print(f"[\033[96mCACHE-HIT\033[0m] Identischer Prompt aus Cache bedient ({len(res_content)} Zeichen)")
                        self._send_openai_completion(
                            f"chatcmpl-gemini-cache-{int(time.time() * 1000)}",
                            model,
                            is_stream,
                            res_content,
                            estimate_tokens(task_prompt),
                            estimate_tokens(res_content),
                        )
                        return

            # Schneller Check: Falls Tabs verbunden sind, aber kein Worker aktiv ist, nicht 180s aufstauen
            tabs_now = cleanup_and_get_tabs()
            if tabs_now and not any(t.get("role") in ("worker", "coder") for t in tabs_now.values()):
                self._send_json(503, {
                    "error": {
                        "message": "Kein aktiver Gemini Worker-Tab im Browser (aktuell nur: " + ", ".join(t.get("role", "?") for t in tabs_now.values()) + "). Bitte Tab auf Worker stellen.",
                        "type": "no_worker_tab",
                        "code": 503
                    }
                })
                return

            # Strikte Serialisierung: Falls parallele Requests kommen, warten sie in der Queue
            with MISSION_LOCK:
                mission_id = str(int(time.time() * 1000))
                MISSION_COMPLETED_EVENT.clear()
                DEBATE_STATE["active_task"] = task_prompt
                DEBATE_STATE["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                DEBATE_STATE["mission_source"] = "litellm"
                DEBATE_STATE["mission_started_ts"] = time.time()
                DEBATE_STATE["final_result"] = ""
                DEBATE_STATE["pending_mission"] = {
                    "id": mission_id,
                    "task": task_prompt,
                    "rounds": 1,
                    "execute": execute,
                    "status": "pending"
                }

                print(f"\n[\033[93mLITELLM / HERMES\033[0m] OpenAI-Anfrage empfangen (Modell: {model}, Stream: {is_stream}): {raw_user_msg[:80]}...")

                # Warten bis Gemini die Mission beendet hat
                start_wait = time.time()
                finished = MISSION_COMPLETED_EVENT.wait(timeout=timeout)

                if finished and DEBATE_STATE.get("final_result"):
                    res_content = DEBATE_STATE["final_result"]
                    elapsed_ms = int((time.time() - start_wait) * 1000)
                    duration = round(elapsed_ms / 1000.0, 1)
                    print(f"[\033[92mFERTIG AN LITELLM\033[0m] Gemini Tandem liefert Antwort ({duration}s)")
                    prompt_tokens = estimate_tokens(task_prompt)
                    completion_tokens = estimate_tokens(res_content)

                    # Prüfe ob Gemini einen tool_call im JSON-Format zurückgegeben hat
                    parsed_tc = None
                    if tool_names and "tool_call" in res_content:
                        start_idx = res_content.find("{")
                        end_idx = res_content.rfind("}")
                        if start_idx != -1 and end_idx > start_idx:
                            cand_str = res_content[start_idx:end_idx + 1]
                            try:
                                obj = json.loads(cand_str)
                                tc_obj = obj.get("tool_call") or {}
                                tc_name = tc_obj.get("name", "")
                                tc_args = tc_obj.get("arguments", {})
                                if tc_name in tool_names and isinstance(tc_args, dict):
                                    parsed_tc = (tc_name, tc_args)
                                    print(f"[\033[95mTOOL-CALL\033[0m] Gemini ruft Hermes-Tool '{tc_name}' auf: {tc_args}")
                            except Exception:
                                pass

                    if use_cache and not parsed_tc:
                        with CACHE_LOCK:
                            RESPONSE_CACHE[cache_key] = {"ts": time.time(), "content": res_content}
                            if len(RESPONSE_CACHE) > 50:
                                oldest = min(RESPONSE_CACHE.items(), key=lambda kv: kv[1]["ts"])[0]
                                del RESPONSE_CACHE[oldest]
                    record_model_run(
                        provider=DEBATE_STATE.get("browser_provider", "gemini"),
                        model=DEBATE_STATE.get("browser_model") or model,
                        capability=DEBATE_STATE.get("browser_capability", "chat"),
                        source="litellm",
                        status="ok",
                        latency_ms=elapsed_ms,
                        prompt_text=effective_input,
                        completion_text=res_content,
                    )
                    self._send_openai_completion(
                        f"chatcmpl-gemini-{mission_id}",
                        model,
                        is_stream,
                        res_content,
                        prompt_tokens,
                        completion_tokens,
                        tool_call=parsed_tc,
                    )
                else:
                    elapsed_ms = int((time.time() - start_wait) * 1000)
                    record_model_run(
                        provider=DEBATE_STATE.get("browser_provider", "gemini"),
                        model=DEBATE_STATE.get("browser_model") or model,
                        capability=DEBATE_STATE.get("browser_capability", "chat"),
                        source="litellm",
                        status="timeout",
                        latency_ms=elapsed_ms,
                        prompt_text=raw_user_msg,
                        completion_text="",
                    )
                    print(f"[\033[91mTIMEOUT AN LITELLM\033[0m] Gemini hat innerhalb von {timeout}s nicht geantwortet.")
                    self._send_json(504, {
                        "error": {
                            "message": f"Gemini Browser Tandem Timeout nach {timeout}s. Bitte prüfen, ob Tab 1 und Tab 2 im Browser aktiv sind.",
                            "type": "timeout_error",
                            "code": 504
                        }
                    })
            return

        # ------------------------------------------------------------------
        # 0b. /generate_image - Bildgenerierung über den Browser-Bild-Tab
        # ------------------------------------------------------------------
        elif self.path == "/generate_image":
            prompt = (payload.get("prompt") or "").strip()
            timeout = int(payload.get("timeout", 180))
            if not prompt:
                self._send_json(400, {"error": "No prompt provided"})
                return

            with MISSION_LOCK:
                mission_id = f"img_{int(time.time() * 1000)}"
                MISSION_COMPLETED_EVENT.clear()
                DEBATE_STATE["last_image"] = None
                DEBATE_STATE["active_task"] = prompt
                DEBATE_STATE["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                DEBATE_STATE["mission_source"] = "image_skill"
                DEBATE_STATE["mission_started_ts"] = time.time()
                DEBATE_STATE["final_result"] = ""
                DEBATE_STATE["pending_mission"] = {
                    "id": mission_id,
                    "task": prompt,
                    "capability": "image",
                    "rounds": 1,
                    "execute": False,
                    "status": "pending"
                }

                print(f"\n[\033[95mBILD-AUFTRAG\033[0m] Prompt: {prompt[:90]}...")
                start_wait = time.time()
                finished = MISSION_COMPLETED_EVENT.wait(timeout=timeout)
                elapsed_ms = int((time.time() - start_wait) * 1000)
                last_img = DEBATE_STATE.get("last_image")

                if finished and last_img and last_img.get("file_path"):
                    record_model_run(
                        provider=DEBATE_STATE.get("browser_provider", "gemini"),
                        model=DEBATE_STATE.get("browser_model") or "gemini-image",
                        capability="image",
                        source="image_skill",
                        status="ok",
                        latency_ms=elapsed_ms,
                        prompt_text=prompt,
                        completion_text=last_img.get("file_path", ""),
                    )
                    self._send_json(200, {
                        "success": True,
                        "file_path": last_img["file_path"],
                        "filename": last_img["filename"],
                        "image_url": f"http://{os.getenv('BRIDGE_PUBLIC_HOST', 'localhost')}:{PORT}/images/{last_img['filename']}",
                        "mime_type": last_img.get("mime_type", "image/png"),
                        "image_base64": last_img.get("image_base64", ""),
                        "text": DEBATE_STATE.get("final_result", ""),
                        "latency_ms": elapsed_ms,
                    })
                elif finished:
                    record_model_run(
                        provider=DEBATE_STATE.get("browser_provider", "gemini"),
                        model=DEBATE_STATE.get("browser_model") or "gemini-image",
                        capability="image",
                        source="image_skill",
                        status="error",
                        latency_ms=elapsed_ms,
                        prompt_text=prompt,
                        completion_text=DEBATE_STATE.get("final_result", ""),
                    )
                    self._send_json(500, {
                        "success": False,
                        "error": "Kein Bild in der Gemini-Antwort gefunden",
                        "text": DEBATE_STATE.get("final_result", ""),
                    })
                else:
                    record_model_run(
                        provider=DEBATE_STATE.get("browser_provider", "gemini"),
                        model=DEBATE_STATE.get("browser_model") or "gemini-image",
                        capability="image",
                        source="image_skill",
                        status="timeout",
                        latency_ms=elapsed_ms,
                        prompt_text=prompt,
                        completion_text="",
                    )
                    self._send_json(504, {"success": False, "error": f"Timeout nach {timeout}s"})
            return

        # ------------------------------------------------------------------
        # 1. /submit_mission - Neue Mission vom Dashboard absenden
        # ------------------------------------------------------------------
        elif self.path == "/submit_mission":
            task = payload.get("task", "").strip()
            rounds = payload.get("rounds", 2)
            execute = payload.get("execute", True)

            if not task:
                self._send_json(400, {"error": "No task provided"})
                return

            mission_id = str(int(time.time()))
            MISSION_COMPLETED_EVENT.clear()
            DEBATE_STATE["active_task"] = task
            DEBATE_STATE["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            DEBATE_STATE["mission_source"] = "dashboard"
            DEBATE_STATE["mission_started_ts"] = time.time()
            DEBATE_STATE["final_result"] = ""
            DEBATE_STATE["pending_mission"] = {
                "id": mission_id,
                "task": task,
                "rounds": rounds,
                "execute": execute,
                "status": "pending"
            }

            print(f"\n[\033[93mMISSION\033[0m] Neue Aufgabe eingereicht: {task}")
            self._send_json(200, {"success": True, "mission_id": mission_id})

        # ------------------------------------------------------------------
        # 2. /claim_mission - Tab 1 meldet Übernahme der Mission
        # ------------------------------------------------------------------
        elif self.path == "/claim_mission":
            mission_id = payload.get("id")
            if DEBATE_STATE.get("pending_mission") and DEBATE_STATE["pending_mission"]["id"] == mission_id:
                DEBATE_STATE["pending_mission"]["status"] = "in_progress"
            self._send_json(200, {"success": True})

        # ------------------------------------------------------------------
        # 3. /finish_mission - Speichert finale Antwort für das Dashboard
        # ------------------------------------------------------------------
        elif self.path == "/finish_mission":
            result = payload.get("result", "")
            img_b64 = payload.get("image_base64", "")
            img_mime = payload.get("image_mime", "image/png")
            if img_b64:
                if "," in img_b64 and img_b64.startswith("data:"):
                    header, img_b64 = img_b64.split(",", 1)
                    if "jpeg" in header or "jpg" in header:
                        img_mime = "image/jpeg"
                    elif "webp" in header:
                        img_mime = "image/webp"
                ext = ".png"
                if "jpeg" in img_mime or "jpg" in img_mime:
                    ext = ".jpg"
                elif "webp" in img_mime:
                    ext = ".webp"
                fname = f"gemini_{int(time.time() * 1000)}{ext}"
                fpath = os.path.join(IMAGES_DIR, fname)
                try:
                    with open(fpath, "wb") as f:
                        f.write(base64.b64decode(img_b64))
                    DEBATE_STATE["last_image"] = {
                        "file_path": fpath,
                        "filename": fname,
                        "mime_type": img_mime,
                        "image_base64": img_b64,
                    }
                    print(f"[\033[92mBILD GESPEICHERT\033[0m] {fpath}")
                except Exception as e:
                    print(f"[\033[91mBILD-FEHLER\033[0m] {e}")

            DEBATE_STATE["final_result"] = result
            DEBATE_STATE["pending_mission"] = None
            if DEBATE_STATE.get("mission_source") == "dashboard" and DEBATE_STATE.get("mission_started_ts", 0) > 0:
                elapsed_ms = int((time.time() - DEBATE_STATE["mission_started_ts"]) * 1000)
                record_model_run(
                    provider=DEBATE_STATE.get("browser_provider", "gemini"),
                    model=DEBATE_STATE.get("browser_model", "unknown"),
                    capability=DEBATE_STATE.get("browser_capability", "chat"),
                    source="dashboard",
                    status="ok",
                    latency_ms=elapsed_ms,
                    prompt_text=DEBATE_STATE.get("active_task", ""),
                    completion_text=result,
                )
                DEBATE_STATE["mission_started_ts"] = 0.0
            MISSION_COMPLETED_EVENT.set()
            print(f"\n[\033[92mFERTIG\033[0m] Finale Antwort empfangen und im Dashboard bereitgestellt!")
            self._send_json(200, {"success": True})

        # ------------------------------------------------------------------
        # 4. /log_event - Speichert Agenten-Events für Dashboard und Markdown
        # ------------------------------------------------------------------
        elif self.path == "/log_event":
            role = payload.get("role", "info").lower()
            round_no = payload.get("round", 1)
            title = payload.get("title", "")
            content = payload.get("content", "")
            task = payload.get("task", "")

            if task:
                DEBATE_STATE["active_task"] = task
                if not DEBATE_STATE["started_at"]:
                    DEBATE_STATE["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            event = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "role": role,
                "round": round_no,
                "title": title,
                "content": content
            }
            DEBATE_STATE["events"].append(event)
            append_to_markdown_log(event)

            color = {
                "coder": "\033[94m",
                "reviewer": "\033[95m",
                "terminal": "\033[92m",
                "final": "\033[93m"
            }.get(role, "\033[0m")
            print(f"{color}[Runde {round_no} - {role.upper()}]\033[0m {title}")

            self._send_json(200, {"success": True, "logged": True})

        # ------------------------------------------------------------------
        # 5. /clear_events - Verlauf zurücksetzen
        # ------------------------------------------------------------------
        elif self.path == "/clear_events":
            DEBATE_STATE["events"] = []
            DEBATE_STATE["active_task"] = ""
            DEBATE_STATE["started_at"] = ""
            DEBATE_STATE["final_result"] = ""
            DEBATE_STATE["pending_mission"] = None
            MISSION_COMPLETED_EVENT.clear()
            DEBATE_STATE["pending_mission"] = None
            try:
                with open(LOG_MD_PATH, "w", encoding="utf-8") as f:
                    f.write("# 🎯 Gemini Multi-Agent Debatten-Protokoll\n\n*Verlauf geleert am " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "*\n\n---\n")
            except Exception as e:
                pass
            print("\n[\033[93mCLEAR\033[0m] Debatten-Verlauf geleert.")
            self._send_json(200, {"success": True, "cleared": True})

        # ------------------------------------------------------------------
        # 5b. /model_status - Browser-Modell & Limit-Warnung melden
        # ------------------------------------------------------------------
        elif self.path == "/model_status":
            current = payload.get("current_model", "").strip()
            available = payload.get("available_models", [])
            limit_warn = payload.get("limit_warning", None)
            provider = payload.get("provider", "").strip() or "gemini"
            capability = payload.get("capability", "").strip()
            cooldown_info = payload.get("cooldown_info", None)
            if provider:
                DEBATE_STATE["browser_provider"] = provider
            if capability:
                DEBATE_STATE["browser_capability"] = capability
            if isinstance(cooldown_info, dict) and cooldown_info.get("limited_model"):
                set_model_cooldown(
                    provider,
                    cooldown_info["limited_model"],
                    cooldown_info.get("retry_after_ts", 0),
                    cooldown_info.get("retry_after_str", ""),
                    cooldown_info.get("reason", limit_warn or "Limit erreicht"),
                )
                if DEBATE_STATE.get("desired_model") and cooldown_info["limited_model"].lower() in DEBATE_STATE["desired_model"].lower():
                    DEBATE_STATE["desired_model"] = ""
            if payload.get("clear_desired"):
                DEBATE_STATE["desired_model"] = ""
            if current:
                DEBATE_STATE["browser_model"] = current
                if DEBATE_STATE.get("desired_model") and DEBATE_STATE["desired_model"].lower() in current.lower():
                    DEBATE_STATE["desired_model"] = ""
                if not limit_warn and not cooldown_info:
                    clear_model_cooldown(current)
            if available:
                DEBATE_STATE["available_models"] = available
            if limit_warn is not None:
                DEBATE_STATE["limit_warning"] = limit_warn
            self._send_json(200, {
                "success": True,
                "desired_model": DEBATE_STATE.get("desired_model", ""),
                "model_cooldowns": DEBATE_STATE.get("model_cooldowns", {})
            })

        # ------------------------------------------------------------------
        # 5c. /set_model - Manueller Modellwechsel vom Dashboard anstoßen
        # ------------------------------------------------------------------
        elif self.path == "/set_model":
            target_model = payload.get("model", "").strip()
            DEBATE_STATE["desired_model"] = target_model
            print(f"[\033[96mMODELL-WECHSEL\033[0m] Manuelle Anfrage aus Dashboard: Wechsel zu '{target_model}'")
            self._send_json(200, {"success": True, "desired_model": target_model})

        # ------------------------------------------------------------------
        # 6. /test_code - Code ausführen (ReAct Action)
        # ------------------------------------------------------------------
        elif self.path == "/test_code":
            lang = payload.get("language", "python").lower()
            code = payload.get("code", "")
            execute = payload.get("execute", True)

            if not code.strip():
                self._send_json(400, {"error": "No code provided"})
                return

            ext_map = {
                "python": ".py", "py": ".py",
                "javascript": ".js", "js": ".js", "node": ".js",
                "bash": ".sh", "sh": ".sh", "shell": ".sh"
            }
            ext = ext_map.get(lang, ".txt")
            temp_file = os.path.join(SCRATCH_DIR, f"check_{int(time.time()*1000)}{ext}")

            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(code)

            start_time = time.time()

            # Ausführen oder nur kompilieren
            check_cmd = None
            if lang in ["python", "py"]:
                check_cmd = f"python3 {temp_file}" if execute else f"python3 -m py_compile {temp_file}"
            elif lang in ["javascript", "js", "node"]:
                check_cmd = f"node {temp_file}" if execute else f"node -c {temp_file}"
            elif lang in ["bash", "sh", "shell"]:
                check_cmd = f"bash {temp_file}" if execute else f"bash -n {temp_file}"

            if not check_cmd:
                self._send_json(400, {"error": f"Unsupported language: {lang}"})
                return

            try:
                res = subprocess.run(
                    check_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=25
                )
                duration = round((time.time() - start_time) * 1000, 1)

                is_valid = (res.returncode == 0)
                status_color = "\033[92mERFOLGREICH AUSGEFÜHRT\033[0m" if is_valid else "\033[91mFEHLER\033[0m"
                print(f"[\033[96mACTION\033[0m] {lang} -> [{status_color}] ({duration}ms)")

                term_event = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "role": "terminal",
                    "round": payload.get("round", 1),
                    "title": f"Terminal {'Ausführung' if execute else 'Syntax-Check'} ({lang}): {'✅ Erfolgreich' if is_valid else '❌ Fehler'}",
                    "content": res.stdout if is_valid else res.stderr
                }
                DEBATE_STATE["events"].append(term_event)
                append_to_markdown_log(term_event)

                self._send_json(200, {
                    "valid": is_valid,
                    "exit_code": res.returncode,
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "duration_ms": duration,
                    "temp_file": temp_file
                })
            except subprocess.TimeoutExpired:
                self._send_json(408, {"valid": False, "stderr": "Ausführung hat das Timeout (25s) überschritten."})
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        else:
            self._send_json(404, {"error": "Endpoint not found"})

    def log_message(self, format, *args):
        return

def run_server():
    server_address = (HOST, PORT)
    httpd = http.server.ThreadingHTTPServer(server_address, GeminiBridgeHandler)
    print("=" * 65)
    print(f"🎯 \033[1;32mGemini Multi-Agent Mission Control & OpenAI Bridge v3.1 läuft!\033[0m")
    print(f"📍 \033[1;34mDashboard & Mission-Eingabe:\033[0m http://{HOST}:{PORT}/dashboard")
    print(f"🤖 \033[1;35mOpenAI / LiteLLM API:\033[0m        http://{HOST}:{PORT}/v1/chat/completions")
    print(f"📝 \033[1;33mMarkdown-Protokoll:\033[0m          {LOG_MD_PATH}")
    print("=" * 65)
    print("Bereit für Missionen via Dashboard oder Hermes / LiteLLM!\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server beendet.")
        httpd.server_close()

if __name__ == "__main__":
    run_server()
