---
name: verify-topstep-app
description: >-
  Run runtime verification (doctor, isolated launch, read-only smoke probe, cleanup)
  and build-verifier gates for topstep-chart-app while capturing evidence artifacts.
---

# Verify Topstep App

Use this skill to verify runtime behavior, Read-Only compliance, and zero-secret guarantees without disturbing user data.

## 1. Environment & Repo Doctor (Read-Only)
```powershell
python topstep-chart-app/harness/scripts/verify_app.py doctor
```

## 2. Execute `build-verifier` Gate
```powershell
python topstep-chart-app/harness/scripts/build_verifier.py `
  --phase <PHASE_NUM> `
  --strict-clean `
  --out topstep-chart-app/harness/runs/<TASK_ID>/verifier.json
```

## 3. Isolated Runtime Launch, Smoke & Cleanup
1. Launch server bound strictly to `127.0.0.1` with a clean temp `DATA_DIR`:
   ```powershell
   python topstep-chart-app/harness/scripts/verify_app.py launch --port 18456
   ```
2. Probe `/api/status` (no leaked keys/tokens) and verify `/api/Order/place` is rejected (`401/403/404/405`), saving evidence under `harness/evidence/`:
   ```powershell
   python topstep-chart-app/harness/scripts/verify_app.py smoke --url http://127.0.0.1:18456
   ```
3. Clean up ONLY the tracked PID (preserves saved evidence):
   ```powershell
   python topstep-chart-app/harness/scripts/verify_app.py cleanup
   ```

## 4. Feature Map & Statechart Sync
If a PR adds/changes UI controls, HTTP/WS endpoints, or state transitions, update the corresponding `topstep-chart-app/docs/features/*.md` and `topstep-chart-app/docs/statecharts.md` before final commit.
