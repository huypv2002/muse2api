"""Tự động kiểm tra bản cập nhật mới từ GitHub, tải về, cập nhật và khởi động lại."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

# Repo mặc định trên GitHub (có thể cấu hình qua biến môi trường hoặc QSettings)
DEFAULT_REPO = os.environ.get("MUSE_DESKTOP_REPO", "huypv2002/muse2api")
APP_VERSION = "1.7.0"


def parse_semver(ver: str) -> tuple[int, ...]:
    """Chuyển chuỗi version dạng v1.7.0 thành tuple số (1, 7, 0) để so sánh."""
    cleaned = re.sub(r"^[^\d]*", "", str(ver).strip())
    parts = []
    for chunk in cleaned.split("."):
        m = re.match(r"^(\d+)", chunk)
        if m:
            parts.append(int(m.group(1)))
        else:
            break
    return tuple(parts) if parts else (0, 0, 0)


def get_current_version(base_dir: str | None = None) -> str:
    """Đọc version hiện tại từ version.json nếu có, hoặc dùng APP_VERSION mặc định."""
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    vpath = os.path.join(root, "version.json")
    if os.path.isfile(vpath):
        try:
            with open(vpath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("version"):
                    return str(data["version"]).strip()
        except Exception:
            pass
    return APP_VERSION


def _match_asset(assets: list[dict]) -> dict | None:
    """Tìm asset tải về phù hợp với hệ điều hành hiện tại (Windows, macOS, Linux)."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    # Ưu tiên các pattern theo OS
    if "windows" in system:
        patterns = [r"win.*\.zip$", r"windows.*\.zip$", r".*\.exe$", r".*\.zip$"]
    elif "darwin" in system:
        patterns = [r"mac.*\.zip$", r"darwin.*\.zip$", r".*\.dmg$", r".*\.zip$"]
    else:  # Linux
        patterns = [r"linux.*\.tar\.gz$", r"linux.*\.zip$", r".*\.AppImage$", r".*\.tar\.gz$", r".*\.zip$"]

    for pat in patterns:
        regex = re.compile(pat, re.IGNORECASE)
        for asset in assets:
            name = asset.get("name", "")
            if regex.search(name):
                return asset

    # Nếu không khớp chính xác, lấy asset nén zip đầu tiên
    for asset in assets:
        name = asset.get("name", "").lower()
        if name.endswith((".zip", ".tar.gz", ".dmg", ".exe", ".appimage")):
            return asset
    return assets[0] if assets else None


def check_for_updates(repo: str = DEFAULT_REPO, current_ver: str | None = None) -> dict | None:
    """Kiểm tra bản phát hành mới nhất từ GitHub Releases API.
    
    Trả về dict thông tin cập nhật nếu có bản mới hơn, hoặc None nếu đã là bản mới nhất.
    """
    repo = (repo or DEFAULT_REPO).strip()
    if "/" not in repo:
        repo = DEFAULT_REPO

    curr = current_ver or get_current_version()
    curr_tuple = parse_semver(curr)

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": f"MuseDesktop-Updater/{curr}",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Chưa có release chính thức, thử kiểm tra tags
            return _check_from_tags(repo, curr, curr_tuple)
        return None
    except Exception:
        return None

    tag_name = data.get("tag_name") or ""
    remote_tuple = parse_semver(tag_name)
    if remote_tuple <= curr_tuple:
        return None

    assets = data.get("assets") or []
    target_asset = _match_asset(assets)
    if not target_asset:
        # Không có asset nhị phân, dùng zipball source
        download_url = data.get("zipball_url") or f"https://github.com/{repo}/archive/refs/tags/{tag_name}.zip"
        asset_name = f"{repo.split('/')[-1]}-{tag_name}.zip"
        asset_size = 0
    else:
        download_url = target_asset.get("browser_download_url") or ""
        asset_name = target_asset.get("name") or "update.zip"
        asset_size = int(target_asset.get("size") or 0)

    return {
        "has_update": True,
        "latest_version": tag_name.lstrip("v"),
        "current_version": curr.lstrip("v"),
        "release_title": data.get("name") or tag_name,
        "body": data.get("body") or "Bản cập nhật mới với nhiều cải tiến và sửa lỗi.",
        "html_url": data.get("html_url") or f"https://github.com/{repo}/releases",
        "download_url": download_url,
        "asset_name": asset_name,
        "asset_size": asset_size,
    }


