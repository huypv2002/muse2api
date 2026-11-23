#!/usr/bin/env python3
"""Muse desktop — hàng đợi Prompt → Ảnh → Video → gộp clip.

Chạy: python3 gui_app.py
"""
from __future__ import annotations

import base64
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QSettings, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QHeaderView, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import (
    CFG, ImageRequest, VideoRequest, build_image_prompt, build_video_prompt,
    parse_cookie_text, store,
)
from engine import MuseEngine
from refs import (
    TAG_RE, RefMemory, compose_prompt, load_characters, resolve_refs, save_characters, tags_in,
)
from auth_client import DEFAULT_AUTH_URL, AuthError, login as auth_login, logout as auth_logout, me as auth_me
from cdp import http_json
from engine import MuseAuthError, MuseGenerationError, MuseProxyError
from updater import (
    DEFAULT_REPO, apply_update_and_restart, check_for_updates, download_file,
    get_current_version,
)

PAGE_SIZE = 12

MODES = [
    ("pipeline", "Prompt → Ảnh → Video → Gộp"),
    ("t2v", "Text → Video"),
    ("t2i", "Text → Ảnh"),
    ("i2v", "Ảnh → Video"),
]


_NOTE_VI = (
    (re.compile(r"会话有效 · 自动保活 \(VM: ([^)]*)\)"), r"Phiên còn hiệu lực · tự giữ kết nối (VM: \1)"),
    (re.compile(r"保活认证失败: (.*)"), r"Giữ phiên thất bại: \1"),
    (re.compile(r"保活未确认（保留账号状态）: (.*)"), r"Chưa xác nhận phiên, giữ trạng thái cũ: \1"),
    (re.compile(r"会话有效；额度读取失败"), "Phiên còn hiệu lực; không đọc được hạn mức"),
    (re.compile(r"会话有效，可正常生成"), "Phiên còn hiệu lực, tạo được"),
    (re.compile(r"会话有效"), "Phiên còn hiệu lực"),
)


def _note_vi(note: str) -> str:
    text = note or ""
    for pattern, repl in _NOTE_VI:
        text = pattern.sub(repl, text)
    return text


_ERROR_VI = (
    ("任务已取消", "Đã hủy tác vụ"),
    ("任务准备超时，已停止发送", "Hết thời gian chuẩn bị gửi prompt"),
    ("提示词发送未确认，已停止生成", "Chưa xác nhận đã gửi prompt"),
    ("等待生成超时，未出现新的生成结果", "Hết thời gian chờ, chưa có kết quả mới"),
    ("已生成但未能取回文件", "Đã tạo nhưng không tải được file"),
    ("页面只返回了本次上传的参考素材，未出现新的生成结果", "Chỉ thấy ảnh tham chiếu, chưa có video mới"),
    ("等待助手首字响应超时", "Hết thời gian chờ Muse trả lời"),
    ("等待助手回复超时", "Hết thời gian chờ Muse trả lời"),
    ("云端 VM 连接超时 (Still sending)", "Máy ảo Muse kết nối quá lâu (Still sending)"),
    ("找不到聊天输入框", "Không thấy ô nhập trên trang Muse"),
    ("找不到上传控件，已停止生成", "Không thấy chỗ tải ảnh trên trang Muse"),
    ("附加参考素材失败，已停止生成", "Không gắn được ảnh tham chiếu, đã dừng"),
    ("客户端已断开连接，终止生成任务", "Kết nối bị ngắt, đã dừng"),
    ("账号额度不足", "Tài khoản hết hạn mức"),
    ("Chromium 启动失败", "Không mở được trình duyệt Chromium"),
    ("Chromium 未返回独立上下文 ID", "Trình duyệt không mở được phiên riêng"),
    ("Chromium 未返回独立页面 ID", "Trình duyệt không mở được trang"),
    ("页面网关连接超时 (Connecting...)，正在自动切换账号", "Trang Muse kết nối quá lâu (Connecting...)"),
    ("muse.ai 页面加载超时（未出现聊天输入框），请检查服务器网络后重试；未确认会话失效", "Trang Muse tải quá lâu, không thấy ô nhập"),
    ("参考图 Data URL 格式无效", "Ảnh tham chiếu không đúng định dạng"),
    ("参考图 Data URL 编码无效", "Ảnh tham chiếu bị lỗi mã hóa"),
    ("参考图必须是 URL、Data URL、本地图片或有效 Base64", "Ảnh tham chiếu không hợp lệ"),
    ("无法确认参考视频时长，请提供带有效时长信息的 MP4/MOV/WebM/3GP 文件", "Không đọc được thời lượng video tham chiếu"),
    ("账号代理认证失败 (HTTP 407)，请检查代理用户名和密码", "Proxy từ chối đăng nhập (HTTP 407)"),
    ("账号代理认证失败 (HTTP 407)，未完成 VM 唤醒", "Proxy từ chối đăng nhập (HTTP 407)"),
    ("账号代理认证失败 (HTTP 407)，未完成 VM 状态确认", "Proxy từ chối đăng nhập (HTTP 407)"),
    ("账号代理浏览器连接失败，请检查节点或认证；未回退直连", "Không kết nối được proxy"),
    ("提示词未成功送达后端（Still sending / Delivery not confirmed），终止等待并重试", "Prompt chưa tới Muse, đang thử lại"),
    ("额度面板未读到有效用量；旧缓存未覆盖", "Không đọc được hạn mức"),
    ("额度面板未读到有效数据；旧缓存未覆盖", "Không đọc được hạn mức"),
    ("会话已被 muse.ai 登出", "Tài khoản bị muse.ai đăng xuất. Hãy nhập lại cookie"),
    ("无法创建该账号的独立浏览器会话", "Không tạo được phiên trình duyệt riêng cho tài khoản"),
)

_CJK = re.compile(r"[\u3400-\u9fff]")


def _vi_error(text: str) -> str:
    raw = text or ""
    if raw.startswith("模型未生成媒体"):
        return "Muse chỉ trả lời chữ, không xuất video. Đang tạo lại..."
    raw = re.sub(r"提示词没有发出去（.*?）.*", "Prompt chưa được gửi đi, đang tạo lại...", raw)
    raw = re.sub(r"期望生成视频，但.*?图片.*", "Muse trả ảnh thay vì video, đang tạo lại...", raw)
    raw = re.sub(r"未能取回生成结果:.*", "Không lấy được file đã tạo, đang tạo lại...", raw)
    raw = re.sub(r"第 (\d+) 个参考素材读取失败.*", r"Không đọc được ảnh tham chiếu số \1.", raw)
    raw = re.sub(r"第 (\d+) 个参考素材编码无效", r"Ảnh tham chiếu số \1 bị lỗi.", raw)
    raw = re.sub(r"第 (\d+) 个参考素材.*", r"Ảnh tham chiếu số \1 không hợp lệ.", raw)
    raw = re.sub(r"参考素材最多 (\d+) 个", r"Tối đa \1 ảnh tham chiếu.", raw)
    raw = re.sub(r"/api/session HTTP (\d+).*", r"Phiên Muse lỗi HTTP \1. Hãy nhập lại cookie.", raw)
    for zh, vi in _ERROR_VI:
        raw = raw.replace(zh, vi)
    if _CJK.search(raw):
        return "Đã xảy ra lỗi tạm thời khi tạo. Đang tạo lại..."
    return raw[:240]


def _slug(text: str, limit: int = 36) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", text.strip(), flags=re.UNICODE).strip("_")
    return (cleaned[:limit] or "clip")


def _data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as handle:
        payload = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def _cdp_in_use() -> bool:
    try:
        http_json(f"http://127.0.0.1:{CFG.cdp_port}/json/version", timeout=0.4)
        return True
    except Exception:
        return False


def merge_videos(paths: list[str], dest: str) -> None:
    list_path = dest + ".concat.txt"
    with open(list_path, "w", encoding="utf-8") as handle:
        for path in paths:
            escaped = path.replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")
    try:
        copy_cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy", dest,
        ]
        done = subprocess.run(copy_cmd, capture_output=True, text=True)
        if done.returncode == 0:
            return
        encode_cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
            dest,
        ]
        encoded = subprocess.run(encode_cmd, capture_output=True, text=True)
        if encoded.returncode != 0:
            tail = (encoded.stderr or done.stderr or "")[-400:]
            raise RuntimeError(tail or "ffmpeg thất bại")
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)


