# pstack-codex, codex-room-setup và mô hình SLP (Supervisor–Lead–Peer)

_Nghiên cứu ngày 17/11/2026 (giờ Bangkok). Nguồn: transcript phụ đề tự động tiếng Việt của video, đọc trực tiếp hai repo (đã clone vào `/workspace/gh/hoangnb24_pstack-codex`, `/workspace/gh/hoangnb24_codex-room-setup`), và tài liệu chính thức của Claude Code CLI / Codex CLI. Không xem hình video._

## 1. Video: "Giáo án của Quỷ Vương!"
- URL: https://youtu.be/RBpdfjIFrRY — kênh **Tù Bà Khuỳm**, đăng 27/09/2026, 34:33
- Cùng tác giả với 2 repo: đường dẫn máy trong evidence của pstack-codex là `/Users/tubakhuym/...`, và video giới thiệu codex-room-setup là "repository của mình".
- "Quỷ Vương" là biệt danh một người trên Discord, tác giả gốc của mô hình SLP và ý tưởng "Codex Room"; tác giả video chỉ phổ biến lại.
- ⚠️ Video **không nhắc pstack / pstack-codex** (repo pstack-codex tạo ngày 16/10/2026, sau video). Video chỉ nói về Paseo + SLP + codex-room-setup.

| Mốc | Nội dung | Liên hệ repo |
|---|---|---|
| 00:00 | Giới thiệu mô hình SLP & "Quỷ Vương"; đi theo trường phái khác các tool phổ biến | — |
| 01:13 | **Paseo** = control plane cho coding agent: chạy agent "từ bất kỳ đâu", self-host, đa provider; app mobile rất mượt; code/kiến trúc sạch dễ custom | `paseo/`, `home/.paseo/config.json.template` |
| 02:31 | Cài Paseo → khởi tạo Paseo daemon (Node process); cài app mobile, pair bằng QR hoặc pairing link; 1 laptop quản lý nhiều host (Mac Studio, Mac mini công ty) | `scripts/install-paseo`, `docs/paseo-release.md` |
| 04:29 | Multi-provider: Codex, Claude Code, Pi, Hermes, Grok CLI, OpenRouter… bật trong config Paseo. Trong một thread có thể bảo agent này hand-off việc cho agent khác (Codex → Pi/Grok) không cần copy-paste. Paseo dùng SDK native của provider (đọc được từng tool call/message) thay vì đọc màn hình tmux như "Herder"; không có SDK thì dùng ACP | `terminalProfiles` codex/claude/opencode trong template |
| 09:46 | **SLP**: Supervisor (giám sát workflow, giữ scope/intent không bị drift, nhắc Lead), Lead (nắm project, delegate), Peer (thực thi; biến hình theo scope Lead giao: backend/frontend/architect… thay vì tạo nhiều profile). Supervisor có thể giám sát nhiều project; 1 project có thể nhiều Lead | `WORKSPACE_PROTOCOL.md`, overlays |
| 13:57 | Cấu hình bằng Agent Profile trong Paseo: Lead model mạnh nhất (full access, thinking cao); Supervisor không cần mạnh (ví dụ model "Sol" thinking low); Peer ví dụ dùng **Grok** | `overlays/*.config.toml` |
| 16:46 | Người dùng chủ yếu nói chuyện với **Supervisor** (hỏi tiến độ, "vì sao làm vậy") để không làm nhiễu context của Lead; hầu như không nói với Peer. Feedback cho Supervisor → ghi thành "notebook/experience" để lần sau giám sát tốt hơn. Supervisor dùng **heartbeat/schedule của Paseo** (ví dụ 5 phút/lần) để kiểm tra luồng của Lead. Đúng/sai là gì phụ thuộc project → bạn phải ghi lại hành vi đúng/sai thành tài liệu để Supervisor tham chiếu → "buy back time", bỏ human-in-the-loop | Supervisor overlay: heartbeat cron `*/2 * * * *` (repo hiện tại 2 phút) |
| 22:16 | Lead–Peer là **đồng nghiệp ngang hàng**, không phải cấp trên–cấp dưới: Peer trực tiếp chạm code nên có quyền phản biện, trả bằng chứng; Lead phải xử lý feedback. Khác mô hình main-agent/sub-agent nơi sub-agent cố chiều lòng main agent | `REOPEN_REQUEST`, `DEPENDENCY_REQUEST`, `BLOCKED` |
| 24:40 | Hạn chế: 3 profile dùng chung một config Codex và chung bộ tool; Supervisor/Lead cần tool Paseo để tạo agent, Peer không cần → cần tách context/tool theo role | → codex-room-setup |
| 26:00 | **Codex Room Setup**: mỗi role một runtime Codex riêng (CODEX_HOME riêng), chia sẻ phần cơ bản (auth, skills…), mỗi role SQLite/log/trace riêng, override config.toml theo role; cùng một tài khoản | `codex-room`, `codex-room-sync`, `~/.codex-runtime/<role>` |
| 28:00 | Install script cài config + Paseo (lúc quay pin v0.8.0, mới nhất 0.9.2). Sau đó Paseo có 3 provider `codex-supervisor`, `codex-lead`, `codex-peer`; danh sách model được thu gọn bằng script để Lead chọn Peer phù hợp với task | `scripts/lib/model-alignment.py` |
| 30:30 | Tác giả không dùng Claude Code nên chưa cấu hình; trên Discord có người làm biến thể cho Claude Code | — |
| 31:07 | **Bắt buộc tắt multi-agent native của Codex** (multi-agent, feature liên quan, `[agents]`) vì điều phối đã do Paseo làm; nếu không model tự spawn sub-agent → mất kiểm soát | sync ép `[agents].enabled = false` |
| 32:00 | Hiện chỉ hỗ trợ macOS; cài vào `$HOME`; symlink `paseo` CLI; overlay, script sinh profile, config Paseo | `home/` mirror `$HOME` |

