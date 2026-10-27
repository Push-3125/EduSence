# EduSense — Antigravity & Claude Code Agent Setup

Tài liệu này mô tả **đúng hiện trạng** cấu hình agent trong repo. Khi thêm, xóa hoặc đổi tên rule/skill/agent,
cập nhật tài liệu này trong cùng commit.

## Cấu trúc

```text
AGENTS.md           Chỉ dẫn chung cho mọi agent (Antigravity, Claude Code, ...)
CLAUDE.md           Import AGENTS.md + con trỏ tới rule/skill cho Claude Code
.agents/
├── rules/      7 rule: ai-rag, architecture, backend, database, frontend-ui, security, testing
├── agents/     8 agent (xem bảng "Danh sách agent" bên dưới)
├── skills/     14 skill tự viết:
│               agent-handoff, api-design, code-review, database-design, debugging,
│               frontend-design, git-workflow, implement-feature, requirements-analysis,
│               security-audit, stitch-to-vue, task-planning, test-plan, test-writer
└── plugins/    skill bên thứ ba, mỗi skill chỉ có MỘT bản
    ├── stitch-design/     generate-design, code-to-design, manage-design-system,
    │                      extract-design-md, extract-static-html, upload-to-stitch
    ├── stitch-utilities/  design-md, enhance-prompt, site-md, stitch-loop, taste-design
    └── taste-skill/       design-taste-frontend (art direction + inspect cho page/app screen)
docs/
├── DESIGN.md           Design system / token (nguồn chuẩn cho Tailwind)
├── ui-agent-team.md    Quy trình phase-gated của nhóm agent thiết kế UI
├── agent-setup.md      Tài liệu này
└── reference/
    └── stitch-build/   react-components, react-vite-dashboard, shadcn-ui, react-native, remotion
                        (chỉ tham khảo, agent không tự nạp)
```

## Danh sách agent

Tất cả agent dùng `model: inherit` (theo model của phiên chính), `subagent: true`, `commandExecutionPolicy: sandbox`.

| Agent | Nhóm | Trách nhiệm | Ghi file |
|---|---|---|---|
| `ui-art-director` | UI | Định hướng nghệ thuật, Design Brief, chống giao diện rập khuôn | Không |
| `stitch-designer` | UI | Thiết kế màn hình trên Stitch; màn hình phải được duyệt trước khi viết Vue | Không |
| `ui-reviewer` | UI | Kiểm duyệt UI/UX, a11y, độ nhất quán với `DESIGN.md` | Không |
| `vue-frontend-builder` | UI | Chuyển màn hình Stitch đã duyệt sang Vue 3 SFC (`stitch-to-vue`) | Không |
| `backend-architect` | Engineering | Kiến trúc FastAPI (router → service → repository), DB/pgvector, Alembic, MinIO, Celery | Có |
| `test-engineer` | Engineering | Thiết kế và viết test pytest/Vitest; không nới assertion để test pass | Có |
| `code-reviewer` | Review | Review tính đúng, khả năng bảo trì, regression, tuân thủ rule; chuyển phần bảo mật cho `edusense-security-reviewer` | Không |
| `edusense-security-reviewer` | Review | Chuyên gia bảo mật: auth/RBAC, ownership, secret, upload, rủi ro AI/RAG | Không |

Luồng dự kiến: UI theo `ui-agent-team.md` (Art Director → Stitch Designer → UI Reviewer → Vue Builder);
backend: `backend-architect` → `test-engineer` → `code-reviewer` (+ `edusense-security-reviewer` khi chạm vùng nhạy cảm bảo mật).

### Phân bổ Model & Mức suy luận (Intended Allocation vs. Runtime Configuration)

| Agent | Model mong muốn | Reasoning Effort mong muốn | Cấu hình thực tế áp dụng trong repo |
|---|---|---|---|
| Main Orchestrator | Claude Opus 5.5 | Low | Chọn trên menu UI (`Model Selection`) |
| `ui-art-director` | Claude Opus 5.5 | Medium | `model: inherit` |
| `stitch-designer` | Gemini 3.8 Flash | High | `model: inherit` |
| `ui-reviewer` | Gemini 3.8 Flash | Low | `model: inherit` |
| `vue-frontend-builder` | Gemini 3.8 Flash | Medium | `model: inherit` |
| `backend-architect` | Gemini 4 Argon | High | `model: inherit` |
| `test-engineer` | Gemini 3.8 Flash | Medium | `model: inherit` |
| `code-reviewer` | Gemini 4 Argon | Medium | `model: inherit` |
| `edusense-security-reviewer` | Claude Opus 5.5 | Medium | `model: inherit` |