def _check_from_tags(repo: str, curr: str, curr_tuple: tuple[int, ...]) -> dict | None:
    """Dự phòng: đọc từ raw version.json trên nhánh main của repo."""
    raw_url = f"https://raw.githubusercontent.com/{repo}/main/version.json"
    headers = {"User-Agent": f"MuseDesktop-Updater/{curr}"}
    req = urllib.request.Request(raw_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    ver = data.get("version", "")
    if parse_semver(ver) <= curr_tuple:
        return None

    highlights = "\n".join(f"- {h}" for h in data.get("highlights", []))
    zip_url = f"https://github.com/{repo}/archive/refs/heads/main.zip"
    return {
        "has_update": True,
        "latest_version": ver,
        "current_version": curr,
        "release_title": f"Bản cập nhật v{ver}",
        "body": highlights or "Cải tiến và sửa lỗi mới nhất.",
        "html_url": f"https://github.com/{repo}",
        "download_url": zip_url,
        "asset_name": "update.zip",
        "asset_size": 0,
    }


def download_file(url: str, dest_path: str, progress_callback=None, cancel_event=None) -> bool:
    """Tải file từ URL về dest_path, hỗ trợ báo tiến trình và huỷ tải."""
    headers = {"User-Agent": "MuseDesktop-Updater"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            block_size = 64 * 1024
            os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
            with open(dest_path, "wb") as f:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        return False
                    chunk = resp.read(block_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        percent = int(downloaded * 100 / total) if total > 0 else 0
                        progress_callback(downloaded, total, percent)
        return True
    except Exception as exc:
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except Exception:
                pass
        raise exc


def is_running_compiled() -> bool:
    """Kiểm tra xem app đang chạy dưới dạng file nhị phân compiled (Nuitka / PyInstaller) hay script python."""
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def apply_update_and_restart(update_file: str) -> None:
    """Giải nén / thay thế bản mới, tự động đóng app hiện tại và khởi chạy bản mới."""
    system = platform.system().lower()
    app_exe = sys.executable
    app_dir = os.path.dirname(os.path.abspath(app_exe)) if is_running_compiled() else os.path.dirname(os.path.abspath(__file__))
    current_script = os.path.abspath(sys.argv[0])

    staging_dir = tempfile.mkdtemp(prefix="muse_update_stage_")

    # Giải nén nếu là file zip
    if zipfile.is_zipfile(update_file):
        with zipfile.ZipFile(update_file, "r") as zf:
            zf.extractall(staging_dir)
    else:
        # Nếu là file đơn lẻ (executable)
        shutil.copy2(update_file, staging_dir)

    # Nếu file giải nén nằm trong một thư mục con duy nhất, di chuyển lên
    entries = os.listdir(staging_dir)
    if len(entries) == 1 and os.path.isdir(os.path.join(staging_dir, entries[0])):
        nested = os.path.join(staging_dir, entries[0])
        for item in os.listdir(nested):
            shutil.move(os.path.join(nested, item), staging_dir)
        try:
            os.rmdir(nested)
        except Exception:
            pass

    if "windows" in system:
        _apply_and_restart_windows(staging_dir, app_dir, app_exe, current_script)
    elif "darwin" in system:
        _apply_and_restart_macos(staging_dir, app_dir, app_exe, current_script)
    else:
        _apply_and_restart_linux(staging_dir, app_dir, app_exe, current_script)

    # Đóng ứng dụng ngay lập tức
    sys.exit(0)


def _apply_and_restart_windows(staging_dir: str, app_dir: str, app_exe: str, script: str):
    """Tạo batch script tự động chép đè sau khi ứng dụng thoát, rồi khởi động lại."""
    runner = os.path.join(tempfile.gettempdir(), f"muse_updater_{int(time.time())}.bat")
    
    if is_running_compiled():
        launch_target = f'"{app_exe}"'
    else:
        launch_target = f'"{sys.executable}" "{script}"'

    bat_content = f"""@echo off
chcp 65001 > nul
:: Chờ tiến trình cũ tắt hẳn
timeout /t 2 /nobreak > nul

:: Sao chép đè toàn bộ thư mục mới vào thư mục ứng dụng
xcopy /s /y /e /h "{staging_dir}\\*" "{app_dir}\\" > nul

:: Khởi chạy ứng dụng mới
start "" {launch_target}

:: Dọn dẹp thư mục tạm và tự xóa batch
rmdir /s /q "{staging_dir}" > nul 2>&1
del "%~f0" > nul 2>&1
"""
    with open(runner, "w", encoding="utf-8") as f:
        f.write(bat_content)

    # Chạy script detached không hiện cửa sổ console
    creation_flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
    subprocess.Popen(["cmd.exe", "/c", runner], creationflags=creation_flags, close_fds=True)


def _apply_and_restart_macos(staging_dir: str, app_dir: str, app_exe: str, script: str):
    """Tạo bash script cập nhật và khởi chạy lại trên macOS."""
    runner = os.path.join(tempfile.gettempdir(), f"muse_updater_{int(time.time())}.sh")

    # Kiểm tra xem có phải chạy trong .app bundle không
    app_bundle = ""
    parts = app_exe.split(".app")
    if len(parts) > 1:
        app_bundle = parts[0] + ".app"

    # Tìm xem trong staging có file .app mới không
    new_app = ""
    for item in os.listdir(staging_dir):
        if item.endswith(".app"):
            new_app = os.path.join(staging_dir, item)
            break

    if app_bundle and new_app:
        copy_cmd = f'rm -rf "{app_bundle}" && cp -R "{new_app}" "{os.path.dirname(app_bundle)}/"'
        launch_cmd = f'open -n "{app_bundle}"'
    elif is_running_compiled():
        copy_cmd = f'cp -R "{staging_dir}/"* "{app_dir}/"'
        launch_cmd = f'"{app_exe}" &'
    else:
        copy_cmd = f'cp -R "{staging_dir}/"* "{app_dir}/"'
        launch_cmd = f'"{sys.executable}" "{script}" &'

    sh_content = f"""#!/bin/bash
sleep 2
{copy_cmd}
{launch_cmd}
rm -rf "{staging_dir}"
rm -f "$0"
"""
    with open(runner, "w", encoding="utf-8") as f:
        f.write(sh_content)
    os.chmod(runner, 0o755)

    subprocess.Popen(["/bin/bash", runner], close_fds=True)


def _apply_and_restart_linux(staging_dir: str, app_dir: str, app_exe: str, script: str):
    """Tạo bash script cập nhật và khởi chạy lại trên Linux."""
    runner = os.path.join(tempfile.gettempdir(), f"muse_updater_{int(time.time())}.sh")

    if is_running_compiled():
        launch_cmd = f'"{app_exe}" &'
    else:
        launch_cmd = f'"{sys.executable}" "{script}" &'

    sh_content = f"""#!/bin/bash
sleep 2
cp -R "{staging_dir}/"* "{app_dir}/"
{launch_cmd}
rm -rf "{staging_dir}"
rm -f "$0"
"""
    with open(runner, "w", encoding="utf-8") as f:
        f.write(sh_content)
    os.chmod(runner, 0o755)

    subprocess.Popen(["/bin/bash", runner], close_fds=True)