## 2. Repo https://github.com/hoangnb24/codex-room-setup
- Mô tả: "Reproducible Codex Room roles, isolated runtimes, and Paseo fork setup." 28 sao, tạo 15/08/2026, commit cuối **05/11/2026 16:31 (UTC+7)**, 38 commit.
- **Kiến trúc**
  ```
  ~/.paseo/config.json (custom provider command)
    -> ~/.local/bin/codex-room <supervisor|lead|peer>
       -> codex-room-sync <role>: đọc ~/.codex/config.toml (base) + overlays/<role>.config.toml
          + `codex debug models` -> ghi ~/.codex-runtime/<role>/
       -> CODEX_HOME=~/.codex-runtime/<role> exec codex ...
  ```
- **File chính**
  - `home/.config/codex-room/overlays/{supervisor,lead,peer}.config.toml`: `model`, `model_reasoning_effort`, `sandbox_mode = "danger-full-access"`, `approval_policy = "never"`, `developer_instructions` rất dài (prompt vai trò).
  - `home/.config/codex-room/workflow/WORKSPACE_PROTOCOL.md`: hợp đồng chung (authority, dispatch, evidence, acceptance). Project có thể thêm `docs/WORKSPACE_PROTOCOL.md` (chỉ bổ sung, không đổi authority).
  - `home/.config/codex-room/model-instructions.md` (~18KB): chỉ dẫn chung cho model.
  - `home/.local/bin/codex-room` (launcher ~20 dòng), `codex-room-sync` (merge config), `paseo-local-update`.
  - `home/.paseo/config.json.template`: 3 provider `extends: "codex"`; chỉ Supervisor/Lead có `paseoTools.enabled` (Paseo MCP để tạo/nhắn agent); Peer không có.
  - `install` (plan / `--apply` transactional + rollback / `--verify`), `scripts/` (doctor, verify, sync-all, export-runtime-snapshots…), `tests/` (pytest + container acceptance `make test-container`), `docs/` (architecture, operations, profiles-and-overlays, `huong-dan-codex-room-setup.md` tiếng Việt).