class BatchWorker(QThread):
    job_changed = Signal(int, str, str)
    progress = Signal(int, int)
    logged = Signal(str)
    batch_finished = Signal(str)

    def __init__(self, jobs: list[dict], account_id: str, out_dir: str,
                 merge: bool, stop_event: threading.Event, characters: list[dict],
                 account_ids: list[str] | None = None, plan_threads: int = 0):
        super().__init__()
        self.jobs = jobs
        self.account_id = account_id
        self.account_ids = account_ids or [account_id]
        self.plan_threads = max(0, int(plan_threads or 0))
        self.out_dir = out_dir
        self.merge = merge
        self.stop_event = stop_event
        self.characters = characters
        self._tls = threading.local()
        self.memory = RefMemory(os.path.join(CFG.data_dir, "ref_uploads.json"), account_id)
        self._hash_lock = threading.Lock()
        self._claimed_hashes: set[str] = set()

    def _eng(self) -> MuseEngine:
        return getattr(self._tls, "engine", None)

    def _mem(self) -> RefMemory:
        return getattr(self._tls, "memory", None) or self.memory

    def _log(self, text: str):
        self.logged.emit(text)

    def _account(self) -> dict:
        account_id = getattr(self._tls, "account_id", None) or self.account_id
        account = store.get_account(account_id)
        if not account or not account.get("cookies"):
            raise MuseAuthError("Tài khoản không còn cookie. Hãy nhập lại.")
        return account

    def _claim_hash(self, digest: str) -> bool:
        with self._hash_lock:
            if digest in self._claimed_hashes:
                return False
            self._claimed_hashes.add(digest)
            return True

    def _generate(self, prompt: str, kind: str, timeout: int,
                  references: list[str] | None = None, fresh: bool = True, index: int = -1):
        account = self._account()

        def on_progress(value):
            if index >= 0:
                self.progress.emit(index, 15 + int(value) * 70 // 100)

        eng = self._eng()
        eng.start()
        with self._hash_lock:
            skip = set(self._claimed_hashes)
        return eng.generate(
            account["cookies"], prompt, expect=kind, timeout=timeout,
            expires=account.get("cookies_exp"), account_id=account["id"],
            reference_images=references, stop_event=self.stop_event,
            fresh_thread=fresh, on_progress=on_progress,
            skip_hashes=skip, claim_hash=self._claim_hash,
        )

    def _render(self, prompt: str, kind: str, timeout: int, aspect: str,
                duration: int, extra_path: str = "", index: int = -1):
        pairs, error = resolve_refs(prompt, self.characters, extra_paths=extra_path if isinstance(extra_path, list) else ([extra_path] if extra_path else []))
        if error:
            raise MuseGenerationError(error)
        if not pairs:
            built = (build_image_prompt(ImageRequest(prompt=prompt, aspect_ratio=aspect, size=aspect))
                     if kind == "image" else
                     build_video_prompt(VideoRequest(
                         prompt=prompt, duration=duration, aspect_ratio=aspect, size=aspect)))
            return self._generate(built, kind, timeout, None, True, index)
        memory = self._mem()
        legend, uploads = memory.plan(pairs)
        fresh = not memory.url
        if memory.url:
            account = self._account()
            eng = self._eng()
            eng.start()
            eng.ensure_page(account["cookies"], account.get("cookies_exp"), account_id=account["id"])
            if not eng.focus_thread(memory.url):
                self._log("Hội thoại cũ không mở lại được, đăng ảnh lại từ đầu.")
                memory.reset()
                legend, uploads = memory.plan(pairs)
                fresh = True
            else:
                fresh = False
                if uploads:
                    self._log("Giữ ảnh đã đăng, chỉ đăng ảnh còn thiếu.")
                else:
                    self._log("Ảnh đã có trên tài khoản này, bỏ qua upload.")
        text = compose_prompt(prompt, aspect, duration, legend, kind)
        references = [_data_url(path) for path, _digest in uploads] or None
        if references:
            self._log(f"Đăng {len(references)} ảnh mới.")
        result = self._generate(text, kind, timeout, references, fresh, index)
        memory.commit(self._eng().page_url())
        return result

    def _save(self, src: str, name: str) -> str:
        os.makedirs(self.out_dir, exist_ok=True)
        dest = os.path.join(self.out_dir, name)
        shutil.copy2(src, dest)
        return dest

    def _one(self, index: int, job: dict) -> str | None:
        prompt = job["prompt"]
        mode = job["mode"]
        aspect = job["aspect"]
        duration = job["duration"]
        stamp = f"{index + 1:03d}_{_slug(prompt)}"

        extra = ""
        if mode in ("t2i", "pipeline"):
            if self.stop_event.is_set():
                self.job_changed.emit(index, "Đã dừng", "")
                return None
            cached_image = job.get("cached_image")
            if cached_image and os.path.isfile(cached_image) and os.path.getsize(cached_image) > 1024:
                image_file = cached_image
                self._log(f"Card {index + 1}: Sử dụng ảnh đã tạo trước đó ({os.path.basename(image_file)}), tiếp tục tạo video…")
            else:
                self.job_changed.emit(index, "Đang chạy", "")
                self.progress.emit(index, 8)
                image = self._render(prompt, "image", CFG.image_timeout, aspect, duration, "", index)
                if not image or not image.get("path") or not os.path.isfile(image["path"]) or os.path.getsize(image["path"]) < 1024:
                    raise MuseGenerationError("File ảnh sinh ra không hợp lệ hoặc bị rỗng, cần tạo lại.")
                image_file = self._save(image["path"], f"{stamp}{image['ext']}")
                job["cached_image"] = image_file
                self._log(f"Card {index + 1} - Ảnh: {image_file}")

            self.job_changed.emit(index, "Đã có ảnh" if mode == "t2i" else "Đã có ảnh, đang tạo video", image_file)
            if mode == "t2i":
                self.job_changed.emit(index, "Xong", image_file)
                return image_file
            extra = [image_file, *list(job.get("images") or [])]
        elif mode == "i2v":
            extra = list(job.get("images") or [])
            if not extra and not tags_in(prompt):
                raise MuseGenerationError("Chưa thêm ảnh hoặc @nhân vật")

        if self.stop_event.is_set():
            self.job_changed.emit(index, "Đã dừng", "")
            return None
        self.job_changed.emit(index, "Đang chạy", "")
        self.progress.emit(index, 12)
        video = self._render(prompt, "video", CFG.video_timeout, aspect, duration, extra, index)
        if not video or not video.get("path") or not os.path.isfile(video["path"]) or os.path.getsize(video["path"]) < 1024:
            raise MuseGenerationError("File video sinh ra không hợp lệ hoặc bị rỗng, cần tạo lại.")
        video_path = self._save(video["path"], f"{stamp}{video['ext']}")
        self._log(f"Video #{index + 1} ({_slug(prompt)}): {video_path}")
        self.job_changed.emit(index, "Xong", video_path)
        return video_path

    def _account_label(self, account_id: str) -> str:
        return (store.get_account(account_id) or {}).get("label") or account_id[:8]

    def _is_dead_account(self, exc: Exception) -> bool:
        """Chỉ trả về True nếu tài khoản bị hỏng vĩnh viễn (hết hạn cookie, hết quota, 401, 407)."""
        if isinstance(exc, MuseAuthError):
            return True
        text = str(exc).lower()
        return any(part in text for part in (
            "401", "407", "hết hạn mức", "额度不足", "phiên đăng nhập", "会话已被",
            "proxy", "hết lượt", "bị muse.ai đăng xuất", "tài khoản không còn cookie",
        ))

    def _is_fatal_input(self, exc: Exception) -> bool:
        """Lỗi do dữ liệu đầu vào hoặc file tham chiếu không hợp lệ, không thể khắc phục bằng cách thử lại."""
        text = str(exc).lower()
        return any(part in text for part in (
            "đã hủy", "chưa thêm ảnh", "không thấy ảnh", "tối đa",
            "không hợp lệ", "không đúng định dạng", "bị lỗi mã hóa",
            "không đọc được thời lượng", "chưa có nhân vật",
        ))

    def _claim(self, account_id: str):
        """Lấy một card công việc để thực thi. Tự động tái sử dụng (recycle) các card chưa thành công."""
        with self._gate:
            while not self.stop_event.is_set():
                if all(item["done"] for item in self._pending):
                    return None
                if account_id in self._dead:
                    return None

                alive = [aid for aid in self._account_pool if aid not in self._dead]
                if not alive:
                    for item in self._pending:
                        if not item["done"]:
                            item["done"] = True
                            self.job_changed.emit(
                                item["index"], "Lỗi",
                                "Tất cả tài khoản đều đã hết hạn mức hoặc cookie không hợp lệ.")
                    return None

                # 1. Tìm card chưa ai nhận mà tài khoản này chưa thử
                for item in self._pending:
                    if item["done"] or item["owner"] is not None:
                        continue
                    if account_id not in item["tried"]:
                        item["owner"] = account_id
                        self._busy += 1
                        return item

                # 2. Tìm card đã được thử bởi tất cả tài khoản còn sống -> tái chế để tạo lại vòng mới
                now = time.monotonic()
                can_recycle = False
                for item in self._pending:
                    if item["done"] or item["owner"] is not None:
                        continue
                    if all(aid in item["tried"] for aid in alive):
                        if now - item.get("last_tried_ts", 0) >= 2.0:
                            item["tried"].clear()
                            item["attempts_on_acc"] = 0
                            item["owner"] = account_id
                            self._busy += 1
                            total = item.get("total_attempts", 0)
                            self._log(f"Card {item['index'] + 1}: Bắt đầu vòng tạo lại mới (lượt {total + 1}).")
                            self.job_changed.emit(
                                item["index"], "Đang tạo lại",
                                f"Thử lại vòng mới (lượt {total + 1})...")
                            return item
                        else:
                            can_recycle = True

                # Nếu các luồng khác đang bận hoặc đang đợi cooldown tái chế
                if self._busy > 0 or can_recycle:
                    self._gate.wait(timeout=0.5)
                    continue

                return None

    def _handoff_dead_account(self, account_id: str, item: dict, message: str):
        label = self._account_label(account_id)
        with self._gate:
            item["tried"].add(account_id)
            item["owner"] = None
            item["attempts_on_acc"] = 0
            self._dead.add(account_id)
            alive = [aid for aid in self._account_pool if aid not in self._dead]

        if alive:
            self.job_changed.emit(
                item["index"], "Đang chuyển",
                f"{label} hết hạn/lỗi. Đưa card sang tài khoản khác.")
            self._log(f"Card {item['index'] + 1}: {label} dừng hoạt động. Chuyển sang tài khoản khác. {message}")
        else:
            with self._gate:
                item["done"] = True
            self.job_changed.emit(
                item["index"], "Lỗi",
                "Tất cả tài khoản đều đã hết hạn hoặc hết hạn mức.")
            self._log(f"Card {item['index'] + 1}: Không còn tài khoản khả dụng. {message}")

    def _serve_account(self, account_id: str, videos: dict[int, str], video_lock: threading.Lock) -> bool:
        """Chạy xử lý card cho một tài khoản. Trả về True nếu tài khoản này bị hỏng (chết hẳn)."""
        eng = MuseEngine(CFG)
        self._tls.engine = eng
        self._tls.account_id = account_id
        self._tls.memory = RefMemory(
            os.path.join(CFG.data_dir, f"ref_lane_{account_id[:8]}.json"), account_id)
        label = self._account_label(account_id)
        self._log(f"Tài khoản {label} bắt đầu nhận card.")
        try:
            eng.start()
            while not self.stop_event.is_set():
                item = self._claim(account_id)
                if item is None:
                    return False
                card_idx = item["index"]
                job = item["job"]
                try:
                    path = self._one(card_idx, job)
                    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                        with video_lock:
                            videos[card_idx] = path
                        with self._gate:
                            item["done"] = True
                            item["owner"] = None
                        self.job_changed.emit(card_idx, "Xong", path)
                    else:
                        raise MuseGenerationError("Không tạo được file media hợp lệ.")
                except Exception as exc:  # noqa: BLE001
                    if self.stop_event.is_set():
                        with self._gate:
                            item["done"] = True
                            item["owner"] = None
                        self.job_changed.emit(card_idx, "Đã dừng", "")
                        continue

                    message = _vi_error(str(exc))

                    # 1. Kiểm tra tài khoản chết hẳn (401, quota, proxy lỗi)
                    if self._is_dead_account(exc):
                        if isinstance(exc, MuseAuthError):
                            try:
                                store.mark(account_id, False, message)
                            except Exception:
                                pass
                        self._handoff_dead_account(account_id, item, message)
                        return True

                    # 2. Kiểm tra lỗi đầu vào không thể khắc phục
                    if self._is_fatal_input(exc):
                        with self._gate:
                            item["done"] = True
                            item["owner"] = None
                        self.job_changed.emit(card_idx, "Lỗi", message)
                        self._log(f"Lỗi card {card_idx + 1}: {message}")
                        continue

                    # 3. Lỗi tạm thời (timeout, prompt chưa xác nhận, refusal, mạng, v.v.)
                    try:
                        eng.close_current_page()
                    except Exception:
                        pass
                    try:
                        self._mem().reset()
                    except Exception:
                        pass

                    with self._gate:
                        item["attempts_on_acc"] = item.get("attempts_on_acc", 0) + 1
                        item["total_attempts"] = item.get("total_attempts", 0) + 1
                        attempts = item["attempts_on_acc"]
                        total_att = item["total_attempts"]

                    # Thử lại tối đa 2 lần liên tiếp trên tài khoản này
                    if attempts < 2:
                        with self._gate:
                            item["owner"] = None
                        self.job_changed.emit(
                            card_idx, "Đang tạo lại",
                            f"Lần {total_att} ({label}). {message}")
                        self._log(f"Card {card_idx + 1} ({label}) tạo lại lần {total_att}. {message}")
                        time.sleep(1.5)
                        continue

                    # Đã thử 2 lần trên tài khoản này: nhường card cho tài khoản khác
                    with self._gate:
                        item["attempts_on_acc"] = 0
                        item["owner"] = None
                        item["tried"].add(account_id)
                        item["last_tried_ts"] = time.monotonic()
                        alive = [aid for aid in self._account_pool if aid not in self._dead]
                        other_alive = [aid for aid in alive if aid not in item["tried"]]

                    if other_alive:
                        self.job_changed.emit(
                            card_idx, "Đang chuyển",
                            f"Đổi sang tài khoản khác sau {total_att} lần thử.")
                        self._log(f"Card {card_idx + 1}: Chuyển tài khoản sau {total_att} lần thử. {message}")
                    else:
                        self.job_changed.emit(
                            card_idx, "Đang tạo lại",
                            f"Chuẩn bị thử lại vòng mới (lượt {total_att}).")
                        self._log(f"Card {card_idx + 1}: Đã thử hết tài khoản, sẽ tạo lại vòng mới.")
                    time.sleep(1.0)
                    continue
                finally:
                    with self._gate:
                        self._busy -= 1
                        self._gate.notify_all()
            return False
        finally:
            try:
                eng.close_current_page()
            except Exception:
                pass

    def _lane(self, lane: int, account_id: str, videos: dict[int, str], video_lock: threading.Lock):
        current = account_id
        while current and not self.stop_event.is_set():
            failed = self._serve_account(current, videos, video_lock)
            if not failed:
                return
            with self._gate:
                current = self._spare.pop(0) if self._spare else ""

    def run(self):
        videos: dict[int, str] = {}
        merged = ""
        owner = MuseEngine(CFG)
        try:
            owner.start()
            aids = [item for item in self.account_ids if item] or [self.account_id]
            lane_count = min(len(aids), max(1, len(self.jobs)))
            if self.plan_threads:
                lane_count = min(lane_count, self.plan_threads)
            self._pending = [{
                "index": job.get("index", index),
                "job": job,
                "tried": set(),
                "owner": None,
                "done": False,
                "attempts_on_acc": 0,
                "total_attempts": 0,
                "last_tried_ts": 0.0,
            } for index, job in enumerate(self.jobs)]
            self._dead: set[str] = set()
            self._busy = 0
            self._gate = threading.Condition()
            self._account_pool = list(aids)
            self._spare = aids[lane_count:]
            self._log(f"Chạy {lane_count} luồng. Tự động thử lại và đổi tài khoản cho đến khi tạo thành công.")
            video_lock = threading.Lock()
            threads = []
            for lane in range(lane_count):
                thread = threading.Thread(
                    target=self._lane, args=(lane, aids[lane], videos, video_lock), daemon=True)
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
            for item in self._pending:
                if item["done"]:
                    continue
                if self.stop_event.is_set():
                    self.job_changed.emit(item["index"], "Đã dừng", "")
                elif len(self._dead) >= len(self._account_pool):
                    self.job_changed.emit(item["index"], "Lỗi", "Tất cả tài khoản đều đã hết hạn mức hoặc cookie.")
                else:
                    self.job_changed.emit(item["index"], "Lỗi", "Không thể tạo sau nhiều lần thử lại.")
            ordered = [videos[index] for index in sorted(videos)]
            if self.merge and len(ordered) >= 2 and not self.stop_event.is_set():
                if not shutil.which("ffmpeg"):
                    self._log("Không thấy ffmpeg, bỏ qua bước gộp.")
                else:
                    self._log(f"Đang gộp {len(ordered)} video theo thứ tự prompt…")
                    merged = os.path.join(
                        self.out_dir, time.strftime("merged_%Y%m%d_%H%M%S.mp4"))
                    merge_videos(ordered, merged)
                    self._log(f"Đã gộp: {merged}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Dừng hàng đợi: {exc}")
            self.job_changed.emit(-1, "Lỗi", _vi_error(str(exc)))
        finally:
            try:
                owner.stop()
            except Exception:
                pass
            self.batch_finished.emit(merged)


class CookieDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nhập cookie Muse")
        self.resize(560, 280)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Tên gợi nhớ, ví dụ acc-01")
        self.cookie_edit = QPlainTextEdit()
        self.cookie_edit.setPlaceholderText("hatch_sess=...  hoặc cả chuỗi cookie")
        form.addRow("Tên", self.label_edit)
        form.addRow("Cookie", self.cookie_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, dict]:
        return self.label_edit.text().strip(), parse_cookie_text(self.cookie_edit.toPlainText())


class CircleProgress(QWidget):
    """Vòng tròn: chờ thì viền xám, đang chạy fill xanh, lỗi fill đỏ."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(22, 22)
        self.mode = "idle"
        self.value = 0
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    def set_mode(self, mode: str, value: int = 0):
        self.mode = mode
        self.value = max(0, min(100, int(value)))
        if mode == "run":
            if not self.timer.isActive():
                self.timer.start(40)
        else:
            self.timer.stop()
        self.update()

    def _tick(self):
        self.angle = (self.angle + 14) % 360
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(2.5, 2.5, 17, 17)
        painter.setPen(QPen(QColor("#cbd5e1"), 2.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)
        if self.mode == "idle":
            return
        color = QColor("#dc2626") if self.mode == "bad" else QColor("#16a34a")
        if self.mode == "run":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            span = max(self.value, 8) * 3.6
            painter.drawPie(rect, int((90 - self.angle) * 16), int(-span * 16))
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(rect)


class ResultCard(QFrame):
    """Một thẻ kết quả: prompt, ảnh nhân vật, video và trạng thái."""

    images_changed = Signal(list)
    play_started = Signal(int)

    def __init__(self, index: int, prompt: str, aspect: str, duration: int, characters: list[dict],
                 images: list[str] | None = None, max_images: int = 10):
        super().__init__()
        self.setObjectName("resultCard")
        self.setFixedWidth(220)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Maximum)
        self.job_index = index - 1
        self.index = index
        self.kind = "video"
        self.path = ""
        self.status = "Chờ"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        head = QHBoxLayout()
        self.title = QLabel(f"VIDEO #{index}")
        self.title.setObjectName("cardTitle")
        self.circle = CircleProgress()
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.circle)
        layout.addLayout(head)

        prompt_box = QPlainTextEdit(prompt.strip())
        prompt_box.setReadOnly(True)
        prompt_box.setFixedHeight(64)
        prompt_box.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.prompt_box = prompt_box
        layout.addWidget(prompt_box)

        self.max_images = max(0, min(10, max_images))
        self.images: list[str] = []
        self._thumb_size = 72
        self._cast = []
        names = {tag.lower() for tag in tags_in(prompt)}
        for item in characters:
            name = (item.get("name") or "").strip()
            image = item.get("image") or ""
            if name.lower() not in names or not os.path.isfile(image):
                continue
            self._cast.append((name, image))
            if len(self._cast) >= 10:
                break

        self.image_btn = QPushButton("Thêm ảnh")
        self.image_btn.setObjectName("small")
        self.image_btn.clicked.connect(self.pick_source_image)
        self.image_count = QLabel("0/10")
        self.image_count.setObjectName("muted")
        self.image_strip = self._make_strip()
        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        add_row.addWidget(self.image_btn)
        add_row.addWidget(self.image_strip, 1)
        add_row.addWidget(self.image_count)
        layout.addLayout(add_row)
        self.set_images(list(images or []))

        self.placeholder = QLabel("Chưa có video")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setObjectName("stage")
        self.placeholder.setFixedHeight(96)
        self.placeholder.installEventFilter(self)
        layout.addWidget(self.placeholder)

        self.ready = QLabel("")
        self.ready.setWordWrap(True)
        self.ready.setObjectName("muted")
        layout.addWidget(self.ready)

    def set_kind(self, mode_key: str):
        self.kind = "image" if mode_key == "t2i" else "video"
        self.title.setText(f"{'ẢNH' if self.kind == 'image' else 'VIDEO'} #{self.index}")
        if not self.path:
            self.placeholder.setPixmap(QPixmap())
            self.placeholder.setText("Chưa có ảnh" if self.kind == "image" else "Chưa có video")

    def set_images(self, paths: list[str]):
        kept = []
        for path in paths:
            if path and os.path.isfile(path) and path not in kept:
                kept.append(path)
            if len(kept) >= self.max_images:
                break
        self.images = kept
        entries = [(path, "Bấm để bỏ " + os.path.basename(path), path) for path in self.images]
        entries.extend((path, "@" + name, "") for name, path in self._cast)
        self._fill_strip(self.image_strip, entries)
        total = len(self.images) + len(self._cast)
        self.image_count.setText(f"{total}/10")
        self.image_btn.setEnabled(total < 10)

    def fit_width(self, width: int):
        self.setFixedWidth(width)

    def _make_strip(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setFixedHeight(58)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def _fill_strip(self, scroll: QScrollArea, entries: list[tuple[str, str, str]]):
        scroll.setVisible(bool(entries))
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        size = 52
        for path, tip, remove_path in entries:
            thumb = QPushButton()
            thumb.setObjectName("thumb")
            thumb.setIcon(QIcon(QPixmap(path).scaled(
                size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)))
            thumb.setIconSize(QSize(size - 4, size - 4))
            thumb.setFixedSize(size, size)
            thumb.setToolTip(tip)
            if remove_path:
                thumb.clicked.connect(lambda _checked=False, item=remove_path: self._remove_image(item))
            row.addWidget(thumb)
        row.addStretch(1)
        scroll.setWidget(host)

    def _remove_image(self, path: str):
        self.set_images([item for item in self.images if item != path])
        self.images_changed.emit(list(self.images))

    def pick_source_image(self):
        room = 10 - len(self.images) - len(self._cast)
        if room <= 0:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Thêm ảnh cho card này", "", "Ảnh (*.png *.jpg *.jpeg *.webp)")
        if not paths:
            return
        merged = list(self.images)
        for path in paths:
            if path not in merged and len(merged) + len(self._cast) < 10:
                merged.append(path)
        self.set_images(merged)
        self.images_changed.emit(list(self.images))

    def set_progress(self, value: int):
        if self.status not in ("Xong", "Lỗi", "Đã dừng"):
            self.status = "Đang chạy"
            self.circle.set_mode("run", value)

    def set_state(self, status: str, detail: str = ""):
        self.status = status
        if status == "Xong":
            self.circle.set_mode("ok", 100)
        elif status == "Lỗi":
            self.circle.set_mode("bad", 100)
        elif status in ("Chờ", "Đã dừng"):
            self.circle.set_mode("idle", 0)
        else:
            self.circle.set_mode("run", max(self.circle.value, 8))
        if detail and os.path.isfile(detail):
            self.path = detail
            ext = os.path.splitext(detail)[1].lower()
            if ext in {".mp4", ".webm", ".mov"}:
                self.placeholder.setText("Bấm để xem")
                self.placeholder.setCursor(Qt.CursorShape.PointingHandCursor)
                self.ready.setText("Đã lưu · " + os.path.basename(detail))
                self.ready.setObjectName("ok")
            else:
                pix = QPixmap(detail)
                if not pix.isNull():
                    self.placeholder.setPixmap(pix.scaled(
                        284, 132, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    self.placeholder.setText("")
                self.ready.setText("Đã lưu")
                self.ready.setObjectName("ok")
        elif status in ("Đang chuyển", "Đang tạo lại") and detail:
            self.ready.setText(detail)
            self.ready.setObjectName("running")
        elif status == "Lỗi" and detail:
            self.ready.setText(detail)
            self.ready.setObjectName("bad")
        self.ready.style().unpolish(self.ready)
        self.ready.style().polish(self.ready)

    def eventFilter(self, obj, event):
        if obj is self.placeholder and event.type() == QEvent.Type.MouseButtonPress and self.path:
            self.open_player()
            return True
        return super().eventFilter(obj, event)

    def open_player(self):
        if not self.path or not os.path.isfile(self.path):
            return
        dialog = QDialog(self.window())
        dialog.setWindowTitle(os.path.basename(self.path))
        dialog.resize(960, 540)
        video = QVideoWidget()
        player = QMediaPlayer(dialog)
        player.setAudioOutput(QAudioOutput(dialog))
        player.setVideoOutput(video)
        player.setSource(QUrl.fromLocalFile(self.path))
        box = QVBoxLayout(dialog)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(video)
        player.play()
        dialog.finished.connect(player.stop)
        dialog.exec()

    def shutdown(self):
        return None


class CheckUpdateThread(QThread):
    update_found = Signal(dict)
    no_update = Signal()
    check_failed = Signal(str)

    def __init__(self, repo: str, current_ver: str):
        super().__init__()
        self.repo = repo
        self.current_ver = current_ver

    def run(self):
        try:
            info = check_for_updates(self.repo, self.current_ver)
            if info and info.get("has_update"):
                self.update_found.emit(info)
            else:
                self.no_update.emit()
        except Exception as exc:
            self.check_failed.emit(str(exc))


class DownloadUpdateThread(QThread):
    progress = Signal(int, int, int)  # downloaded, total, percent
    download_finished = Signal(str)
    download_failed = Signal(str)

    def __init__(self, url: str, dest_path: str):
        super().__init__()
        self.url = url
        self.dest_path = dest_path
        self.cancel_event = threading.Event()

    def run(self):
        try:
            def on_progress(down, total, pct):
                self.progress.emit(down, total, pct)

            ok = download_file(self.url, self.dest_path, on_progress, self.cancel_event)
            if ok:
                self.download_finished.emit(self.dest_path)
            else:
                self.download_failed.emit("Tải bản cập nhật đã bị huỷ.")
        except Exception as exc:
            self.download_failed.emit(str(exc))

    def cancel(self):
        self.cancel_event.set()


class UpdateDialog(QDialog):
    """Hộp thoại thông báo có bản cập nhật mới, tải về và tự động khởi động lại."""

    def __init__(self, update_info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cập nhật phiên bản mới")
        self.resize(520, 380)
        self.update_info = update_info
        self.download_thread: DownloadUpdateThread | None = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        head = QHBoxLayout()
        title = QLabel("CẬP NHẬT PHIÊN BẢN MỚI")
        title.setObjectName("cardTitle")
        title.setStyleSheet("font-size: 14px; font-weight: 800; color: #0f172a;")
        head.addWidget(title)
        head.addStretch(1)

        badge = QLabel(f"v{self.update_info.get('latest_version')}")
        badge.setObjectName("chip")
        badge.setStyleSheet("background: #0f172a; color: white; border-radius: 6px; padding: 3px 8px; font-weight: 700;")
        head.addWidget(badge)
        layout.addLayout(head)

        curr = self.update_info.get("current_version", "")
        latest = self.update_info.get("latest_version", "")
        ver_info = QLabel(f"Phiên bản hiện tại: <b>v{curr}</b>  ➔  Phiên bản mới nhất: <b style='color:#16a34a'>v{latest}</b>")
        layout.addWidget(ver_info)

        notes_label = QLabel("Nội dung thay đổi:")
        notes_label.setObjectName("muted")
        layout.addWidget(notes_label)

        notes_box = QPlainTextEdit()
        notes_box.setReadOnly(True)
        notes_box.setPlainText(self.update_info.get("body") or "Bản cập nhật mới với nhiều cải tiến.")
        notes_box.setFixedHeight(120)
        layout.addWidget(notes_box)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(18)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Sẵn sàng tải xuống.")
        self.status_label.setObjectName("muted")
        layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        self.close_btn = QPushButton("Để sau")
        self.close_btn.setObjectName("small")
        self.close_btn.clicked.connect(self.reject)

        self.update_btn = QPushButton("Cập nhật ngay")
        self.update_btn.setObjectName("primary")
        self.update_btn.setStyleSheet("background: #0f172a; color: white; padding: 8px 16px; border-radius: 8px; font-weight: 700;")
        self.update_btn.clicked.connect(self.start_download)

        btn_row.addStretch(1)
        btn_row.addWidget(self.close_btn)
        btn_row.addWidget(self.update_btn)
        layout.addLayout(btn_row)

    def start_download(self):
        url = self.update_info.get("download_url")
        if not url:
            QMessageBox.warning(self, "Lỗi", "Không tìm thấy liên kết tải bản cập nhật.")
            return

        self.update_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.status_label.setText("Đang kết nối máy chủ...")

        asset_name = self.update_info.get("asset_name") or "update.zip"
        dest_path = os.path.join(tempfile.gettempdir(), f"muse_update_{int(time.time())}_{asset_name}")

        self.download_thread = DownloadUpdateThread(url, dest_path)
        self.download_thread.progress.connect(self._on_progress)
        self.download_thread.download_finished.connect(self._on_finished)
        self.download_thread.download_failed.connect(self._on_failed)
        self.download_thread.start()

    def _on_progress(self, downloaded: int, total: int, percent: int):
        self.progress_bar.setValue(percent)
        down_mb = downloaded / (1024 * 1024)
        total_mb = total / (1024 * 1024) if total > 0 else 0
        if total > 0:
            self.status_label.setText(f"Đang tải: {percent}% ({down_mb:.1f} MB / {total_mb:.1f} MB)")
        else:
            self.status_label.setText(f"Đang tải: {down_mb:.1f} MB")

    def _on_finished(self, dest_path: str):
        self.status_label.setText("Đã tải xong! Đang cài đặt và khởi động lại...")
        self.status_label.setStyleSheet("color: #16a34a; font-weight: 700;")
        QTimer.singleShot(800, lambda: self._apply_and_exit(dest_path))

    def _apply_and_exit(self, dest_path: str):
        try:
            apply_update_and_restart(dest_path)
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi cập nhật", f"Không thể tự động cập nhật: {exc}")
            self.update_btn.setEnabled(True)
            self.close_btn.setEnabled(True)

    def _on_failed(self, error_msg: str):
        self.status_label.setText(f"Lỗi: {error_msg}")
        self.status_label.setStyleSheet("color: #dc2626; font-weight: 700;")
        self.update_btn.setEnabled(True)
        self.close_btn.setEnabled(True)
        QMessageBox.warning(self, "Cập nhật thất bại", f"Không thể tải bản cập nhật: {error_msg}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MUSE DESKTOP")
        self.resize(1280, 820)
        self.settings = QSettings("muse2api", "desktop")
        self.stop_event = threading.Event()
        self.worker: BatchWorker | None = None
        self.check_thread: CheckUpdateThread | None = None
        self.mode_key = "pipeline"
        self._build()
        self.session = None
        self._restore()
        self.reload_accounts()
        QTimer.singleShot(3000, lambda: self.check_update(silent=True))

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("cardTitle")
        layout.addWidget(label)
        return card, layout

    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        tab_bar = QFrame()
        tab_bar.setObjectName("tabBar")
        tab_row = QHBoxLayout(tab_bar)
        tab_row.setContentsMargins(16, 6, 16, 6)
        self.tab_video = QPushButton("Tạo Video")
        self.tab_chars = QPushButton("Nhân vật")
        self.tab_accounts = QPushButton("Tài khoản")
        for button in (self.tab_video, self.tab_chars, self.tab_accounts):
            button.setObjectName("tab")
            button.setCheckable(True)
            button.setAutoExclusive(True)
        self.tab_video.setChecked(True)
        self.tab_video.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        self.tab_chars.clicked.connect(lambda: self.pages.setCurrentIndex(1))
        self.tab_accounts.clicked.connect(lambda: self.pages.setCurrentIndex(2))
        tab_row.addWidget(self.tab_video)
        tab_row.addWidget(self.tab_chars)
        tab_row.addWidget(self.tab_accounts)
        tab_row.addStretch(1)
        self._account_ids: list[str] = []
        add_btn = QPushButton("+ Thêm tài khoản")
        add_btn.setObjectName("small")
        add_btn.clicked.connect(self.import_cookie)
        tab_row.addWidget(add_btn)
        self.session_label = QLabel("Chưa đăng nhập")
        self.session_label.setObjectName("muted")
        self.login_btn = QPushButton("Đăng nhập")
        self.login_btn.setObjectName("small")
        self.login_btn.clicked.connect(self.toggle_login)
        tab_row.addWidget(self.session_label)
        tab_row.addWidget(self.login_btn)
        self.check_update_btn = QPushButton("Kiểm tra cập nhật")
        self.check_update_btn.setObjectName("small")
        self.check_update_btn.clicked.connect(lambda: self.check_update(silent=False))
        tab_row.addWidget(self.check_update_btn)
        outer.addWidget(tab_bar)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._studio_page())
        self.pages.addWidget(self._characters_page())
        self.pages.addWidget(self._accounts_page())
        outer.addWidget(self.pages, 1)
        self.reload_characters()

        footer = QFrame()
        footer.setObjectName("footer")
        foot_row = QHBoxLayout(footer)
        foot_row.setContentsMargins(16, 6, 16, 6)
        self.status_label = QLabel("Sẵn sàng.")
        self.status_label.setObjectName("status")
        engine_label = QLabel("Muse · một Chrome · tự lưu và gộp")
        engine_label.setObjectName("engine")
        foot_row.addWidget(self.status_label, 1)
        foot_row.addWidget(engine_label)
        outer.addWidget(footer)
        self._sync_mode()

    def _studio_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("workspace")
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        toolbar = QFrame()
        toolbar.setObjectName("subbar")
        tool_row = QHBoxLayout(toolbar)
        tool_row.setContentsMargins(16, 8, 16, 8)
        caption = QLabel("Chế độ:")
        caption.setObjectName("muted")
        tool_row.addWidget(caption)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons = {}
        for key, label in MODES:
            button = QPushButton(label)
            button.setObjectName("sub")
            button.setCheckable(True)
            self.mode_group.addButton(button)
            self.mode_buttons[key] = button
            tool_row.addWidget(button)
        self.mode_buttons["pipeline"].setChecked(True)
        self.mode_group.buttonClicked.connect(self._on_mode)
        tool_row.addStretch(1)
        hint = QLabel("Động cơ: Muse")
        hint.setObjectName("muted")
        tool_row.addWidget(hint)
        column.addWidget(toolbar)

        body = QHBoxLayout()
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(14)
        body.addWidget(self._left_panel())
        body.addWidget(self._right_panel(), 1)
        column.addLayout(body, 1)
        return page

    def _left_panel(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setFixedWidth(340)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(12)

        config, config_layout = self._card("CẤU HÌNH")
        row = QHBoxLayout()
        aspect_box = QVBoxLayout()
        aspect_label = QLabel("Tỉ lệ khung hình")
        aspect_label.setObjectName("field")
        self.aspect = QComboBox()
        self.aspect.addItem("9:16 Dọc", "9:16")
        self.aspect.addItem("16:9 Ngang", "16:9")
        aspect_box.addWidget(aspect_label)
        aspect_box.addWidget(self.aspect)
        duration_box = QVBoxLayout()
        duration_label = QLabel("Độ dài video")
        duration_label.setObjectName("field")
        self.duration = QComboBox()
        self.duration.addItem("15 giây", 15)
        self.duration.addItem("30 giây", 30)
        duration_box.addWidget(duration_label)
        duration_box.addWidget(self.duration)
        row.addLayout(aspect_box, 1)
        row.addLayout(duration_box, 1)
        config_layout.addLayout(row)
        out_label = QLabel("Thư mục lưu kết quả")
        out_label.setObjectName("field")
        config_layout.addWidget(out_label)
        out_row = QHBoxLayout()
        self.out_dir = QLineEdit()
        browse_out = QPushButton("Chọn")
        browse_out.setObjectName("small")
        browse_out.clicked.connect(self.pick_output)
        out_row.addWidget(self.out_dir, 1)
        out_row.addWidget(browse_out)
        config_layout.addLayout(out_row)
        self.merge_box = QCheckBox("Tự gộp các video trong đợt này thành 1 file")
        self.merge_box.setChecked(True)
        config_layout.addWidget(self.merge_box)
        layout.addWidget(config)

        self._lines: list[str] = []
        prompt_card, prompt_layout = self._card("NGUỒN PROMPT (.TXT)")
        hint = QLabel("Mỗi dòng trong file .txt là một prompt. Viết @tên để gắn ảnh nhân vật, tối đa 10 ảnh.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        prompt_layout.addWidget(hint)
        file_label = QLabel("File .txt")
        file_label.setObjectName("field")
        prompt_layout.addWidget(file_label)
        file_row = QHBoxLayout()
        self.prompt_file = QLineEdit()
        self.prompt_file.setReadOnly(True)
        self.prompt_file.setPlaceholderText("Chưa chọn file .txt")
        pick_file = QPushButton("Chọn file")
        pick_file.setObjectName("small")
        pick_file.clicked.connect(self.pick_prompt_file)
        file_row.addWidget(self.prompt_file, 1)
        file_row.addWidget(pick_file)
        prompt_layout.addLayout(file_row)
        folder_label = QLabel("Thư mục chứa file .txt")
        folder_label.setObjectName("field")
        prompt_layout.addWidget(folder_label)
        folder_row = QHBoxLayout()
        self.prompt_folder = QLineEdit()
        self.prompt_folder.setReadOnly(True)
        self.prompt_folder.setPlaceholderText("Chưa chọn thư mục")
        pick_folder = QPushButton("Chọn thư mục")
        pick_folder.setObjectName("small")
        pick_folder.clicked.connect(self.pick_prompt_folder)
        folder_row.addWidget(self.prompt_folder, 1)
        folder_row.addWidget(pick_folder)
        prompt_layout.addLayout(folder_row)
        head = QHBoxLayout()
        self.count_label = QLabel("0 prompt")
        self.count_label.setObjectName("muted")
        clear_files = QPushButton("Bỏ chọn")
        clear_files.setObjectName("small")
        clear_files.clicked.connect(self.clear_prompt_files)
        head.addWidget(self.count_label)
        head.addStretch(1)
        head.addWidget(clear_files)
        prompt_layout.addLayout(head)
        self.file_table = QTableWidget(0, 3)
        self.file_table.setHorizontalHeaderLabels(["#", "File", "Prompt"])
        self.file_table.horizontalHeader().setStretchLastSection(False)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.file_table.setShowGrid(False)
        self.file_table.setMaximumHeight(140)
        prompt_layout.addWidget(self.file_table)
        layout.addWidget(prompt_card)

        self.image_card, image_layout = self._card("ẢNH NGUỒN")
        note = QLabel("Thêm ảnh vào mọi card. Mỗi card tối đa 10 ảnh, tính cả @nhân vật. Bấm ảnh nhỏ trên card để bỏ.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        image_layout.addWidget(note)
        image_row = QHBoxLayout()
        self.image_path = QLineEdit()
        self.image_path.setReadOnly(True)
        self.image_path.setPlaceholderText("Chưa thêm ảnh...")
        browse_image = QPushButton("Thêm ảnh")
        browse_image.setObjectName("small")
        browse_image.clicked.connect(self.pick_image)
        image_row.addWidget(self.image_path, 1)
        image_row.addWidget(browse_image)
        image_layout.addLayout(image_row)
        layout.addWidget(self.image_card)

        control, control_layout = self._card("ĐIỀU KHIỂN TIẾN TRÌNH")
        self.start_btn = QPushButton("Bắt đầu tạo")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start_batch)
        self.retry_btn = QPushButton("Tạo lại card lỗi")
        self.retry_btn.setObjectName("small")
        self.retry_btn.setVisible(False)
        self.retry_btn.clicked.connect(self.retry_failed_cards)
        self.stop_btn = QPushButton("Dừng")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_batch)
        control_layout.addWidget(self.start_btn)
        control_layout.addWidget(self.retry_btn)
        control_layout.addWidget(self.stop_btn)
        layout.addWidget(control)
        layout.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _right_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("card")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        head = QFrame()
        head_row = QHBoxLayout(head)
        head_row.setContentsMargins(14, 10, 14, 10)
        title = QLabel("KẾT QUẢ KẾT XUẤT")
        title.setObjectName("cardTitle")
        head_row.addWidget(title)
        head_row.addStretch(1)
        layout.addWidget(head)

        stats = QFrame()
        stats.setObjectName("stats")
        stats_row = QHBoxLayout(stats)
        stats_row.setContentsMargins(14, 8, 14, 8)
        stats_row.setSpacing(28)
        self.stat_total = QLabel("Tổng: 0")
        self.stat_total.setObjectName("stat")
        self.stat_running = QLabel("Đang chạy: 0")
        self.stat_running.setObjectName("running")
        self.stat_ok = QLabel("Thành công: 0")
        self.stat_ok.setObjectName("ok")
        self.stat_rate = QLabel("Tạo được: 0%")
        self.stat_rate.setObjectName("ok")
        self.stat_error = QLabel("Lỗi: 0")
        self.stat_error.setObjectName("bad")
        for label in (self.stat_total, self.stat_running, self.stat_ok, self.stat_rate, self.stat_error):
            label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            stats_row.addWidget(label)
        stats_row.addStretch(1)
        layout.addWidget(stats)

        self.shared_images: list[str] = []
        self.queue: list[dict] = []
        self.page = 0
        self.cards: list[ResultCard] = []
        self.card_host = QWidget()
        self.card_grid = QGridLayout(self.card_host)
        self.card_grid.setContentsMargins(12, 12, 12, 12)
        self.card_grid.setSpacing(8)
        self.card_grid.setAlignment(Qt.AlignTop)
        self._card_cols = 0
        self.card_scroll = QScrollArea()
        self.card_scroll.setWidgetResizable(True)
        self.card_scroll.setFrameShape(QFrame.NoFrame)
        self.card_scroll.setWidget(self.card_host)
        self.card_scroll.viewport().installEventFilter(self)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._place_cards)
        layout.addWidget(self.card_scroll, 1)

        bottom = QFrame()
        bottom.setObjectName("stats")
        bottom_row = QHBoxLayout(bottom)
        bottom_row.setContentsMargins(12, 8, 12, 8)
        self.prev_btn = QPushButton("Trước")
        self.prev_btn.setObjectName("small")
        self.prev_btn.clicked.connect(self._prev_page)
        self.page_label = QLabel("Trang 0/0")
        self.page_label.setObjectName("muted")
        self.next_btn = QPushButton("Sau")
        self.next_btn.setObjectName("small")
        self.next_btn.clicked.connect(self._next_page)
        bottom_row.addWidget(self.prev_btn)
        bottom_row.addWidget(self.page_label)
        bottom_row.addWidget(self.next_btn)
        bottom_row.addStretch(1)
        clear_btn = QPushButton("Dọn sạch kết quả")
        clear_btn.setObjectName("small")
        clear_btn.clicked.connect(self.clear_results)
        bottom_row.addWidget(clear_btn)
        layout.addWidget(bottom)
        return panel

    def _characters_path(self) -> str:
        return os.path.join(CFG.data_dir, "characters.json")

    def _characters_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("workspace")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        card, box = self._card("THƯ VIỆN NHÂN VẬT")
        note = QLabel(
            "Mỗi nhân vật có một tên và một ảnh. Trong file prompt viết @tên, ví dụ @linh. "
            "Một prompt dùng tối đa 10 ảnh. Ảnh đã đăng trên cùng tài khoản sẽ không upload lại.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        box.addWidget(note)
        row = QHBoxLayout()
        self.char_name = QLineEdit()
        self.char_name.setPlaceholderText("Tên, ví dụ linh")
        self.char_image = QLineEdit()
        self.char_image.setReadOnly(True)
        self.char_image.setPlaceholderText("Chưa chọn ảnh")
        pick = QPushButton("Chọn ảnh")
        pick.setObjectName("small")
        pick.clicked.connect(self.pick_character_image)
        add = QPushButton("Thêm")
        add.setObjectName("small")
        add.clicked.connect(self.add_character)
        row.addWidget(self.char_name, 1)
        row.addWidget(self.char_image, 2)
        row.addWidget(pick)
        row.addWidget(add)
        box.addLayout(row)
        self.char_table = QTableWidget(0, 2)
        self.char_table.setHorizontalHeaderLabels(["@tag", "Ảnh"])
        self.char_table.horizontalHeader().setStretchLastSection(True)
        self.char_table.verticalHeader().setVisible(False)
        self.char_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.char_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.char_table.setShowGrid(False)
        box.addWidget(self.char_table, 1)
        remove = QPushButton("Xóa nhân vật đã chọn")
        remove.setObjectName("small")
        remove.clicked.connect(self.delete_character)
        box.addWidget(remove)
        layout.addWidget(card, 1)
        return page

    def reload_characters(self):
        self._characters = load_characters(self._characters_path())
        self.char_table.setRowCount(len(self._characters))
        for index, item in enumerate(self._characters):
            self.char_table.setItem(index, 0, QTableWidgetItem("@" + item.get("name", "")))
            self.char_table.setItem(index, 1, QTableWidgetItem(item.get("image") or ""))
        self.char_table.resizeColumnsToContents()

    def pick_character_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Ảnh nhân vật", "", "Ảnh (*.png *.jpg *.jpeg *.webp)")
        if path:
            self.char_image.setText(path)

    def add_character(self):
        name = self.char_name.text().strip()
        image = self.char_image.text().strip()
        if not TAG_RE.fullmatch("@" + name):
            QMessageBox.warning(self, "Nhân vật", "Tên chỉ gồm chữ, số và dấu gạch dưới, không có khoảng trắng.")
            return
        if not image or not os.path.isfile(image):
            QMessageBox.warning(self, "Nhân vật", "Hãy chọn một ảnh có thật.")
            return
        items = load_characters(self._characters_path())
        if len(items) >= 10:
            QMessageBox.warning(self, "Nhân vật", "Tối đa 10 ảnh nhân vật.")
            return
        if any((item.get("name") or "").lower() == name.lower() for item in items):
            QMessageBox.warning(self, "Nhân vật", f"@{name} đã có trong thư viện.")
            return
        items.append({"name": name, "image": image})
        save_characters(self._characters_path(), items)
        self.char_name.clear()
        self.char_image.clear()
        self.reload_characters()
        self._log(f"Đã thêm nhân vật @{name}")

    def delete_character(self):
        rows = sorted({item.row() for item in self.char_table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        items = load_characters(self._characters_path())
        for row in rows:
            if 0 <= row < len(items):
                items.pop(row)
        save_characters(self._characters_path(), items)
        self.reload_characters()

    def _accounts_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("workspace")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        card, card_layout = self._card("DANH SÁCH TÀI KHOẢN MUSE")
        tools = QHBoxLayout()
        tools.addStretch(1)
        add_btn = QPushButton("+ Thêm cookie")
        add_btn.setObjectName("small")
        add_btn.clicked.connect(self.import_cookie)
        refresh_btn = QPushButton("Tải lại")
        refresh_btn.setObjectName("small")
        refresh_btn.clicked.connect(self.reload_accounts)
        tools.addWidget(refresh_btn)
        tools.addWidget(add_btn)
        card_layout.addLayout(tools)
        self.accounts_table = QTableWidget(0, 4)
        self.accounts_table.setHorizontalHeaderLabels(["Tên", "Cookie", "Trạng thái", "Ghi chú"])
        self.accounts_table.horizontalHeader().setStretchLastSection(True)
        self.accounts_table.verticalHeader().setVisible(False)
        self.accounts_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.accounts_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.accounts_table.setShowGrid(False)
        card_layout.addWidget(self.accounts_table, 1)
        layout.addWidget(card, 1)
        return page

    def _restore(self):
        self.out_dir.setText(self.settings.value(
            "out_dir", str(Path.home() / "Movies" / "MuseDesktop")))
        self.aspect.setCurrentIndex(int(self.settings.value("aspect", 0)))
        saved_duration = int(self.settings.value("duration", 15))
        duration_index = self.duration.findData(30 if saved_duration == 30 else 15)
        self.duration.setCurrentIndex(max(0, duration_index))
        mode_index = int(self.settings.value("mode", 0))
        if 0 <= mode_index < len(MODES):
            self.mode_buttons[MODES[mode_index][0]].setChecked(True)
            self.mode_key = MODES[mode_index][0]
        saved_image = self.settings.value("image", "") or ""
        if os.path.isfile(saved_image):
            self.shared_images = [saved_image]
            self.image_path.setText(os.path.basename(saved_image))
        self._sync_mode()

    def _save_settings(self):
        self.settings.setValue("out_dir", self.out_dir.text().strip())
        self.settings.setValue("aspect", self.aspect.currentIndex())
        self.settings.setValue("duration", int(self.duration.currentData()))
        self.settings.setValue("mode", [key for key, _label in MODES].index(self.mode_key))
        self.settings.setValue("image", self.image_path.text().strip())

    def _on_mode(self, button):
        for key, item in self.mode_buttons.items():
            if item is button:
                self.mode_key = key
                break
        self._sync_mode()

    def _sync_mode(self):
        self.image_card.setVisible(self.mode_key == "i2v")
        self.duration.setEnabled(self.mode_key != "t2i")
        self.merge_box.setEnabled(self.mode_key != "t2i")
        for card in getattr(self, "cards", []):
            card.set_kind(self.mode_key)

    def _prompt_lines(self) -> list[str]:
        return list(self._lines)

    def _read_prompt_file(self, path: Path) -> list[str]:
        text = path.read_text(encoding="utf-8", errors="replace")
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _set_prompt_files(self, paths: list[Path]) -> bool:
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "Prompt", "Đang chạy, hãy dừng trước khi nạp file khác.")
            return False
        lines: list[str] = []
        shown = paths[:30]
        self.file_table.setRowCount(len(shown))
        for index, path in enumerate(paths):
            file_lines = self._read_prompt_file(path)
            lines.extend(file_lines)
            if index < len(shown):
                self.file_table.setItem(index, 0, QTableWidgetItem(str(index + 1)))
                self.file_table.setItem(index, 1, QTableWidgetItem(path.name))
                self.file_table.setItem(index, 2, QTableWidgetItem(str(len(file_lines))))
        self._lines = lines
        extra = f" · hiện {len(shown)}/{len(paths)} file" if len(paths) > len(shown) else ""
        self.count_label.setText(f"{len(lines)} prompt{extra}")
        self._build_queue(lines)
        self._log(f"Đã tạo {len(lines)} card, mỗi trang {PAGE_SIZE}.")
        return True

    def pick_prompt_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Chọn file prompt", "", "Text (*.txt)")
        if not path:
            return
        if not self._set_prompt_files([Path(path)]):
            return
        self.prompt_file.setText(path)
        self.prompt_folder.clear()
        self._log(f"Đã nạp {Path(path).name}")

    def pick_prompt_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Thư mục prompt")
        if not folder:
            return
        paths = sorted(Path(folder).glob("*.txt"))
        if not self._set_prompt_files(paths):
            return
        self.prompt_folder.setText(folder)
        self.prompt_file.clear()
        if not paths:
            QMessageBox.warning(self, "Prompt", "Thư mục không có file .txt.")
            return
        self._log(f"Đã nạp {len(paths)} file .txt")

    def clear_prompt_files(self):
        self._lines = []
        self.prompt_file.clear()
        self.prompt_folder.clear()
        self.file_table.setRowCount(0)
        self.count_label.setText("0 prompt")
        self.queue = []
        self.page = 0
        self._render_page()

    def reload_accounts(self):
        self._account_ids = [
            item["id"] for item in store.list_accounts() if item.get("enabled", True)
        ]
        rows = store.list_accounts()
        self.accounts_table.setRowCount(len(rows))
        for index, item in enumerate(rows):
            state = "Sẵn sàng" if item.get("ok") is not False else "Lỗi"
            if not item.get("enabled", True):
                state = "Tắt"
            self.accounts_table.setItem(index, 0, QTableWidgetItem(item.get("label") or item["id"]))
            self.accounts_table.setItem(index, 1, QTableWidgetItem(str(item.get("cookie_count") or 0)))
            self.accounts_table.setItem(index, 2, QTableWidgetItem(state))
            self.accounts_table.setItem(index, 3, QTableWidgetItem(_note_vi(item.get("note") or "")))
        self.accounts_table.resizeColumnsToContents()

    def import_cookie(self):
        dialog = CookieDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        label, cookies = dialog.values()
        if "hatch_sess" not in cookies:
            QMessageBox.warning(self, "Cookie", "Cần có hatch_sess.")
            return
        account = store.add_account(cookies, label)
        self.reload_accounts()
        self._log(f"Đã thêm tài khoản {account['label']}")

    def _slots(self, prompt: str) -> int:
        return max(0, 10 - len(tags_in(prompt)))

    def pick_image(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Thêm ảnh cho mọi card", "", "Ảnh (*.png *.jpg *.jpeg *.webp *.gif)")
        if not paths:
            return
        for path in paths:
            if path not in self.shared_images and len(self.shared_images) < 10:
                self.shared_images.append(path)
        self.image_path.setText(", ".join(os.path.basename(path) for path in self.shared_images))
        self._add_shared_images(paths)

    def _add_shared_images(self, paths: list[str]):
        start = self.page * PAGE_SIZE
        for index, item in enumerate(self.queue):
            images = list(item.get("images") or [])
            limit = self._slots(item["prompt"])
            for path in paths:
                if path not in images and len(images) < limit:
                    images.append(path)
            item["images"] = images
            local = index - start
            if 0 <= local < len(self.cards):
                self.cards[local].set_images(images)

    def _set_card_images(self, row: int, paths: list[str]):
        if not (0 <= row < len(self.queue)):
            return
        self.queue[row]["images"] = list(paths)

    def pick_output(self):
        path = QFileDialog.getExistingDirectory(self, "Thư mục lưu", self.out_dir.text())
        if path:
            self.out_dir.setText(path)

    def clear_results(self):
        if self.worker and self.worker.isRunning():
            return
        for item in self.queue:
            item["status"] = "Chờ"
            item["detail"] = ""
        self._render_page()
        self._refresh_stats()

    def _page_count(self) -> int:
        if not self.queue:
            return 1
        return (len(self.queue) + PAGE_SIZE - 1) // PAGE_SIZE

    def _prev_page(self):
        if self.page <= 0:
            return
        self.page -= 1
        self._render_page()

    def _next_page(self):
        if self.page + 1 >= self._page_count():
            return
        self.page += 1
        self._render_page()

    def _build_queue(self, lines: list[str]):
        aspect = self.aspect.currentData()
        duration = int(self.duration.currentData())
        shared = [path for path in self.shared_images if os.path.isfile(path)]
        self.queue = [{
            "prompt": line,
            "status": "Chờ",
            "detail": "",
            "aspect": aspect,
            "duration": duration,
            "images": shared[:self._slots(line)],
        } for line in lines]
        self.page = 0
        self._render_page()
        self._refresh_stats()

    def _render_page(self):
        for card in self.cards:
            card.shutdown()
            card.setParent(None)
            card.deleteLater()
        self.cards = []
        pages = self._page_count()
        if self.page >= pages:
            self.page = max(0, pages - 1)
        start = self.page * PAGE_SIZE
        characters = load_characters(self._characters_path()) if self.queue else []
        for offset, item in enumerate(self.queue[start:start + PAGE_SIZE]):
            card = ResultCard(
                start + offset + 1, item["prompt"], item["aspect"], item["duration"], characters,
                item.get("images") or [], self._slots(item["prompt"]))
            card.set_kind(self.mode_key)
            card.images_changed.connect(lambda paths, row=start + offset: self._set_card_images(row, paths))
            if item["status"] != "Chờ" or item["detail"]:
                card.set_state(item["status"], item["detail"])
                if item["status"] == "Đang chạy":
                    card.set_progress(item.get("progress") or 8)
            self.cards.append(card)
        self._card_cols = 0
        self._place_cards()
        shown = f"Trang {self.page + 1}/{pages}" if self.queue else "Trang 0/0"
        self.page_label.setText(f"{shown} · {PAGE_SIZE}/trang")
        self.prev_btn.setEnabled(self.page > 0)
        self.next_btn.setEnabled(bool(self.queue) and self.page + 1 < pages)

    def eventFilter(self, obj, event):
        scroll = getattr(self, "card_scroll", None)
        timer = getattr(self, "_resize_timer", None)
        if scroll is not None and timer is not None and obj is scroll.viewport() and event.type() == QEvent.Type.Resize:
            timer.start(120)
        return super().eventFilter(obj, event)

    def _place_cards(self):
        if not hasattr(self, "card_grid") or getattr(self, "_placing", False):
            return
        self._placing = True
        try:
            width = self.card_scroll.viewport().width() if hasattr(self, "card_scroll") else 1100
            current = self._card_cols
            if current == 5 and width < 1040:
                cols = 4
            elif current == 4 and width >= 1180:
                cols = 5
            elif current in (4, 5):
                cols = current
            else:
                cols = 5 if width >= 1100 else 4
            gap = 8
            card_w = max(168, (width - 16 - gap * (cols - 1)) // cols)
            same_grid = cols == current and self.card_grid.count() == len(self.cards)
            if same_grid and abs(card_w - getattr(self, "_card_w", 0)) < 8:
                return
            self._card_w = card_w
            if same_grid:
                for card in self.cards:
                    card.fit_width(card_w)
                return
            self._card_cols = cols
            while self.card_grid.count():
                item = self.card_grid.takeAt(0)
                widget = item.widget()
                if widget is not None and widget not in self.cards:
                    widget.deleteLater()
            rows = 0
            for index, card in enumerate(self.cards):
                card.fit_width(card_w)
                row, col = divmod(index, cols)
                rows = row + 1
                self.card_grid.addWidget(card, row, col, Qt.AlignLeft | Qt.AlignTop)
                self.card_grid.setRowStretch(row, 0)
                self.card_grid.setColumnStretch(col, 0)
            self.card_grid.setRowStretch(rows, 1)
            self.card_grid.setColumnStretch(cols, 1)
        finally:
            self._placing = False

    def _refresh_stats(self):
        total = len(self.queue)
        running = ok = bad = 0
        for item in self.queue:
            text = item["status"]
            if text == "Xong":
                ok += 1
            elif text == "Lỗi":
                bad += 1
            elif text and text not in ("Chờ", "Đã dừng"):
                running += 1
        self.stat_total.setText(f"Tổng: {total}")
        self.stat_running.setText(f"Đang chạy: {running}")
        self.stat_ok.setText(f"Thành công: {ok}")
        rate = round(100 * ok / total) if total else 0
        self.stat_rate.setText(f"Tạo được: {rate}% ({ok}/{total})")
        self.stat_error.setText(f"Lỗi: {bad}")
        for label in (self.stat_total, self.stat_running, self.stat_ok, self.stat_rate, self.stat_error):
            width = label.fontMetrics().horizontalAdvance(label.text()) + 20
            label.setMinimumWidth(width)

        if hasattr(self, "retry_btn"):
            self.retry_btn.setVisible(bad > 0 and (self.worker is None or not self.worker.isRunning()))

        if total > 0:
            if running > 0:
                self.setWindowTitle(f"MUSE DESKTOP — Đang tạo {rate}% ({ok}/{total} video)")
            elif ok == total:
                self.setWindowTitle(f"MUSE DESKTOP — Hoàn thành 100% ({ok}/{total} video)")
            else:
                self.setWindowTitle(f"MUSE DESKTOP — Đã tạo {rate}% ({ok}/{total} video)")

    def _log(self, text: str):
        self.status_label.setText(time.strftime("%H:%M:%S ") + text)

    def _set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        if hasattr(self, "retry_btn"):
            self.retry_btn.setEnabled(not running)

    def auth_base(self) -> str:
        return (self.settings.value("auth_url", DEFAULT_AUTH_URL) or DEFAULT_AUTH_URL).strip()

    def _refresh_session(self):
        token = self.settings.value("auth_token", "") or ""
        if not token:
            self.session = None
            self._paint_session()
            return
        try:
            self.session = auth_me(self.auth_base(), token).get("user")
        except AuthError:
            self.session = None
            self.settings.remove("auth_token")
        self._paint_session()

    def _paint_session(self):
        user = self.session or {}
        if user.get("active"):
            self.session_label.setText(f"{user['username']} · gói {user['plan_threads']} luồng")
            self.login_btn.setText("Đăng xuất")
        else:
            self.session_label.setText("Chưa đăng nhập")
            self.login_btn.setText("Đăng nhập")

    def toggle_login(self):
        if self.session and self.session.get("active"):
            token = self.settings.value("auth_token", "") or ""
            if token:
                try:
                    auth_logout(self.auth_base(), token)
                except AuthError:
                    pass
            self.settings.remove("auth_token")
            self.session = None
            self._paint_session()
        self.show_login(required=True)

    def show_login(self, required: bool = False):
        while not (self.session and self.session.get("active") and self.session.get("enabled", True)):
            dialog = QDialog(self)
            dialog.setWindowTitle("Đăng nhập")
            dialog.setModal(True)
            form = QFormLayout(dialog)
            name = QLineEdit(self.settings.value("saved_user", "") or "")
            password = QLineEdit(self.settings.value("saved_pass", "") or "")
            password.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow("Tên đăng nhập", name)
            form.addRow("Mật khẩu", password)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            form.addRow(buttons)
            if dialog.exec() != QDialog.Accepted:
                if required:
                    QApplication.instance().quit()
                return
            try:
                result = auth_login(self.auth_base(), name.text().strip(), password.text())
            except AuthError as exc:
                QMessageBox.warning(self, "Đăng nhập", str(exc))
                continue
            user = result.get("user") or {}
            if not user.get("enabled", True) or not user.get("active"):
                QMessageBox.warning(self, "Đăng nhập", "Tài khoản đang bị khóa hoặc gói đã hết hạn.")
                continue
            self.settings.setValue("saved_user", name.text().strip())
            self.settings.setValue("saved_pass", password.text())
            self.settings.setValue("auth_token", result.get("token", ""))
            self.session = user
            self._paint_session()
            self._log(f"Đăng nhập {user['username']}, gói {user['plan_threads']} luồng.")
            return

    def check_update(self, silent: bool = False):
        """Kiểm tra bản cập nhật mới từ GitHub Releases."""
        repo = self.settings.value("update_repo", DEFAULT_REPO) or DEFAULT_REPO
        current_ver = get_current_version()

        self.check_thread = CheckUpdateThread(repo, current_ver)

        def on_found(info):
            dialog = UpdateDialog(info, self)
            dialog.exec()

        def on_none():
            if not silent:
                QMessageBox.information(
                    self, "Cập nhật",
                    f"Bạn đang sử dụng phiên bản mới nhất (v{current_ver}).")

        def on_err(err):
            if not silent:
                QMessageBox.warning(self, "Lỗi kiểm tra", f"Không thể kiểm tra cập nhật: {err}")

        self.check_thread.update_found.connect(on_found)
        self.check_thread.no_update.connect(on_none)
        self.check_thread.check_failed.connect(on_err)
        self.check_thread.start()

    def start_batch(self):
        if self.worker and self.worker.isRunning():
            return
        self._refresh_session()
        if not self.session or not self.session.get("active"):
            QMessageBox.warning(self, "Đăng nhập", "Hãy đăng nhập gói 5, 10 hoặc 20 luồng trước.")
            return
        self.reload_accounts()
        account_id = self._account_ids[0] if self._account_ids else ""
        if not account_id:
            QMessageBox.warning(self, "Tài khoản", "Hãy nhập cookie Muse trước.")
            return
        lines = self._prompt_lines()
        if not lines:
            QMessageBox.warning(self, "Prompt", "Hãy chọn file .txt hoặc thư mục chứa file .txt.")
            return
        mode = self.mode_key
        if mode == "i2v" and self.queue:
            missing = [
                item["prompt"] for item in self.queue
                if not tags_in(item["prompt"]) and not item.get("images")
            ]
            if missing:
                QMessageBox.warning(
                    self, "Ảnh",
                    "Có card chưa có ảnh và prompt không có @nhân vật. "
                    "Hãy chọn ảnh chung hoặc ảnh trên card đó.")
                return
        out_dir = self.out_dir.text().strip()
        if not out_dir:
            QMessageBox.warning(self, "Thư mục", "Hãy chọn thư mục lưu.")
            return
        os.makedirs(out_dir, exist_ok=True)
        if not self.queue:
            self._build_queue(lines)
        aspect = self.aspect.currentData()
        duration = int(self.duration.currentData())
        for item in self.queue:
            item["status"] = "Chờ"
            item["detail"] = ""
            item["aspect"] = aspect
            item["duration"] = duration
        self._render_page()
        jobs = [{
            "index": index,
            "prompt": item["prompt"],
            "mode": mode,
            "aspect": aspect,
            "duration": duration,
            "images": list(item.get("images") or []),
            "cached_image": item.get("cached_image"),
        } for index, item in enumerate(self.queue)]
        self._save_settings()
        characters = load_characters(self._characters_path())
        self.stop_event = threading.Event()
        account_ids = list(self._account_ids) or [account_id]
        self.worker = BatchWorker(
            jobs, account_id, out_dir, self.merge_box.isChecked() and mode != "t2i",
            self.stop_event, characters, account_ids, int(self.session["plan_threads"]))
        self.worker.job_changed.connect(self._on_job)
        self.worker.progress.connect(self._on_progress)
        self.worker.logged.connect(self._log)
        self.worker.batch_finished.connect(self._on_finished)
        self._set_running(True)
        self._log(
            f"Bắt đầu {len(jobs)} prompt, {len(account_ids)} tài khoản, "
            f"khung {self.aspect.currentData()}, {self.duration.currentData()} giây.")
        self.worker.start()

    def retry_failed_cards(self):
        """Chỉ chạy lại các card bị lỗi hoặc bị dừng mà không tạo lại các card đã thành công."""
        if self.worker and self.worker.isRunning():
            return
        failed_indices = [i for i, item in enumerate(self.queue) if item["status"] in ("Lỗi", "Đã dừng")]
        if not failed_indices:
            QMessageBox.information(self, "Thông báo", "Không có card nào bị lỗi để tạo lại.")
            return
        self.reload_accounts()
        if not self._account_ids:
            QMessageBox.warning(self, "Tài khoản", "Hãy nhập cookie Muse trước.")
            return
        self._refresh_session()
        if not self.session or not self.session.get("active"):
            QMessageBox.warning(self, "Đăng nhập", "Hãy đăng nhập gói 5, 10 hoặc 20 luồng trước.")
            return

        aspect = self.aspect.currentData()
        duration = int(self.duration.currentData())
        mode = self.mode_key
        for idx in failed_indices:
            item = self.queue[idx]
            item["status"] = "Chờ"
            item["detail"] = ""
            card = self._card_for(idx)
            if card is not None:
                card.set_state("Chờ", "")
        self._refresh_stats()

        jobs = [{
            "index": idx,
            "prompt": self.queue[idx]["prompt"],
            "mode": mode,
            "aspect": aspect,
            "duration": duration,
            "images": list(self.queue[idx].get("images") or []),
            "cached_image": self.queue[idx].get("cached_image"),
        } for idx in failed_indices]

        out_dir = self.out_dir.text().strip()
        characters = load_characters(self._characters_path())
        self.stop_event = threading.Event()
        account_ids = list(self._account_ids)
        self.worker = BatchWorker(
            jobs, account_ids[0], out_dir, self.merge_box.isChecked() and mode != "t2i",
            self.stop_event, characters, account_ids, int(self.session["plan_threads"]))
        self.worker.job_changed.connect(self._on_job)
        self.worker.progress.connect(self._on_progress)
        self.worker.logged.connect(self._log)
        self.worker.batch_finished.connect(self._on_finished)
        self._set_running(True)
        self._log(f"Bắt đầu tạo lại {len(jobs)} card chưa hoàn thành với {len(account_ids)} tài khoản.")
        self.worker.start()

    def stop_batch(self):
        self.stop_event.set()
        self._log("Sẽ dừng sau bước hiện tại.")
        self.stop_btn.setEnabled(False)

    def _card_for(self, job_index: int) -> ResultCard | None:
        for card in self.cards:
            if card.job_index == job_index:
                return card
        return None

    def _on_job(self, row: int, status: str, detail: str):
        targets = range(len(self.queue)) if row < 0 else (row,)
        for index in targets:
            if not (0 <= index < len(self.queue)):
                continue
            if row < 0 and self.queue[index]["status"] not in ("Chờ", "Đang chạy"):
                continue
            self.queue[index]["status"] = status
            if detail:
                self.queue[index]["detail"] = detail
            card = self._card_for(index)
            if card is not None:
                card.set_state(status, detail)
        self._refresh_stats()

    def _on_progress(self, row: int, value: int):
        if not (0 <= row < len(self.queue)):
            return
        if self.queue[row]["status"] in ("Xong", "Lỗi"):
            return
        self.queue[row]["status"] = "Đang chạy"
        self.queue[row]["progress"] = value
        card = self._card_for(row)
        if card is not None:
            card.set_progress(value)

    def closeEvent(self, event):
        self.stop_event.set()
        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.wait(8000)
        event.accept()

    def _on_finished(self, merged: str):
        self._set_running(False)
        total = len(self.queue)
        ok = sum(1 for item in self.queue if item["status"] == "Xong")
        bad = sum(1 for item in self.queue if item["status"] == "Lỗi")
        rate = round(100 * ok / total) if total else 0
        self._refresh_stats()
        if ok == total and total > 0:
            msg = f"Tuyệt vời! Đã hoàn thành 100% ({ok}/{total} video đã tạo thành công)."
            if merged:
                msg += f" File gộp: {os.path.basename(merged)}"
            self._log(msg)
        elif merged:
            self._log(f"Đã hoàn thành {ok}/{total} video ({rate}%). File gộp: {os.path.basename(merged)}")
        elif bad > 0:
            failed = next((item for item in self.queue if item["status"] == "Lỗi"), {})
            self._log(f"Hàng đợi kết thúc: {ok}/{total} video ({rate}% thành công). {failed.get('detail', 'Có card chưa tạo được')}")
        else:
            self._log(f"Hàng đợi đã kết thúc: {ok}/{total} video ({rate}% thành công).")


def _style() -> str:
    return """
    QWidget { background: #f8fafc; color: #0f172a; font-size: 13px; }
    QFrame#tabBar, QFrame#subbar { background: white; border-bottom: 1px solid #e2e8f0; }
    QPushButton#tab {
        background: transparent; color: #64748b; border: 1px solid transparent;
        border-radius: 10px; padding: 6px 14px; font-weight: 700; font-size: 12px;
    }
    QPushButton#tab:hover { background: #f1f5f9; color: #0f172a; }
    QPushButton#tab:checked { background: #0f172a; color: white; }
    QLabel#muted, QLabel#field { color: #64748b; background: transparent; font-size: 11px; font-weight: 600; }
    QPushButton#sub {
        background: #f8fafc; color: #475569; border: 1px solid #e2e8f0;
        border-radius: 6px; padding: 5px 12px; font-size: 11px; font-weight: 700;
    }
    QPushButton#sub:checked { background: #0f172a; color: white; border-color: #0f172a; }
    QFrame#card, QFrame#resultCard { background: white; border: 1px solid #e2e8f0; border-radius: 16px; }
    QLabel#chip {
        background: #f1f5f9; color: #475569; border: 1px solid #e2e8f0;
        border-radius: 999px; padding: 2px 8px; font-size: 11px; font-weight: 700;
    }
    QLabel#stage { background: #0f172a; color: #94a3b8; border-radius: 12px; font-weight: 700; }
    QLabel#wait { color: #64748b; font-weight: 700; background: transparent; }
    QLabel#cardTitle {
        color: #64748b; font-size: 11px; font-weight: 800; letter-spacing: 0.4px; background: transparent;
    }
    QLineEdit, QPlainTextEdit, QComboBox {
        background: white; color: #0f172a; border: 1px solid #cbd5e1; border-radius: 10px; padding: 7px 10px;
    }
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border: 1px solid #0f172a; }
    QLabel#stat, QLabel#running, QLabel#ok, QLabel#bad { padding: 0 4px; }
    QPushButton {
        background: white; color: #0f172a; border: 1px solid #cbd5e1; border-radius: 10px; padding: 7px 12px; font-weight: 700;
    }
    QPushButton:hover { background: #f1f5f9; }
    QPushButton#small { padding: 4px 10px; font-size: 11px; border-radius: 6px; }
    QPushButton#thumb { padding: 0; border-radius: 8px; border: 1px solid #e2e8f0; background: #f8fafc; }
    QPushButton#primary {
        background: #0f172a; color: white; border: none; min-height: 42px; font-size: 13px; border-radius: 10px;
    }
    QPushButton#primary:hover { background: #1e293b; }
    QPushButton#danger { background: #fef2f2; color: #ef4444; border: 1px solid #fca5a5; }
    QPushButton:disabled { color: #94a3b8; background: #f8fafc; }
    QCheckBox { background: transparent; font-weight: 600; }
    QFrame#stats { background: #f8fafc; border-top: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; }
    QLabel#running { color: #2563eb; font-weight: 700; background: transparent; }
    QLabel#ok { color: #16a34a; font-weight: 700; background: transparent; }
    QLabel#bad { color: #dc2626; font-weight: 700; background: transparent; }
    QTableWidget {
        background: white; alternate-background-color: #f8fafc; border: none; color: #0f172a; font-size: 12px;
    }
    QHeaderView::section {
        background: #f8fafc; color: #475569; font-weight: 800; font-size: 11px;
        border: none; border-bottom: 1px solid #e2e8f0; padding: 8px;
    }
    QTableWidget::item:selected { background: #eff6ff; color: #1e40af; }
    QFrame#footer { background: #0f172a; }
    QLabel#status { color: #e2e8f0; background: transparent; font-size: 11px; }
    QLabel#engine { color: #64748b; background: transparent; font-size: 10px; }
    QScrollArea { background: transparent; border: none; }
    """


class LoginDialog(QDialog):
    """Form đăng nhập. Có thể điền sẵn, nhưng không tự gửi."""

    def __init__(self, settings: QSettings):
        super().__init__()
        self.settings = settings
        self.user = None
        self.token = ""
        self.setWindowTitle("Đăng nhập")
        self.resize(420, 180)
        form = QFormLayout(self)
        self.name = QLineEdit(settings.value("saved_user", "") or "tempuser")
        self.password = QLineEdit(settings.value("saved_pass", "") or "TempMuse2026")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.name.setPlaceholderText("tempuser")
        form.addRow("Tên đăng nhập", self.name)
        form.addRow("Mật khẩu", self.password)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Đăng nhập")
        buttons.button(QDialogButtonBox.Cancel).setText("Thoát")
        buttons.accepted.connect(self.try_login)
        buttons.rejected.connect(QApplication.quit)
        form.addRow(buttons)

    def closeEvent(self, event):
        if not self.user:
            QApplication.quit()
        event.accept()

    def try_login(self):
        base = (self.settings.value("auth_url", DEFAULT_AUTH_URL) or DEFAULT_AUTH_URL).strip()
        try:
            result = auth_login(base, self.name.text().strip(), self.password.text())
        except AuthError as exc:
            QMessageBox.warning(self, "Đăng nhập", str(exc))
            return
        user = result.get("user") or {}
        if not user.get("enabled", True) or not user.get("active"):
            QMessageBox.warning(self, "Đăng nhập", "Tài khoản đang bị khóa hoặc gói đã hết hạn.")
            return
        self.settings.setValue("saved_user", self.name.text().strip())
        self.settings.setValue("saved_pass", self.password.text())
        self.settings.setValue("auth_token", result.get("token", ""))
        self.user = user
        self.token = result.get("token", "")
        self.accept()


def main():
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(_style())
    app.setFont(QFont("Helvetica Neue", 13))
    settings = QSettings("muse2api", "desktop")
    settings.remove("auth_token")
    gate = LoginDialog(settings)

    def open_tool():
        if not gate.user:
            return
        window = MainWindow()
        window.session = gate.user
        window._paint_session()
        window.show()
        window._log(f"Đăng nhập {gate.user['username']}, gói {gate.user['plan_threads']} luồng.")
        app._muse_window = window

    gate.accepted.connect(open_tool)
    gate.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
