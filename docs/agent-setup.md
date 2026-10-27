# EduSense — Antigravity & Claude Code Agent Setup

Tài liệu này mô tả **đúng hiện trạng** cấu hình agent trong repo. Khi thêm, xóa hoặc đổi tên rule/skill/agent,
cập nhật tài liệu này trong cùng commit.

## Cấu trúc

```text
AGENTS.md           Chỉ dẫn chung cho mọi agent (Antigravity, Claude Code, ...)
CLAUDE.md           Import AGENTS.md + con trỏ tới rule/skill cho Claude Code
.agents/
├── rules/      7 rule: ai-rag, architecture, backend, database, frontend-ui, security, testing
├── agents/     5 agent: edusense-security-reviewer, stitch-designer, ui-art-director,
│               ui-reviewer, vue-frontend-builder
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
- Chưa xác minh tên tool khai báo trong `edusense-security-reviewer.md` (`view_file`, `grep_search`, `run_command`).
- Chưa có `.claude/skills/` và `.claude/agents/`; tạo khi nhóm bắt đầu dùng Claude Code.