- **Role mặc định hiện tại** (README): Supervisor `gpt-6.1-sol` medium; Lead `gpt-6-astra` medium; Peer `gpt-6.1-sol` medium (catalog Peer còn astra low, gpt-5.6-sol…).
- **Quy tắc phối hợp đáng học** (WORKSPACE_PROTOCOL + overlays):
  - Human giữ: mục tiêu sản phẩm, ưu tiên, chi phí, tác động bên ngoài, rủi ro không đảo ngược.
  - Supervisor: quan sát, chuyển nguyên vẹn ý Human, phục hồi có giới hạn; **không thành Lead thứ hai**; can thiệp qua Lead; heartbeat là fallback cho event (finish/error/attention), không poll khi state không đổi.
  - Lead: framing kỹ thuật, dependency, tích hợp, verification, **chấp nhận/từ chối candidate rõ ràng kèm lý do**.
  - Peer: đúng một outcome có giới hạn; trả **immutable candidate** (commit/snapshot) + base ban đầu + danh sách path + lệnh đã chạy + rủi ro còn lại. Tín hiệu: `REOPEN_REQUEST` (tiền đề sai), `DEPENDENCY_REQUEST`, `BLOCKED`, mỗi tín hiệu kèm bằng chứng/hệ quả/quyết định cần.
  - Mỗi request phải **tự đứng một mình**: outcome, input, scope, giới hạn, kết quả mong đợi. Không kể lể chuỗi điều phối, không giả danh user.
  - Mỗi write scope một owner; Peer ghi song song chỉ khi scope tách biệt + input đã accepted; tối đa một Peer ghi vào repo tại một thời điểm (tài liệu tiếng Việt).
  - **Test pass / "DONE" / trạng thái lifecycle chỉ là bằng chứng, không phải acceptance.** Review độc lập = Lead gọi một Peer mới ở chế độ **read-only** trên candidate cố định.
- **Cài đặt**: `git clone …; ./install; ./install --apply; paseo daemon start; ./install --verify`. Yêu cầu: macOS/Unix, Bash, Python ≥3.11, Git, Node 22, npm, jq; Codex đã cài và đăng nhập. Không bao giờ ghi vào `~/.codex`.
- Pin Paseo: video v0.8.0; commit cuối "point local Paseo pin to main at v0.15.1 merge" (README vẫn ghi stable v0.8.0 → chưa đồng bộ).

## 3. Repo https://github.com/hoangnb24/pstack-codex
- "pstack for Codex": port độc lập (không chính thức) của pstack của Lauren Tan, pin upstream `cursor/plugins@3fa33bb…` bản pstack **0.15.43**, plugin **0.6.2**, MIT. 2 sao, tạo 16/10/2026, commit cuối **17/11/2026 08:18 (UTC+7)**, 9 commit — bám upstream rất sát (đồng bộ 0.15.42 ngày 16/11, 0.15.43 ngày 17/11).
- **Cấu trúc**: `.codex-plugin/plugin.json` (manifest, chỉ đăng ký `./skills/`; không MCP/hook/service), `.agents/plugins/marketplace.json` (marketplace local `pstack-local`), `skills/pstack-*` (58 skill, mỗi skill có `agents/openai.yaml` với `allow_implicit_invocation: false`), 22 playbook trong `pstack-poteto-mode`, `automations/benny` (dormant), `UPSTREAM.json` (SHA-256 từng file gốc), `tools/` (`stage.py`, `smoke_discovery.py`, `smoke_plugin.py`, `snapshot.py`), `tests/`, `docs/` (PARITY, CAPABILITIES, CORE-PORT, SKILL-PORT, HELPER-PORT, VALIDATION, PLUGIN) và `docs/evidence/*.json|md` = **biên lai (receipt)** cho mỗi lần cài/đồng bộ (hash payload, số skill, config không đổi…).
- **Cài đặt**
  ```sh
  codex plugin marketplace add <đường-dẫn-repo>
  codex plugin add pstack-codex@pstack-local
  codex plugin list --marketplace pstack-local --json
  # gọi: $pstack-codex:pstack-poteto-mode <task>
  # thử cô lập: python3 tools/stage.py /tmp/sandbox && cd /tmp/sandbox && codex  (gọi $pstack-poteto-mode)
  # kiểm tra: python3 tools/smoke_discovery.py (app-server tạm, CODEX_HOME tạm)
  ```
- **Cách chuyển pstack sang Codex** (CORE-PORT / CAPABILITIES):
  - Tên skill thêm prefix `pstack-` tránh trùng (ví dụ `tdd`); gọi tường minh bằng `$…` (57 skill explicit-only, `setup-pstack` được discovery mặc định).
  - Cursor Task/subagent_type/chọn model theo role → dùng cơ chế delegation thật của Codex nếu có và được phép, **kế thừa model của parent**; không có thì chạy tuần tự và **phải nói rõ là mất review độc lập**; "cùng model ≠ đa dạng model".
  - `~/.cursor/rules/pstack-models.mdc` → mặc định model hiện tại của host.
  - Bỏ các thứ Cursor-only: cloud VM, `/loop` bền, Bugbot, Graphite, transcript store; merge/push/message cần user cho phép; babysit không rebase/force-push.
  - Mỗi skill có khối "Codex execution boundary" (không push/PR/merge/cài phần mềm/paid inference nếu không được phép).
  - Persona `poteto-agent`, `comment-sicko` → prompt template trong `references/agent-briefs.md`.
  - Toolchain statechart (TLA+ 1.7.4, Java 17, Node 22) qua launcher local.
