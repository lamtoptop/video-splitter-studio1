from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/video-splitter"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
MAX_FILES = int(os.getenv("MAX_FILES_PER_JOB", "100"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024 * 1024)))  # 8 GiB total
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "7200"))
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpeg", ".mpg"}

app = FastAPI(title="Video Splitter Studio API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For production, restrict to your frontend domain.
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
stop_flags: dict[str, threading.Event] = {}


def safe_name(name: str) -> str:
    stem = Path(name).stem
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)
    return cleaned[:70].strip("._") or "video"


def run_command(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def probe_duration(path: Path) -> float:
    p = run_command([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ])
    if p.returncode != 0:
        raise RuntimeError(f"Không đọc được video: {p.stderr[-500:]}")
    try:
        duration = float(p.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Không xác định được thời lượng video.") from exc
    if duration <= 0:
        raise RuntimeError("Video không có thời lượng hợp lệ.")
    return duration


def update_job(job_id: str, **kwargs):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)
            jobs[job_id]["updated_at"] = time.time()


def process_job(job_id: str):
    job = jobs[job_id]
    job_dir = Path(job["dir"])
    input_dir = job_dir / "input"
    output_dir = job_dir / "parts"
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = job_dir / "video_parts.zip"
    mode = job["mode"]
    amount = job["amount"]
    quality = job["quality"]
    preset, crf = {
        "fast": ("ultrafast", "25"),
        "balanced": ("veryfast", "22"),
        "high": ("medium", "18"),
    }[quality]

    try:
        total_parts = 0
        # Probe first to compute a stable overall progress denominator.
        video_info = []
        for input_path in job["inputs"]:
            p = Path(input_path)
            duration = probe_duration(p)
            count = int(__import__("math").ceil(duration / amount)) if mode == "duration" else int(amount)
            total_parts += count
            video_info.append((p, duration, count))
        update_job(job_id, status="processing", total_parts=total_parts, completed_parts=0, progress=0,
                   message=f"Đã nhận {len(video_info)} video. Bắt đầu xử lý…")

        completed = 0
        for file_index, (input_path, duration, count) in enumerate(video_info, start=1):
            if stop_flags[job_id].is_set():
                break
            segment = amount if mode == "duration" else duration / count
            stem = f"video_{file_index:03d}_{safe_name(input_path.name)}"
            for part_index in range(count):
                if stop_flags[job_id].is_set():
                    break
                start = part_index * segment
                length = min(segment, duration - start)
                if length <= 0:
                    break
                out_name = f"{stem}_part_{part_index + 1:03d}.mp4"
                out_path = output_dir / out_name
                update_job(job_id, message=f"Video {file_index}/{len(video_info)}: {input_path.name} · part {part_index + 1}/{count}")
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{start:.6f}", "-i", str(input_path), "-t", f"{length:.6f}",
                    "-map", "0:v:0?", "-map", "0:a:0?",
                    "-c:v", "libx264", "-preset", preset, "-crf", crf,
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", "-sn", "-dn", str(out_path)
                ]
                result = run_command(cmd)
                if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
                    detail = (result.stderr or "FFmpeg không tạo được file đầu ra")[-1000:]
                    raise RuntimeError(f"Lỗi khi cắt {input_path.name}, part {part_index + 1}: {detail}")
                completed += 1
                update_job(job_id, completed_parts=completed,
                           progress=round(completed / max(total_parts, 1) * 100, 1))

        if stop_flags[job_id].is_set():
            update_job(job_id, status="stopped", message="Đã dừng theo yêu cầu. Các part đã hoàn thành vẫn có thể tải.")
        parts = sorted(output_dir.glob("*.mp4"))
        if not parts:
            raise RuntimeError("Không tạo được part nào.")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for part in parts:
                zf.write(part, arcname=part.name)
        current = jobs[job_id]["status"]
        if current != "stopped":
            update_job(job_id, status="completed", progress=100, message=f"Hoàn tất: {len(parts)} part.")
        update_job(job_id, part_count=len(parts), zip_path=str(zip_path))
    except Exception as exc:
        update_job(job_id, status="error", error=str(exc), message="Có lỗi khi xử lý video.")
    finally:
        # Remove uploaded source files after processing; keep output ZIP until TTL expires.
        shutil.rmtree(input_dir, ignore_errors=True)


