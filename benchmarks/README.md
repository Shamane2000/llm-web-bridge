# Hermes Browser-Bridge Benchmark Suite

Reproduzierbare Test-Suite für `gemini-browser` und `claude-browser` über `hermes chat -Q` auf **Remote-Host** (Standard-Profil: `gemini`).

## Enthaltene Tests

1. **`t1_health_groundtruth`** — Baseline Live-Health & Config Audit (`free -m`, `docker ps`, `systemctl --user`, `~/litellm/config.yaml`) mit automatischer Ground-Truth-Prüfung.
2. **`t2_multiturn_scavenger`** — Multi-Turn Kontext-Tiefe (4 aufeinanderfolgende Datei-/Shell-Checks über `profile.yaml`, `skills/`, `litellm/config.yaml`, `df -h /`).
3. **`t3_payload_stress`** — Deterministischer 350-Zeilen-Logfile-Test (`/tmp/bridge_bench_payload.log`) mit 3 versteckten `CRITICAL_ERR_CODE`s (`X7K9`, `X4M2`, `X9Q1`).
4. **`t4_code_write_edit`** — Code-Erstellung, Escaping & Edit-Cycle (`calc.py` + `test_calc.py` in `/tmp/bridge_bench_<model>/`, `unittest`, Patch/Edit + erneuter Testlauf; wird direkt auf Remote-Host per `unittest` verifiziert).
5. **`t5_parallel_cross`** — Gleichzeitiger Cross-Provider-Lauf (`gemini-browser` und `claude-browser` parallel).

## Nutzung

```bash
# Nur Preflight prüfen (startet keine Tests und kein /init):
python3 ./gemini-bridge/benchmarks/suite.py --dry-run

# Einzelnen Test gegen beide Modelle fahren:
python3 ./gemini-bridge/benchmarks/suite.py --tests t1_health_groundtruth

# Volle Suite inkl. Parallel-Test:
python3 ./gemini-bridge/benchmarks/suite.py --tests t1_health_groundtruth t2_multiturn_scavenger t3_payload_stress t4_code_write_edit t5_parallel_cross

# Optional mit frischem /init vorab:
python3 ./gemini-bridge/benchmarks/suite.py --init-before
```

## Ergebnisse

Jeder Lauf schreibt nach `benchmarks/results/<timestamp>/`:
- `summary.md` — kompakte Vergleichstabelle (Status, Dauer, Output-Zeichen, Detail)
- `results.json` — maschinenlesbare Rohdaten
- `<test_id>__<model>.txt` — kompletter `stdout`/`stderr` je Lauf
