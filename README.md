# Video Splitter Studio — Server-side Cloud

Web app chia nhiều video thành các part MP4. FFmpeg được cài trong Docker container trên cloud server; người dùng không cần cài FFmpeg/Python.

## Chức năng
- Upload tối đa 100 video/lượt.
- Xem trước video đầu tiên ngay trên trình duyệt trước khi upload.
- Chia theo thời lượng mỗi part hoặc số part bằng nhau.
- Chọn chất lượng encode: nhanh / cân bằng / cao.
- Server xử lý tuần tự, theo dõi tiến trình, dừng sau part hiện tại.
- Tải toàn bộ kết quả trong một file ZIP.
- File nguồn được xóa sau khi xử lý; ZIP và dữ liệu job tự xóa sau tối đa 2 giờ (janitor chạy định kỳ).

## Chạy local bằng Docker
Yêu cầu Docker Desktop.
```bash
docker build -t video-splitter .
docker run --rm -p 8000:8000 -e MAX_FILES_PER_JOB=100 video-splitter
```
Mở http://localhost:8000

## Deploy cloud bằng Render (Docker)
1. Tạo tài khoản tại https://render.com/
2. Tạo GitHub repository mới, upload toàn bộ nội dung project này.
3. Trong Render chọn **New +** → **Blueprint** (hoặc Web Service).
4. Kết nối GitHub repo; Render đọc `render.yaml`, build từ Dockerfile.
5. Chờ deploy hoàn tất. Mở URL `https://<service-name>.onrender.com`.
6. Kiểm tra `https://<service-name>.onrender.com/api/health` phải trả JSON `{"ok":true,...}`.

Nếu tạo Web Service thủ công:
- Runtime: Docker
- Dockerfile path: `./Dockerfile`
- Health check path: `/api/health`
- Environment variables:
  - `MAX_FILES_PER_JOB=100`
  - `MAX_UPLOAD_BYTES=8589934592` (8 GiB tổng mỗi job)
  - `JOB_TTL_SECONDS=7200` (2 giờ)

## Lưu ý chi phí và giới hạn cloud
- Đây là backend thật: video được upload lên cloud và tiêu tốn băng thông, CPU, RAM và ổ đĩa. Chọn gói máy chủ phù hợp với dung lượng video và số job đồng thời.
- Gói miễn phí của một số nền tảng có thể ngủ khi không dùng, giới hạn thời gian request, dung lượng ổ đĩa hoặc băng thông; không phù hợp xử lý video nặng/100 file lớn. Kiểm tra giới hạn hiện hành trước khi chọn gói.
- Mỗi job tối đa 100 file và tổng upload mặc định 8 GiB. Có thể điều chỉnh `MAX_UPLOAD_BYTES`.
- Job lưu trạng thái trong RAM và file tạm trên filesystem của container. Dùng một instance; không bật nhiều instance/replica vì trạng thái job không chia sẻ giữa các instance. Restart/deploy có thể làm mất job đang chạy.
- Filesystem mặc định là tạm thời. Không bật persistent disk nếu muốn dữ liệu biến mất khi container restart; cleanup tự xóa theo TTL 2 giờ.
- Upload nhiều video qua một HTTP request. Với file rất lớn, proxy/hosting có thể có giới hạn request body/time; cần tăng giới hạn hoặc chuyển sang upload trực tiếp object storage theo multipart/resumable.
- Chỉ upload video bạn có quyền sử dụng. Không đưa dữ liệu riêng tư/nhạy cảm lên máy chủ công cộng.

## API
- `GET /api/health` — health check
- `POST /api/jobs` — multipart form: `files` (repeat), `mode=duration|count`, `amount`, `quality=fast|balanced|high`
- `GET /api/jobs/{job_id}` — trạng thái job
- `POST /api/jobs/{job_id}/stop` — yêu cầu dừng
- `GET /api/jobs/{job_id}/download` — tải ZIP khi hoàn tất

## Cấu trúc
```text
video-splitter-server/
├── app/
│   ├── main.py
│   └── static/index.html
├── Dockerfile
├── requirements.txt
├── render.yaml
├── .dockerignore
└── README.md
```