async def save_upload(upload: UploadFile, destination: Path, total_so_far: int) -> int:
    size = 0
    with destination.open("wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if total_so_far + size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"Tổng dung lượng upload vượt giới hạn {MAX_UPLOAD_BYTES // (1024**3)} GiB.")
            out.write(chunk)
    await upload.close()
    if size == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(400, f"File rỗng: {upload.filename}")
    return size


def cleanup_expired():
    now = time.time()
    with jobs_lock:
        expired = [jid for jid, j in jobs.items() if now - j.get("created_at", now) > JOB_TTL_SECONDS and j.get("status") not in ("queued", "processing")]
        for jid in expired:
            shutil.rmtree(jobs[jid]["dir"], ignore_errors=True)
            jobs.pop(jid, None)
            stop_flags.pop(jid, None)
    # Also clean orphan directories left by restarts.
    for child in DATA_DIR.iterdir():
        if child.is_dir() and now - child.stat().st_mtime > JOB_TTL_SECONDS:
            shutil.rmtree(child, ignore_errors=True)


@app.on_event("startup")
async def startup():
    async def janitor():
        while True:
            await asyncio.sleep(300)
            cleanup_expired()
    asyncio.create_task(janitor())


@app.get("/api/health")
def health():
    return {"ok": True, "service": "video-splitter", "max_files": MAX_FILES}


@app.post("/api/jobs")
async def create_job(
    files: list[UploadFile] = File(...),
    mode: Literal["duration", "count"] = Form("duration"),
    amount: float = Form(60),
    quality: Literal["fast", "balanced", "high"] = Form("balanced"),
):
    cleanup_expired()
    if not files:
        raise HTTPException(400, "Vui lòng chọn ít nhất một video.")
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"Mỗi lượt tối đa {MAX_FILES} video.")
    if amount < 1 or (mode == "count" and (not amount.is_integer() or amount > 300)):
        raise HTTPException(400, "Giá trị chia video không hợp lệ. Số phần phải là số nguyên từ 1 đến 300.")
    if mode == "duration" and amount > 86400:
        raise HTTPException(400, "Thời lượng mỗi part tối đa 86400 giây.")

    job_id = uuid.uuid4().hex
    job_dir = DATA_DIR / job_id
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    inputs, total_bytes = [], 0

    try:
        for index, upload in enumerate(files, start=1):
            original = upload.filename or f"video_{index}.mp4"
            ext = Path(original).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                raise HTTPException(415, f"Không hỗ trợ định dạng file: {original}")
            destination = input_dir / f"{index:03d}_{safe_name(original)}{ext}"
            total_bytes += await save_upload(upload, destination, total_bytes)
            inputs.append(str(destination))
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    now = time.time()
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id, "status": "queued", "progress": 0, "completed_parts": 0,
            "total_parts": None, "part_count": 0, "message": "Đã upload xong. Đang chờ xử lý…",
            "error": None, "dir": str(job_dir), "inputs": inputs, "mode": mode,
            "amount": int(amount) if mode == "count" else amount, "quality": quality,
            "created_at": now, "updated_at": now, "zip_path": None,
        }
        stop_flags[job_id] = threading.Event()

    threading.Thread(target=process_job, args=(job_id,), daemon=True).start()
    return {"job_id": job_id, "status": "queued", "file_count": len(inputs), "total_bytes": total_bytes}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Không tìm thấy job hoặc job đã hết hạn.")
        return {k: job.get(k) for k in (
            "id", "status", "progress", "completed_parts", "total_parts", "part_count",
            "message", "error", "created_at", "updated_at"
        )}


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    with jobs_lock:
        if job_id not in jobs:
            raise HTTPException(404, "Không tìm thấy job.")
        if jobs[job_id]["status"] not in ("queued", "processing"):
            return {"ok": True, "status": jobs[job_id]["status"]}
        stop_flags[job_id].set()
    return {"ok": True, "message": "Sẽ dừng sau part hiện tại."}


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job không tồn tại hoặc đã hết hạn.")
        path = job.get("zip_path")
        status = job.get("status")
    if not path or not Path(path).exists() or status not in ("completed", "stopped"):
        raise HTTPException(409, "ZIP chưa sẵn sàng.")
    return FileResponse(path, filename=f"video_parts_{job_id[:8]}.zip", media_type="application/zip")


app.mount("/", StaticFiles(directory=APP_DIR / "static", html=True), name="static")
