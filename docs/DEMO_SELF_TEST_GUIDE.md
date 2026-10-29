# HƯỚNG DẪN CHI TIẾT TỰ KIỂM THỬ (SELF-TEST) DEMO BACKEND EDUSENSE

Tài liệu này cung cấp kế hoạch và hướng dẫn từng bước cụ thể để bạn tự mình trải nghiệm, kiểm thử trực quan toàn bộ hệ thống Backend EduSense đang chạy trên máy tính.

---

## I. THÔNG TIN TRUY CẬP & TÀI KHOẢN MẪU CÓ SẴN

Hệ thống đã khởi tạo sẵn 2 tài khoản mẫu (đã băm mật khẩu bằng Argon2id và cấu hình quyền RBAC chuẩn trong PostgreSQL):

| Loại Tài Khoản | Email Đăng Nhập | Mật Khẩu | Vai Trò (Role) | Gói Dịch Vụ | Mục Đích Kiểm Thử |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **Sinh Viên (Student)** | `student@edusense.edu.vn` | `Student123!` | `student` | `free` | Dùng test toàn bộ tính năng học tập, AI, thanh toán SePay |
| **Quản Trị (Admin)** | `admin@edusense.edu.vn` | `AdminSecure123!` | `admin` | `pro` | Dùng test Quản trị tài khoản, Hạn ngạch AI, Báo cáo hệ thống |

