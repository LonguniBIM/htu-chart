# Lauren Tan (poteto) — Ý tưởng & cách setup codebase/harness cho AI agent

_Nghiên cứu ngày 17/11/2026 (giờ Bangkok). Nguồn: transcript (phụ đề tự động tiếng Anh) của 2 video + đọc trực tiếp các repo GitHub. Không xem hình/slide._

## Ý chính
Muốn tin agent thì phải đầu tư vào codebase, các check tự động và khả năng tự kiểm chứng. Mỗi lần phải sửa agent, biến lần sửa đó thành một check máy tự chạy (lint/type/CI/test) thay vì thêm một dòng vào rules/AGENTS.md.

## Video 1 — "All Code Is Slop (Except Mine)" — Lauren Tan, SpaceXSI
- URL: https://youtu.be/O_zcc6Xl12g — kênh AI Engineer, đăng 14/11/2026, 17:31
- Mô tả video dẫn tới https://github.com/poteto và https://github.com/cursor/plugins/tree/main/pstack

| Mốc | Nội dung |
|---|---|
| 0:00 | Ship 3.000 PR/tháng. Việc của kỹ sư = tăng năng suất mà giữ/nâng chất lượng. Không tin mù quáng mà đầu tư môi trường để tin agent (mất ~6 tháng). |
| 2:03 | Kim tự tháp ngược: (1) code — API, kiến trúc, refactor; (2) kiểm tra tĩnh — lint, type, compiler, test; (3) verification runtime + formal; (4) văn bản (rules, skills, AGENTS.md) là lớp cuối cho việc cần phán đoán. Code là input lớn nhất; workaround/pattern cũ bị chép như "cỏ dại". |
| 5:00 | Dune ("Next.js cho Electron"): cấm `useEffect` trong product code; tách `electron/main` vs `electron/renderer`, chặn import chéo bằng dependency-graph analyzer + lint. |
| 6:56 | Agent dùng comment/markdown để biện minh workaround (ví dụ timer `setTimeout`/rAF lan khắp nơi → GC jank). Grok Bot cấm 99% comment; lint rule có thông báo lỗi chỉ cách sửa tận gốc. Mỗi correction → check xác định hoặc loại bỏ bằng thiết kế API → dần không còn gì để review → agent tự merge. |
| 9:40 | Review = xem bằng chứng (video, screenshot, sơ đồ). Runtime verification qua Chrome DevTools Protocol. "Feature map" = sitemap tính năng theo góc nhìn người dùng. Skill có CLI driver riêng (tiết kiệm token, test được). |
| 10:50 | TLA+ model-check luồng chính (thông báo đa thiết bị); Lean chứng minh thuật toán virtualization; Bombadil cho property-based UI test. |
| 11:51 | Phần lớn token dành cho verification (ví dụ Cindy: 70%). Skill `statechart` trong pstack vẽ state machine để review. |
| 13:32 | 11.000 PR/tháng, revert ~0,5%, ra stable ~1 lần/ngày, 227 người đóng góp tháng 10, hiệu năng vẫn tốt lên (số tự báo cáo). |
| 15:11 | Nền móng trước, tự động hóa sau. Vòng lặp trong ra rác thì tự động hóa ra nhiều rác hơn. Ví dụ: mỗi release, agent tự fuzz app, mở PR sửa bug và auto-merge. |

## Video 2 — "SpaceX AI Engineer Lauren Tan: Part 1"
- URL: https://youtu.be/PaPpyQocMww — kênh "Ai insider" (bản đăng lại của bên thứ ba, webinar có người dẫn Colin), đăng 10/09/2026, 55:30
- ⚠️ Mô tả video có các câu ("Chief of Staff agent, 3 managers, 11 workers", "chỉ 1% dùng đúng") KHÔNG có trong transcript.

| Mốc | Nội dung |
|---|---|
| 0:46 | Đường cong niềm tin: như quản lý — không tin thì micromanage. Từ canh 1–2 agent → sáng dậy 20 PR đã tự merge. ~1.000 PR/tháng ở Cursor. |
| 5:39 | Kỹ năng quan trọng nhất: verification (agent chạy app, CPU trace, heap snapshot, iOS simulator). Không có thì bạn là nút cổ chai. Skill `control-glass` (CDP). Feature map giúp hiểu báo lỗi mơ hồ (ảnh + "???"). pstack có `create-verification-skill`, `maintain-verification-skill`. |
| 13:08 | pstack (đùa với "gstack" của Garry Tan) lớn lên dần: mỗi failure mode lặp lại → một skill (ví dụ `how`). Skill = onboarding cho kỹ sư giỏi nhưng chưa có ngữ cảnh. |
| 17:23 | Eval = unit test cho agent: coordinator viết rubric, spawn subagent trong thư mục tên che giấu (agent biết bị test sẽ đổi hành vi), judge model khác kiểm tra chéo, chạy nhiều model, `/loop` tới 10/10. Đọc tool call/thinking để tìm chỗ fail. Ẩn dụ bếp trưởng. |
| 23:42 | Bắt đầu local để quan sát → cloud agent → automation. "Benny": nhận báo lỗi Slack, chạy app trong VM, tái hiện bug (có khi phát hiện đã fix trên main). Đừng nhảy lên ngàn agent khi chưa tin được 1. |
| 29:20 | Greenfield vibe-code rủi ro nhất. Refactor Grok Bot ~600 PR. "Đường ngắn nhất là đường đúng". Mỗi feature một thư mục; khái niệm (feature, entry point, transcript card) có quy ước cố định. PR không giới hạn size nhưng tách nhỏ, atomic. |
| 38:02 | CI chặn: `useEffect`, comment, import main↔renderer. Lớp cứng (CI đỏ): kiến trúc, lint, compiler. Lớp mềm (có thể quên): rules, skills, Bugbot. Stack compiler chặt (Rust, cấm unsafe) giúp. Comment review lặp lại = code smell → đổi thành lint/CI. |
| 48:00 | Token là bài toán ROI; sau đầu tư, PM/designer cũng ship code an toàn. |

