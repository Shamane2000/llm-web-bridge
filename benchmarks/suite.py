#!/usr/bin/env python3
"""Reproducible Benchmark Suite for gemini-browser & claude-browser via Hermes CLI."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

JARVIS_HOST = "user@hermes-host"
HERMES_BIN = "hermes"
HERMES_HOME_DEFAULT = "~/.hermes/profiles/default"
BRIDGE_URL = "http://localhost:8765"
RESULTS_ROOT = Path(__file__).resolve().parent / "results"


def run_local_http_json(url: str, method: str = "GET", payload: dict | None = None, timeout: int = 10) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body
    except Exception as exc:
        return 0, str(exc)


def run_ssh(cmd: str, timeout: int = 30) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", JARVIS_HOST, cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_hermes_chat(
    model: str,
    prompt: str,
    toolsets: str = "terminal,file",
    hermes_home: str = HERMES_HOME_DEFAULT,
    timeout: int = 240,
) -> dict[str, Any]:
    """Execute one-shot hermes chat on Remote-Host and return timing + outputs."""
    escaped_prompt = prompt.replace("'", "'\"'\"'")
    remote_cmd = (
        f"export HERMES_HOME='{hermes_home}'; "
        f"{HERMES_BIN} chat -Q -t '{toolsets}' -m '{model}' -q '{escaped_prompt}'"
    )
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", JARVIS_HOST, remote_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = round(time.monotonic() - start, 2)
        return {
            "exit_code": proc.returncode,
            "duration_s": duration,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - start, 2)
        return {
            "exit_code": -1,
            "duration_s": duration,
            "stdout": (exc.stdout or "").strip() if isinstance(exc.stdout, str) else "",
            "stderr": f"TIMEOUT after {timeout}s",
            "timed_out": True,
        }


def prepare_payload_fixture() -> None:
    """Create a deterministic 350-line log file on Remote-Host with 3 hidden error codes."""
    py_script = (
        "from pathlib import Path\n"
        "lines = []\n"
        "markers = {42: 'CRITICAL_ERR_CODE=X7K9', 187: 'CRITICAL_ERR_CODE=X4M2', 319: 'CRITICAL_ERR_CODE=X9Q1'}\n"
        "for i in range(1, 351):\n"
        "    if i in markers:\n"
        "        lines.append(f'2026-11-16T00:{i%60:02d}:00Z [ERROR] worker={i%7} {markers[i]} detail=simulated_fault_{i}')\n"
        "    else:\n"
        "        lines.append(f'2026-11-16T00:{i%60:02d}:00Z [INFO] worker={i%7} heartbeat ok seq={i} latency_ms={(i*13)%97}')\n"
        "Path('/tmp/bridge_bench_payload.log').write_text('\\n'.join(lines) + '\\n')\n"
    )
    rc, _, err = run_ssh(f"python3 - <<'EOF'\n{py_script}EOF")
    if rc != 0:
        raise RuntimeError(f"Failed to prepare payload fixture on Remote-Host: {err}")


def safe_tag(model: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", model)


# ---------------------------------------------------------------------------
# Validators (return (passed: bool, details: str))
# ---------------------------------------------------------------------------

def validate_t1_health(model: str, res: dict[str, Any]) -> tuple[bool, str]:
    out = res["stdout"]
    checks = {
        "litellm-router": "litellm-router" in out,
        "hermes-dashboard": "hermes-dashboard" in out,
        "hermes-gateway": "hermes-gateway" in out,
        "bridge_url": "127.0.0.1:8765" in out,
        "ram_total_3910": "3910" in out,
    }
    passed = res["exit_code"] == 0 and all(checks.values())
    missing = [k for k, ok in checks.items() if not ok]
    return passed, "OK" if passed else f"Missing: {missing}"


def validate_t2_multiturn(model: str, res: dict[str, Any]) -> tuple[bool, str]:
    out = res["stdout"]
    checks = {
        "mentions_gemini_profile": "gemini" in out.lower(),
        "mentions_litellm": "litellm" in out.lower(),
        "has_numbers": bool(re.search(r"\b\d+\b", out)),
        "non_empty": len(out) > 80,
    }
    passed = res["exit_code"] == 0 and all(checks.values())
    missing = [k for k, ok in checks.items() if not ok]
    return passed, "OK" if passed else f"Failed checks: {missing}"


def validate_t3_payload(model: str, res: dict[str, Any]) -> tuple[bool, str]:
    out = res["stdout"]
    codes = ["X7K9", "X4M2", "X9Q1"]
    found = [c for c in codes if c in out]
    passed = res["exit_code"] == 0 and len(found) == 3
    return passed, f"Found {len(found)}/3 codes ({found})"


def validate_t4_code_edit(model: str, res: dict[str, Any]) -> tuple[bool, str]:
    work_dir = f"/tmp/bridge_bench_{safe_tag(model)}"
    rc, out, err = run_ssh(
        f"test -f {work_dir}/calc.py && test -f {work_dir}/test_calc.py && "
        f"cd {work_dir} && python3 -m unittest -v test_calc.py"
    )
    passed = res["exit_code"] == 0 and rc == 0 and "OK" in (out + err)
    detail = "unittest OK on Remote-Host" if passed else f"rc={rc} out={out.strip()} err={err.strip()}"
    return passed, detail


# ---------------------------------------------------------------------------
# Test Case Definitions
# ---------------------------------------------------------------------------

def build_test_cases() -> dict[str, dict[str, Any]]:
    return {
        "t1_health_groundtruth": {
            "title": "T1: Live-Health & Config Audit (Baseline)",
            "toolsets": "terminal,file",
            "timeout": 360,
            "prompt": lambda model: (
                "Führe einen Read-Only-Check auf dem aktuellen Host (Remote-Host) über deine System-Tools durch "
                "und beantworte exakt diese 4 Punkte (keine Werte schätzen oder erfinden):\n"
                "1. RAM & Swap: Wie viel MB sind aktuell belegt und frei (free -m)?\n"
                "2. Docker: Welche Container laufen gerade (Name + Status aus docker ps)?\n"
                "3. Hermes-Services: Welche User-Services mit hermes sind aktiv (systemctl --user list-units '*hermes*' --no-pager)?\n"
                "4. LiteLLM-Routing: Auf welche api_base-URL zeigen gemini-browser und claude-browser in ~/litellm/config.yaml?\n"
                "Gib das Ergebnis als kompakte Markdown-Liste (ohne breite Tabellen) mit einem kurzen Fazit zurück."
            ),
            "validator": validate_t1_health,
        },
        "t2_multiturn_scavenger": {
            "title": "T2: Multi-Turn Kontext-Tiefe (4-Schritt-Schnitzeljagd)",
            "toolsets": "terminal,file",
            "timeout": 420,
            "prompt": lambda model: (
                "Erledige diese 4 Schritte nacheinander mit deinen Tools (nichts raten):\n"
                "1. Lies ~/.hermes/profiles/gemini/profile.yaml und nenne den Profilnamen.\n"
                "2. Zähle, wie viele Einträge (Ordner/Dateien) in ~/.hermes/profiles/gemini/skills liegen.\n"
                "3. Ermittle die Zeilenanzahl von ~/litellm/config.yaml und wie viele model_name-Einträge dort definiert sind.\n"
                "4. Prüfe mit `df -h /`, wie viel Prozent der Root-Partition belegt sind.\n"
                "Fasse alle 4 Fakten kompakt in 4 Bulletpoints zusammen."
            ),
            "validator": validate_t2_multiturn,
        },
        "t3_payload_stress": {
            "title": "T3: Payload-Stress (350-Zeilen-Log nach 3 Fehlercodes durchsuchen)",
            "toolsets": "terminal,file",
            "timeout": 420,
            "setup": prepare_payload_fixture,
            "prompt": lambda model: (
                "Lies die Datei /tmp/bridge_bench_payload.log (350 Zeilen) vollständig oder filtere sie gezielt "
                "mit deinen Tools, ohne etwas zu erfinden.\n"
                "Finde ALLE Zeilen mit `CRITICAL_ERR_CODE=` und gib zu jeder Zeile exakt:\n"
                "- Zeilennummer\n"
                "- `CRITICAL_ERR_CODE`\n"
                "- `worker`-ID\n"
                "zurück."
            ),
            "validator": validate_t3_payload,
        },
        "t4_code_write_edit": {
            "title": "T4: Code-Erstellung, Escaping & Edit-Cycle (write_file + edit_file + unittest)",
            "toolsets": "terminal,file",
            "timeout": 420,
            "setup_model": lambda model: run_ssh(f"rm -rf /tmp/bridge_bench_{safe_tag(model)} && mkdir -p /tmp/bridge_bench_{safe_tag(model)}"),
            "prompt": lambda model: (
                f"Arbeite im Verzeichnis /tmp/bridge_bench_{safe_tag(model)} auf Remote-Host:\n"
                "1. Erstelle `calc.py` mit einer Funktion `celsius_to_fahrenheit(c)` (Formel `c * 9 / 5 + 32`) "
                "und `clamp(val, low, high)`.\n"
                "2. Erstelle `test_calc.py` (mit `unittest`) mit mindestens 4 Assertions, inkl. negativer Zahlen (-40 -> -40.0).\n"
                "3. Führe `python3 -m unittest -v test_calc.py` im Verzeichnis aus.\n"
                "4. Ergänze anschließend in `calc.py` einen Docstring für `clamp` (z. B. via Patch/Edit) und führe die Tests nochmal aus.\n"
                "Melde am Ende kurz den exakten Test-Output."
            ),
            "validator": validate_t4_code_edit,
        },
    }


def run_single_test(
    test_id: str,
    spec: dict[str, Any],
    model: str,
    hermes_home: str,
) -> dict[str, Any]:
    if "setup" in spec:
        spec["setup"]()
    if "setup_model" in spec:
        spec["setup_model"](model)

    prompt = spec["prompt"](model)
    res = run_hermes_chat(
        model=model,
        prompt=prompt,
        toolsets=spec["toolsets"],
        hermes_home=hermes_home,
        timeout=spec["timeout"],
    )
    passed, detail = spec["validator"](model, res)
    return {
        "test_id": test_id,
        "title": spec["title"],
        "model": model,
        "passed": passed,
        "detail": detail,
        "exit_code": res["exit_code"],
        "duration_s": res["duration_s"],
        "timed_out": res["timed_out"],
        "output_chars": len(res["stdout"]),
        "stdout": res["stdout"],
        "stderr": res["stderr"],
    }


def run_parallel_cross(models: list[str], cases: dict[str, dict[str, Any]], hermes_home: str) -> list[dict[str, Any]]:
    spec = cases["t1_health_groundtruth"]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futs = {
            pool.submit(run_single_test, "t5_parallel_cross", spec, m, hermes_home): m
            for m in models
        }
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            r["title"] = "T5: Cross-Provider Parallel-Run (T1 gleichzeitig)"
            results.append(r)
    return results


def write_reports(run_dir: Path, records: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.json").write_text(
        json.dumps({"meta": meta, "records": records}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    for rec in records:
        fname = f"{rec['test_id']}__{safe_tag(rec['model'])}.txt"
        body = (
            f"# {rec['title']} | {rec['model']}\n"
            f"passed={rec['passed']} exit_code={rec['exit_code']} duration_s={rec['duration_s']} detail={rec['detail']}\n\n"
            f"=== STDOUT ===\n{rec['stdout']}\n\n=== STDERR ===\n{rec['stderr']}\n"
        )
        (run_dir / fname).write_text(body, encoding="utf-8")

    lines = [
        f"# Hermes Browser-Bridge Benchmark Report",
        f"- **Timestamp:** {meta['timestamp']}",
        f"- **Profile (`HERMES_HOME`):** `{meta['hermes_home']}`",
        f"- **Models:** {', '.join(meta['models'])}",
        "",
        "| Test | Model | Status | Dauer | Output-Zeichen | Detail |",
        "| :--- | :--- | :--- | ---: | ---: | :--- |",
    ]
    for rec in records:
        status = "PASS" if rec["passed"] else "FAIL"
        lines.append(
            f"| `{rec['test_id']}` | `{rec['model']}` | **{status}** | {rec['duration_s']}s | {rec['output_chars']} | {rec['detail']} |"
        )
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    cases = build_test_cases()
    parser = argparse.ArgumentParser(description="Hermes Browser-Bridge Benchmark Suite")
    parser.add_argument("--models", nargs="+", default=["gemini-browser", "claude-browser"], help="Models to test")
    parser.add_argument(
        "--tests",
        nargs="+",
        default=list(cases.keys()),
        choices=list(cases.keys()) + ["t5_parallel_cross"],
        help="Tests to run",
    )
    parser.add_argument("--hermes-home", default=HERMES_HOME_DEFAULT, help="HERMES_HOME on Remote-Host")
    parser.add_argument("--init-before", action="store_true", help="Send /init to bridge providers before running")
    parser.add_argument("--dry-run", action="store_true", help="Check prerequisites and print plan without executing tests")
    args = parser.parse_args()

    # 1. Preflight check: Bridge & Models
    code, models_resp = run_local_http_json(f"{BRIDGE_URL}/v1/models")
    if code != 200 or not isinstance(models_resp, dict):
        print(f"[ERROR] Bridge not reachable at {BRIDGE_URL}/v1/models (code={code}: {models_resp})", file=sys.stderr)
        return 2
    available = {m.get("id") for m in models_resp.get("data", [])}
    missing_models = [m for m in args.models if m not in available]
    if missing_models:
        print(f"[ERROR] Requested models not registered on bridge: {missing_models} (available: {sorted(available)})", file=sys.stderr)
        return 2

    # 2. Preflight check: SSH to Remote-Host
    rc, _, err = run_ssh(f"test -x {HERMES_BIN} && test -d '{args.hermes_home}'")
    if rc != 0:
        print(f"[ERROR] Remote-Host preflight failed (rc={rc}): {err}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"[DRY-RUN OK] Bridge models={sorted(available)} | Target models={args.models} | Tests={args.tests} | HERMES_HOME={args.hermes_home}")
        return 0

    if args.init_before:
        for m in args.models:
            provider = m.split("-")[0]
            ic, ir = run_local_http_json(f"{BRIDGE_URL}/init", method="POST", payload={"provider": provider}, timeout=45)
            print(f"[INIT] {provider}: HTTP {ic} -> {ir}")

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RESULTS_ROOT / ts
    records: list[dict[str, Any]] = []

    seq_tests = [t for t in args.tests if t != "t5_parallel_cross"]
    for t_id in seq_tests:
        spec = cases[t_id]
        for model in args.models:
            print(f"[RUN] {t_id} -> {model} ...", flush=True)
            rec = run_single_test(t_id, spec, model, args.hermes_home)
            status = "PASS" if rec["passed"] else "FAIL"
            print(f"[{status}] {t_id} -> {model} ({rec['duration_s']}s) | {rec['detail']}", flush=True)
            records.append(rec)

    if "t5_parallel_cross" in args.tests and len(args.models) >= 2:
        print(f"[RUN] t5_parallel_cross -> {args.models} ...", flush=True)
        p_recs = run_parallel_cross(args.models, cases, args.hermes_home)
        for rec in p_recs:
            status = "PASS" if rec["passed"] else "FAIL"
            print(f"[{status}] t5_parallel_cross -> {rec['model']} ({rec['duration_s']}s) | {rec['detail']}", flush=True)
            records.append(rec)

    meta = {
        "timestamp": ts,
        "hermes_home": args.hermes_home,
        "models": args.models,
        "tests": args.tests,
    }
    write_reports(run_dir, records, meta)
    print(f"\nReport saved to: {run_dir / 'summary.md'}")
    return 0 if all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