### Các Đường Dẫn Trực Quan (Giao Diện Web)
- **Swagger UI (Interactive API Docs):** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc (Specification Docs):** [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **MinIO Storage S3 Console:** [http://localhost:9001](http://localhost:9001) *(User: `minioadmin` / Pass: `minioadmin`)*

---

## II. CÁCH 1: KIỂM THỬ TỰ ĐỘNG 1-CLICK (KHUYÊN DÙNG ĐẦU TIÊN)

Nếu bạn muốn kiểm tra nhanh toàn bộ luồng hoạt động từ đầu đến cuối mà không cần nhập thủ công, mở PowerShell và chạy:

```powershell
python scripts/run_demo.py
```

*Kịch bản sẽ tự động chạy trong 2 giây qua 9 bước:*
1. Kiểm tra liveness & readiness (PostgreSQL, Redis, MinIO).
2. Tự động tạo sinh viên mới bằng mật khẩu Argon2id.
3. Đăng nhập và lấy Access Token JWT.
4. Lấy thông tin cá nhân và hạn ngạch Free (10 request/ngày).
5. Tạo môn học mới.
6. Tạo bài tập lớn kèm deadline và trọng số.
7. Tổng hợp lịch học tập hợp nhất Calendar.
8. Sinh mã đơn hàng và đường dẫn ảnh VietQR SePay nâng cấp gói Pro.
9. Thử truy cập endpoint Admin để kiểm tra cơ chế chặn 403 Forbidden.

---

## III. CÁCH 2: HƯỚNG DẪN TỰ TEST TỪNG BƯỚC TRÊN SWAGGER UI

Mở trình duyệt truy cập: **[http://localhost:8000/docs](http://localhost:8000/docs)**

---

### KỊCH BẢN 1: ĐĂNG NHẬP & ỦY QUYỀN TRÊN SWAGGER

1. Cuộn đến nhóm **`Auth`** $\rightarrow$ chọn `POST /api/v1/auth/login`.
2. Bấm nút **Try it out**.
3. Nhập body JSON:
   ```json
   {
     "email": "student@edusense.edu.vn",
     "password": "Student123!"
   }
   ```
4. Bấm **Execute**.
5. **Kết quả mong đợi:** Mã `200 OK`. Trong Response Body, sao chép chuỗi `access_token` (chuỗi dài bắt đầu bằng `eyJhbGci...`).
6. **Kích hoạt quyền cho toàn bộ Swagger:**
   - Cuộn lên đầu trang Swagger UI, bấm vào nút **Authorize** màu xanh lá (hình ổ khóa góc trên bên phải).
   - Dán chuỗi `access_token` vừa copy vào ô **Value**.
   - Bấm **Authorize** $\rightarrow$ bấm **Close**.
   - *(Từ lúc này, mọi request bạn test trên Swagger đều tự động đính kèm Token này).*

---

### KỊCH BẢN 2: XEM HỒ SƠ SINH VIÊN & HẠN NGẠCH

1. Mở nhóm **`Users`** $\rightarrow$ chọn `GET /api/v1/users/me`.
2. Bấm **Try it out** $\rightarrow$ bấm **Execute**.
3. **Kết quả mong đợi:** Mã `200 OK`:
   ```json
   {
     "success": true,
     "data": {
       "email": "student@edusense.edu.vn",
       "full_name": "Nguyễn Văn Sinh Viên",
       "current_plan_code": "free",
       "is_email_verified": true
     }
   }
   ```

---

### KỊCH BẢN 3: TẠO MÔN HỌC & BÀI TẬP (ACADEMIC FLOW)

#### Bước 3.1: Tạo môn học mới
1. Mở nhóm **`Courses`** $\rightarrow$ chọn `POST /api/v1/courses`.
2. Bấm **Try it out**, nhập JSON:
   ```json
   {
     "code": "CS101",
     "name": "Nhập môn Trí tuệ Nhân tạo & RAG",
     "credits": 3,
     "semester": "HK1-2026",
     "color_code": "#2563EB"
   }
   ```
3. Bấm **Execute**.
4. **Kết quả mong đợi:** Mã `201 Created`. Sao chép mã `"id"` của môn học vừa tạo (ví dụ: `8316d00a-...`).

#### Bước 3.2: Tạo bài tập theo môn học
1. Mở nhóm **`Assignments`** $\rightarrow$ chọn `POST /api/v1/assignments`.
2. Bấm **Try it out**, nhập JSON (thay `course_id` bằng ID vừa copy ở trên):
   ```json
   {
     "course_id": "<DÁN_ID_MÔN_HỌC_VÀO_ĐÂY>",
     "title": "Báo cáo thực hành: Semantic Search với pgvector",
     "due_date": "2026-11-20T23:59:59Z",
     "priority": "high",
     "weight_percent": 30
   }
   ```
3. Bấm **Execute**.
4. **Kết quả mong đợi:** Mã `201 Created`.

#### Bước 3.3: Xem Lịch học tập hợp nhất
1. Mở nhóm **`Calendar`** $\rightarrow$ chọn `GET /api/v1/calendar`.
2. Bấm **Try it out**, điền parameters:
   - `start_date`: `2026-11-01`
   - `end_date`: `2026-11-30`
3. Bấm **Execute**.
4. **Kết quả mong đợi:** Mã `200 OK`, danh sách sự kiện tự động tổng hợp bài tập vừa tạo vào ngày 20/11/2026.

---

### KỊCH BẢN 4: NÂNG CẤP GÓI PRO & THANH TOÁN VIETQR SEPAY

1. Mở nhóm **`Payments`** $\rightarrow$ chọn `POST /api/v1/payments/create-qr`.
2. Bấm **Try it out**, nhập JSON:
   ```json
   {
     "plan_code": "pro"
   }
   ```
3. Bấm **Execute**.
4. **Kết quả mong đợi:** Mã `201 Created`:
   ```json
   {
     "success": true,
     "data": {
       "payment_id": "...",
       "reference_code": "EDU...",
       "amount_vnd": 50000,
       "qr_url": "https://qr.sepay.vn/img?acc=0123456789&bank=MBBank&amount=50000&des=EDU...",
       "status": "pending"
     }
   }
   ```
5. **Kiểm tra trực quan:** Bạn có thể copy đường link trong trường `qr_url` dán vào tab trình duyệt mới để xem ảnh mã VietQR SePay thực tế!

---

### KỊCH BẢN 5: KIỂM THỬ BẢO MẬT & PHÂN QUYỀN RBAC (STRICT RBAC)

Kịch bản này chứng minh tính bảo mật tuyệt đối của hệ thống:

#### Thử nghiệm 5.1: Tài khoản Sinh viên gọi API Admin (Phải bị chặn 403)
1. Đảm bảo Swagger vẫn đang dùng token của `student@edusense.edu.vn`.
2. Mở nhóm **`Admin - System`** $\rightarrow$ chọn `GET /api/v1/admin/system/overview`.
3. Bấm **Try it out** $\rightarrow$ **Execute**.
4. **Kết quả mong đợi:**
   - HTTP Status: **`403 Forbidden`**
   - Nội dung phản hồi:
     ```json
     {
       "detail": "Admin privileges required."
     }
     ```
   - $\rightarrow$ **Xác nhận:** Sinh viên hoàn toàn không thể xem dữ liệu quản trị!

#### Thử nghiệm 5.2: Đổi sang tài khoản Admin (Thành công 200)
1. Mở lại `POST /api/v1/auth/login`, đăng nhập tài khoản Admin:
   ```json
   {
     "email": "admin@edusense.edu.vn",
     "password": "AdminSecure123!"
   }
   ```
2. Copy `access_token` mới của Admin.
3. Bấm nút **Authorize** ở góc trên Swagger, dán token mới vào và bấm **Authorize**.
4. Thử gọi lại `GET /api/v1/admin/system/overview` $\rightarrow$ Bấm **Execute**.
5. **Kết quả mong đợi:**
   - HTTP Status: **`200 OK`**
   - Hiển thị đầy đủ số liệu Dashboard toàn hệ thống: tổng sinh viên, tổng số tài liệu, dung lượng lưu trữ, tổng vector chunks, và thống kê worker!

---

### KỊCH BẢN 6: KIỂM TRA HẠ TẦNG OBJECT STORAGE TRÊN MINIO

1. Mở trình duyệt: **[http://localhost:9001](http://localhost:9001)**
2. Đăng nhập:
   - **Username:** `minioadmin`
   - **Password:** `minioadmin`
3. Chọn mục **Buckets** ở menu bên trái.
4. **Kết quả mong đợi:** Bạn sẽ thấy bucket **`edusense-documents`** được thiết lập riêng tư (Access Policy: Private), sẵn sàng lưu trữ tài liệu PDF, DOCX của sinh viên an toàn.

---

## IV. BẢNG TỔNG KẾT KẾT QUẢ KIỂM THỬ (TEST RESULT CHECKLIST)

| Kịch Bản | Mục Tiêu Kiểm Thử | Endpoint Kiểm Tra | Kết Quả Thực Tế | Đánh Giá |
| :---: | :--- | :--- | :---: | :---: |
| **KB 1** | Đăng nhập JWT & Argon2id | `POST /api/v1/auth/login` | `200 OK` | ✅ PASS |
| **KB 2** | Lấy thông tin hồ sơ | `GET /api/v1/users/me` | `200 OK` | ✅ PASS |
| **KB 3** | Tạo môn học & bài tập | `POST /api/v1/courses`, `/assignments` | `201 Created` | ✅ PASS |
| **KB 4** | Lịch học hợp nhất | `GET /api/v1/calendar` | `200 OK` | ✅ PASS |
| **KB 5** | Tạo đơn VietQR SePay | `POST /api/v1/payments/create-qr` | `201 Created` | ✅ PASS |
| **KB 6** | Chặn sinh viên truy cập Admin | `GET /api/v1/admin/system/overview` | `403 Forbidden` | ✅ PASS |
| **KB 7** | Cho phép Admin xem Dashboard | `GET /api/v1/admin/system/overview` | `200 OK` | ✅ PASS |
| **KB 8** | Kiểm tra Bucket MinIO | MinIO Console `:9001` | Private Bucket OK | ✅ PASS |