## Repo liên quan (GitHub `poteto`, 85 repo công khai)
| Repo | Mục đích / file nên học | Liên hệ video | Cập nhật |
|---|---|---|---|
| https://github.com/cursor/plugins/tree/main/pstack | Repo chính (Lauren 165/171 commit). `skills/` (~60), `skills/poteto-mode/playbooks/` (22), `agents/comment-sicko.md`, `automations/benny/`, `docs/guide/01–10` | Cả 2 video | 17/11/2026 |
| https://github.com/poteto/verification-skill-example | `.cursor/skills/verify-atlas/SKILL.md` (launch/doctor/drive/evidence/cleanup, checkout song song) + `references/features/*.md` (~30, 4 mục). Không kèm driver script | Feature map, control skill | 31/07/2026 |
| https://github.com/poteto/how | Plugin `/how`: giải thích kiến trúc, subagent song song, chế độ critique đa model | Skill `how` | 15/04/2026 |
| https://github.com/poteto/brainmaxxing | Bộ nhớ vault markdown; skill reflect/ruminate/meditate/plan/review; 16 principle; hook `SessionStart`/`PostToolUse` | Tiền thân `principle-*`, `reflect` | 28/02/2026 |
| https://github.com/poteto/noodle | Điều phối agent bằng skill (Go), "kitchen brigade"; docs https://poteto.github.io/noodle/ | Bếp trưởng, điều phối | 19/03/2026 |
| https://github.com/poteto/homebrew-tap | Formula Homebrew cho noodle | Phụ | 04/03/2026 |
| https://github.com/poteto/benny-avatars | Ảnh avatar bot Slack của orchestrate | Benny | 01/05/2026 |
| https://github.com/cursor/plugins/tree/main/orchestrate | Chia việc ra nhiều Cursor cloud agent qua SDK (Lauren 3/7 commit — chưa chắc tác giả chính) | Cloud agent | — |

## Skill pstack nên đọc trước
- `poteto-mode` (router chọn playbook); `create-verification-skill` / `maintain-verification-skill`
- `correct`: kiến trúc → type → lint có thông báo chỉ cách sửa → test → tài liệu sau cùng; chứng minh check bắt được lỗi thật; bảng "quy tắc ↔ thứ ép buộc" trong AGENTS.md
- `principle-encode-lessons-in-structure`, `no-comments` + agent Comment Sicko
- `create-formal-verification-skill` (TLA+), `statechart`, `tdd`, `swarm`, `arena`, `reflect`, `setup-pstack`

## Cài đặt
- Cursor: `/add-plugin pstack`
- Claude Code: `/plugin marketplace add cursor/plugins` rồi `/plugin install pstack@cursor-plugins` (lệnh có prefix `pstack:`)

## Checklist áp dụng
1. Verification trước: `.cursor/skills/verify-<app>/` gồm launch từ trạng thái sạch, `doctor` chỉ đọc, CLI driver selector ổn định, nơi lưu bằng chứng (không bị cleanup xóa), cleanup chỉ kill thứ mình khởi chạy, chạy song song trên worktree.
2. Feature map: `features/README.md` + mỗi feature 1 file (Sub-features / Cách vào / Driving / Gotchas); cập nhật trong PR đổi UI.
3. Kiến trúc ép buộc: mỗi feature 1 thư mục, ranh giới thư mục + lint import/dependency graph trong CI, mỗi việc 1 cách, xóa API cũ/code chết.
4. Cấm API dễ dùng sai bằng lint có thông báo chỉ cách sửa; pattern cũ chỉ fail khi PR thêm mới.
5. Cấm hầu hết comment, `eslint-disable`/`ts-ignore`; ngoại lệ có lý do + hạn + người duyệt.
6. Vòng `/correct` + bảng quy tắc ↔ enforcement; quy tắc đã ép bằng code thì xóa khỏi văn bản.
7. AGENTS.md/rules/skills ngắn, chỉ cho phán đoán; skill mới khi thấy failure mode lặp lại.
8. Eval cho skill: rubric, subagent không biết bị test, judge model khác, nhiều model, lặp tới đạt.
9. State machine/đồng thời/retry: TLA+, property-based test, `statechart`.
10. Review bằng bằng chứng; PR nhỏ, atomic.
11. Mở rộng: local → cloud agent → automation → auto-merge.

## Chưa xác nhận
- Chỉ dùng transcript, chưa xem slide/code trên màn hình.
- Dune là framework đóng, không open-source.
- Driver `control-atlas.mjs` cố tình bị bỏ khỏi repo mẫu.
- Chưa chắc Lauren là tác giả chính `cursor/plugins/orchestrate`.
- Các con số (11.000 PR, 0,5% revert, 227 người) là tự báo cáo.

## File trên box
- Video 360p: `/workspace/videos/O_zcc6Xl12g.mp4`, `/workspace/videos/PaPpyQocMww.mp4`
- Transcript có mốc: `/workspace/videos/O_zcc6Xl12g.txt`, `/workspace/videos/PaPpyQocMww.txt`; metadata `*.info.json`
- Repo đã clone: `/workspace/gh/` (cursor_plugins, poteto_*)