- **Liên hệ codex-room**: evidence ghi 3 "Droid links" `supervisor/lead/peer` trỏ tới `skills/` của repo → skill pstack được chia sẻ cho cả 3 role (có vẻ là thư mục skills của runtime/agent khác; "Droid" chưa xác minh là gì — có thể Factory Droid).
- **Khác pstack gốc**: không có model routing đa model mặc định (Lauren: Opus 5.5 xhigh cho code, panel Fable/Opus/Gemini cho review); không cloud agent/automation; an toàn hơn (mọi hành động bên ngoài cần cho phép); gọi skill tường minh thay vì auto; có receipt/hash cho mỗi lần cài.

## 4. Sự kiện CLI liên quan thiết kế (từ docs chính thức, kiểm tra 17/11/2026)
### Claude Code (https://code.claude.com/docs/en/cli-reference)
- Headless: `claude -p "<prompt>"` (print mode), stdin pipe được; tiếp tục `-c`, `-r <id|name>`.
- Model: `--model sonnet|opus|haiku|fable|<tên đầy đủ>`; `--effort low|medium|high|xhigh|max|ultracode`; `--fallback-model sonnet,haiku`.
- Output: `--output-format text|json|stream-json`; `--json-schema '<schema>'` (JSON đã validate khi xong); `--input-format stream-json`.
- Giới hạn: `--max-turns N`, `--max-budget-usd X`.
- Quyền/tool: `--permission-mode default|acceptEdits|plan|auto|dontAsk|bypassPermissions`; `--allowedTools`, `--disallowedTools`, `--tools`; `--dangerously-skip-permissions`.
- Prompt: `--append-system-prompt[-file]`, `--system-prompt-file`; `--agents <json>` (subagent động); `--settings`; `--bare` (bỏ auto-discovery cho script nhanh); `--worktree/-w`; `--no-session-persistence`.
### Codex CLI (https://developers.openai.com/codex/noninteractive, https://developers.openai.com/codex/cli/reference)
- Headless: `codex exec "<prompt>"` (tiến trình ra stderr, câu trả lời cuối ra stdout); `codex exec -` đọc prompt từ stdin; `codex exec resume --last|<id>`.
- Mặc định sandbox **read-only** (hợp với reviewer); `--sandbox workspace-write|danger-full-access`; `--full-auto` đã bỏ, dùng `--sandbox workspace-write` hoặc `--approve-for-me`.
- Model: `-m/--model`; reasoning qua `-c model_reasoning_effort=…`; `-p/--profile` (layer `$CODEX_HOME/<profile>.config.toml`); `-c key=value`.
- Output: `--json` (JSONL event: thread.started, turn.completed, item.*…), `-o/--output-last-message <file>`, `--output-schema <schema.json>`.
- Review: `codex review --uncommitted | --base <branch> | --commit <SHA> [--title]` hoặc prompt tùy biến (loại trừ lẫn nhau).
- Khác: `--ephemeral`, `--ignore-user-config`, `--skip-git-repo-check` (mặc định yêu cầu git repo), `--worktree`, `--trust`; `CODEX_HOME` riêng để tách runtime (cách codex-room làm).

## 5. Bài học cho harness Grok Bot (supervisor) + Claude (coder) + Codex (reviewer)
- SLP ánh xạ tự nhiên: Grok Bot = Supervisor (+ có thể kiêm Lead/router), Claude = Peer viết code, Codex = Peer review read-only trên **candidate bất biến** (commit SHA).
- Lấy từ codex-room: request tự đứng một mình; candidate bất biến + base + paths + lệnh + rủi ro; test pass ≠ acceptance; tín hiệu REOPEN/DEPENDENCY/BLOCKED; event thay vì polling; tắt sub-agent native của từng CLI để supervisor nắm topology; tách runtime/config theo role.
- Lấy từ pstack (Lauren): gate cứng (lint/type/test/CI) trước, prose sau; verification skill + feature map; `/correct`; eval cho prompt/skill; review bằng bằng chứng.
- Lấy từ pstack-codex: receipt JSON có hash cho mỗi bước; nói rõ khi mất review độc lập; cùng model ≠ đa dạng.

