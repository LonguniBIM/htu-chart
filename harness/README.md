# `topstep-chart-app` Harness Operations Guide

Harness kết hợp 3 nguồn:
1. **Mô hình S-L-P (quy mô codebase nhỏ)**:
   - **Supervisor + Lead (Gemini Neon)**: Giữ scope & tuân thủ Prop Firm Read-Only, soạn Task Packet, tranh luận kỹ thuật bình đẳng với Peer, chạy gate cứng & Codex Room Review, và quyết định `ACCEPT`/`REJECT`.
   - **Peer Coder (model thấp hơn — `flash`)**: Viết code, viết test theo `test-required.md`, phản biện khi phát hiện lỗ hổng spec (`REOPEN_REQUEST`, `DEPENDENCY_REQUEST`, `BLOCKED`), commit candidate SHA bất biến.
   - **Lead Review (Codex CLI — Codex Room)**: Chạy 2 lượt độc lập **read-only** (`topstep-review-pass1` & `topstep-review-pass2`) trên đúng commit SHA bàn giao.
2. **Lauren Tan Inverted Pyramid**: Khóa cứng bằng kiến trúc, `arch_check.py`, `build_verifier.py`, runtime `verify_app.py`, statecharts & feature map trước khi viết thêm prose.
3. **`test-required.md`**: Test bảo vệ hành vi thật, cấm test tự xác nhận, bugfix bắt buộc chứng minh **RED trước sửa → GREEN sau sửa**, 2 lượt review hợp nhất trên cùng SHA, kết luận bằng biên lai bất biến (`VERIFIED` / `FAILED` / `BLOCKED`).

---

## Lệnh thao tác hằng ngày (chạy tại `D:\01_DCMvn\Workspace\Lightchart`)

### 1. Kiểm tra sức khỏe môi trường (`doctor`)
```powershell
python topstep-chart-app/harness/scripts/verify_app.py doctor
```

### 2. Đồng bộ 2 runtime Codex Room (cô lập ngoài repo tại `%USERPROFILE%\.codex-runtime`)
```powershell
python topstep-chart-app/harness/scripts/codex_room_sync.py --role all
python topstep-chart-app/harness/scripts/codex_room_sync.py --role all --verify-only
```

### 3. Chạy gate tĩnh & `build-verifier`
```powershell
python topstep-chart-app/harness/scripts/arch_check.py
python topstep-chart-app/harness/scripts/build_verifier.py --phase 0 --out topstep-chart-app/harness/runs/verifier.json
```
*(Thay `--phase 1` .. `6` tương ứng phase đang triển khai).*

### 4. Chạy Lead Review 2 lượt độc lập bằng Codex Room
```powershell
# Kiểm tra render prompt & schema không tốn API:
python topstep-chart-app/harness/scripts/codex_room_review.py --commit HEAD --task-id P1-01 --dry-run

# Chạy thật 2 lượt độc lập (read-only) trên đúng SHA commit
# Mặc định `--backend auto`: luôn ưu tiên Codex CLI trước; chỉ fallback sang `agy` (`claude-opus-5-5-medium`) khi Codex CLI hết quota / chưa sẵn sàng:
python topstep-chart-app/harness/scripts/codex_room_review.py --commit <SHA> --task-id P1-01 --task-packet <path> --fail-on-blocking

# Ép chạy thẳng `agy` Opus 5.5 (khi biết chắc Codex CLI đang hết quota) hoặc ép chỉ dùng Codex CLI:
python topstep-chart-app/harness/scripts/codex_room_review.py --commit <SHA> --task-id P1-01 --backend agy --fallback-model claude-opus-5-5-medium --fail-on-blocking
python topstep-chart-app/harness/scripts/codex_room_review.py --commit <SHA> --task-id P1-01 --backend codex --fail-on-blocking
```

### 5. Xuất biên lai (Receipt) chốt nghiệm thu
```powershell
python topstep-chart-app/harness/scripts/receipt.py `
  --task-id P1-01 --phase 1 --base-sha <BASE_SHA> --candidate-sha <CANDIDATE_SHA> `
  --verifier-json topstep-chart-app/harness/runs/verifier.json `
  --review-json topstep-chart-app/harness/runs/P1-01-<short>/consolidated-review.json
```
*(Nếu task là bugfix, thêm `--bugfix --red-proof-json <path>`).*

---

## Bắt đầu Phase 1 với Gemini Neon (S-L-P)
1. Đảm bảo đã chạy `codex_room_sync.py --role all` thành công (1 lần).
2. Nhắn cho Gemini Neon (session này): *"Bắt đầu Phase 1 task đầu tiên theo skill `slp-orchestrator`"*.
3. Lead (Neon) sẽ tách Task Packet trong [task.md](../task.md), gọi `peer-coder` (`flash`) viết code + tranh luận kỹ thuật, chạy `build_verifier.py`, gọi `codex_room_review.py` (2 lượt Codex CLI read-only), xuất `receipt.py` và chỉ đánh dấu xong khi kết luận là `VERIFIED`.