**Kết quả kiểm tra cơ chế runtime (Antigravity v2.26.0):**
- **Session-level (Cấp phiên):** Antigravity cung cấp menu chọn model hợp nhất (`Model Selection` ở góc chat UI) đi kèm mức suy luận (ví dụ `Claude Opus 5.5 (Low)`, `Gemini 3.8 Flash (High)`). Thiết lập này áp dụng cho Orchestrator và tất cả subagent dùng `model: inherit`.
- **Per-agent YAML frontmatter:** Chỉ hỗ trợ `model: inherit` (hoặc các tier khái quát `flash_lite`, `flash`, `pro`). Antigravity **không hỗ trợ** gán ID model cụ thể (`Claude Opus 5.5`, `gemini-4-argon`) trong frontmatter agent và **hoàn toàn không có trường `reasoningEffort`** trong schema protobuf (`language_server.exe`).
- **Quy trình thao tác:** Để đạt được cấu hình tối ưu khi chạy từng nhóm tác vụ, người dùng chọn model trên UI tương ứng trước lượt thực thi (hoặc mở session riêng cho agent đó). Không khai báo trường giả hay model ID tùy tiện vào file agent.

## Quyết định đã chốt

- **Frontend: Vue 3 + TypeScript + Tailwind CSS** (AGENTS.md §2). Token màu nằm ở `docs/DESIGN.md`, không hard-code hex.
- Các skill `stitch-build` chỉ sinh code React nên đã được dời ra `docs/reference/stitch-build/`, ngoài `.agents/`,
  để agent không tự chọn nhầm. Chỉ dùng làm **mẫu tham khảo** về cách chuyển thiết kế Stitch thành component.
  Agent **không được** sinh code React vào `frontend/` (AGENTS.md §16).
- Chuyển thiết kế Stitch sang Vue dùng skill `stitch-to-vue`, chỉ sau khi màn hình đạt kiểm duyệt (xem `ui-agent-team.md`).
- Thiết kế gốc nằm ở Stitch project `12694985645523425274`.
- Đường dẫn trong skill/rule luôn là đường dẫn tương đối từ thư mục gốc repo, không dùng đường dẫn tuyệt đối của máy cá nhân.

## Dùng với từng công cụ

| | Antigravity | Claude Code |
|---|---|---|
| Chỉ dẫn chung | `AGENTS.md` (tự đọc) | `CLAUDE.md` → `@AGENTS.md` |
| Rule | `.agents/rules/` (tự nạp theo `trigger`) | Đọc theo con trỏ trong `CLAUDE.md` |
| Skill | `.agents/skills/` (tự nạp) | Đọc theo con trỏ; muốn tự nạp thì chép sang `.claude/skills/` |
| Agent | `.agents/agents/` | Chưa dùng; nếu cần, chép sang `.claude/agents/` |

## Stitch MCP (secret không nằm trong repo)

Stitch MCP chỉ được cấu hình ở **global**: `~/.gemini/config/mcp_config.json`.

```json
{
  "mcpServers": {
    "stitch": {
      "url": "https://stitch.googleapis.com/mcp",
      "headers": { "X-Goog-Api-Key": "<điền key của bạn tại máy local>" }
    }
  }
}
```

- Không tạo `mcp_config.json` trong `.agents/` hoặc `.agents/plugins/*` (`.gitignore` đã chặn).
- Mỗi thành viên tự điền key của mình trên máy local.

## Việc còn mở

- Lệnh test/lint trong AGENTS.md §3 chờ có `backend/` và `frontend/`; cập nhật ngay khi tạo.
- Chưa xác minh tên tool khai báo trong các file agent (`view_file`, `grep_search`, `run_command`, `write_file`, `edit_file`)
  so với tài liệu chính thức của Antigravity; hiện chỉ đối chiếu theo quy ước của các agent sẵn có.
- Chưa có `.claude/skills/` và `.claude/agents/`; tạo khi nhóm bắt đầu dùng Claude Code.
