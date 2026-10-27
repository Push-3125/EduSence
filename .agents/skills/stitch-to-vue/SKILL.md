---
name: stitch-to-vue
description: >-
  Converts Stitch HTML designs into modular Vue 3 Single File Components (SFC)
  with Composition API (<script setup lang="ts">), Tailwind CSS, TypeScript props,
  and decoupled mock data/composables.
---

# Stitch to Vue 3 Components

Kỹ năng này chuyên trách chuyển đổi mã HTML từ Google Stitch thành các linh kiện Vue 3 chuẩn mực, tuân thủ tuyệt đối quy định kiến trúc của dự án EduSense (AGENTS.md).

## Quy trình chuyển đổi (5 Pha)

### Pha 1: Tải và kiểm tra thiết kế
1. Gọi `get_screen` để lấy `htmlCode.downloadUrl` và `screenshot.downloadUrl`.
2. Tải HTML thiết kế về lưu trữ tại `.stitch/designs/<screen-slug>.html`.
3. Kiểm tra bố cục và intent từ ảnh chụp màn hình tương ứng.

### Pha 2: Trích xuất Design Tokens & Tailwind Config
1. Đọc thẻ `<script id="tailwind-config">` từ HTML của Stitch.
2. Đối chiếu với tài liệu chuẩn tại [docs/DESIGN.md](docs/DESIGN.md).
3. Đảm bảo toàn bộ class màu sắc (`bg-surface`, `text-primary`, `bg-secondary-container`...) được ánh xạ chính xác trong cấu hình Tailwind của dự án. Không hardcode mã màu hex trực tiếp trong component.

### Pha 3: Bóc tách cấu trúc theo nguyên lý Vue 3
1. **Layout Shell**:
   - Tách phần thanh điều hướng (`<aside>`, Header, Breadcrumbs) vào `src/layouts/` hoặc `src/components/layout/`.
   - Thay thế toàn bộ thẻ neo `href="#"` bằng `<RouterLink :to="...">`. Logo hoặc thương hiệu EduSense luôn trỏ về `/`.
2. **Component hóa (Atomic / Modular Components)**:
   - Tách nhỏ các khối lặp lại thành component con trong `src/components/common/` hoặc `src/components/<feature>/`:
     * Thẻ bài tập / thẻ môn học (`TaskCard.vue`, `CourseCard.vue`)
     * Thanh đo tiến độ (`ProgressBar.vue`)
     * Huy hiệu trạng thái (`StatusBadge.vue`)
     * Modal xác nhận / Drawer trợ lý AI (`AiChatDrawer.vue`)
     * Bảng dữ liệu / Danh sách lọc (`DataTable.vue`, `FilterPanel.vue`)
3. **Logic Isolation (Composables)**:
   - Đưa logic xử lý trạng thái, tính toán, phân trang, lọc sang composable riêng trong `src/composables/`:
     * Ví dụ: `usePagination.ts`, `useTaskFilter.ts`, `useAiGrading.ts`.
4. **Data Decoupling (Mock Data & Types)**:
   - Toàn bộ dữ liệu tĩnh, text mẫu, danh sách giả lập được đưa vào `src/data/mock/<feature>.ts`.
   - Khai báo rõ ràng TypeScript interface trong `src/types/<feature>.ts`.
   - Mọi component phải định nghĩa Props bằng `defineProps<Props>()` với TypeScript interface.

### Pha 4: Hiện thực hóa Vue SFC (`<script setup lang="ts">`)
```vue
<script setup lang="ts">
import { computed } from 'vue'
import type { TaskItem } from '@/types/task'

interface Props {
  task: TaskItem
  isCompact?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  isCompact: false
})

const emit = defineEmits<{
  (e: 'submit', id: string): void
  (e: 'viewFeedback', id: string): void
}>()
</script>

<template>
  <div class="bg-surface-container-lowest rounded-xl p-4 shadow-sm border border-outline-variant/30 flex items-center justify-between">
    <!-- UI Markup from Stitch with Vue directives -->
  </div>
</template>
```

### Pha 5: Kiểm tra các trạng thái bắt buộc (UI States)
Theo quy định tại `frontend-ui.md`, mỗi màn hình hoặc khối dữ liệu phải xử lý đủ 5 trạng thái:
1. **Loading state**: Skeleton placeholders tương ứng cấu trúc giao diện.
2. **Empty state**: Hình minh họa hoặc icon + lời nhắc hành động rõ ràng khi chưa có dữ liệu.
3. **Error state**: Thông báo thân thiện, không lộ stacktrace, có nút "Thử lại".
4. **Success state**: Hiển thị dữ liệu chính xác.
5. **Disabled / Read-only state**: Khi sinh viên đã nộp bài hoặc bị khóa quyền.