## 6. Chưa xác minh
- Không xem hình video; tên tool nghe từ phụ đề tự động (Paseo ghi thành "Pasio", Herder, Hermes, Pi) có thể sai chính tả.
- "Droid links" trong pstack-codex là gì (Factory Droid?) chưa rõ.
- Tên model trong repo (`gpt-6.1-sol`, `gpt-6-astra`) lấy nguyên từ file; chưa kiểm tra tài khoản.
- Claude Code / Codex CLI chưa cài trên box nên chưa chạy thử flag nào; chỉ dựa trên docs.
- Paseo docs chính thức chưa đọc (chỉ qua repo/video).

## File
- Transcript: `/workspace/videos/RBpdfjIFrRY.txt`, metadata `/workspace/videos/RBpdfjIFrRY.info.json`
- Repo: `/workspace/gh/hoangnb24_pstack-codex`, `/workspace/gh/hoangnb24_codex-room-setup`
- Nghiên cứu trước: `/workspace/research/lauren-tan-harness.md`

## 7. Các quyết định cần brainstorm (bản nháp)
1. **Có viết test đỏ trước không?** A: bắt buộc red-first mọi task (kỷ luật cao, tốn token, có khi test giả) · B: red-first khi có đường test rẻ (giống `pstack-tdd`), còn lại dùng verification script/runtime · C: test do agent khác viết (chống "tự viết đề tự chấm").
2. **Ai viết test?** Claude (cùng ngữ cảnh, nhanh, nhưng dễ viết test khớp implementation) · Codex (độc lập, đóng vai "đề thi" trước khi Claude code) · Grok sinh tiêu chí chấp nhận/property, Codex hiện thực thành test.
3. **Ai review?** Codex `codex review --commit <SHA>` read-only + `--output-schema` · thêm panel đa model khi rủi ro cao · Grok chỉ chấm biên lai, không đọc diff.
4. **Định tuyến theo độ phức tạp**: tín hiệu (số file/blast radius, loại task, có concurrency/state machine, lịch sử fail) → Grok gate trả `{tier: haiku|sonnet|opus, p, reasons}` → `claude -p --model … --effort …`; escalate khi fail N lần (giống pstack: Fable chỉ sau 2 lần Opus fail).
5. **Gate logic ngữ nghĩa**: mỗi gate = lời gọi Grok với JSON schema `{label, p, evidence_refs, rationale}`; ngưỡng chấp nhận/từ chối/hỏi người; luôn đặt **sau** gate cứng (build/lint/test) để không dùng xác suất thay cho check xác định; cần hiệu chỉnh (calibration) bằng log lịch sử.
6. **Tournament**: K bản Claude trong worktree riêng (khác model/prompt) → loại bằng gate cứng → Codex review từng bản → Grok so cặp/Elo trên biên lai; trade-off: tốn K× token, đáng dùng cho task khó/mơ hồ.
7. **Biên lai (receipt)**: JSON bất biến mỗi bước: task_id, base SHA, candidate SHA, model/effort, lệnh đã chạy + exit code + hash log, test đỏ→xanh, findings review, điểm gate, chi phí/token; chấm điểm chỉ dựa trên receipt (không dựa lời tự khai của agent).
8. **Vị trí gate**: G0 intake (hiểu yêu cầu, rủi ro, cần người?) → G1 routing → G2 test đỏ hợp lệ (fail đúng lý do) → G3 cứng (build/lint/type/test) → G4 review Codex → G5 chấm receipt/acceptance → G6 merge (người hay auto).
9. **Điều phối**: Paseo (đa provider, mobile) vs script/daemon tự viết gọi `claude -p`/`codex exec` vs Grok Bot gọi trực tiếp qua shell/routine; tắt sub-agent native của mỗi CLI.
10. **Quyền & cô lập**: Claude `--permission-mode acceptEdits` + `--allowedTools` trong worktree/container; Codex `--sandbox read-only`; không `danger-full-access` ngoài container.
