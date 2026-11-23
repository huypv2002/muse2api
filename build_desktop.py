#!/usr/bin/env python3
"""Script đóng gói ứng dụng Muse Desktop bằng Nuitka.

Hỗ trợ chạy cục bộ hoặc trên GitHub Actions (Windows, macOS, Linux).
Tự động tạo file nén .zip để xuất bản Release.

Cách dùng:
    python build_desktop.py
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile

# Ép UTF-8 cho stdout/stderr để tránh lỗi charmap cp1252 trên Windows CI / console
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))


def get_version() -> str:
    from updater import get_current_version
    return get_current_version(ROOT)


def prepare_data_template(temp_data_dir: str):
    """Chuẩn bị thư mục data mẫu sạch sẽ, không chứa cookie riêng tư hay media của người dùng."""
    os.makedirs(temp_data_dir, exist_ok=True)
    example_acc = os.path.join(ROOT, "data", "accounts.example.json")
    if os.path.isfile(example_acc):
        shutil.copy2(example_acc, os.path.join(temp_data_dir, "accounts.example.json"))
    
    os.makedirs(os.path.join(temp_data_dir, "media"), exist_ok=True)
    os.makedirs(os.path.join(temp_data_dir, "downloads"), exist_ok=True)


def build(output_dir: str = "dist"):
    system = platform.system().lower()
    machine = platform.machine().lower()
    version = get_version()
    print(f"=== BẮT ĐẦU ĐÓNG GÓI NUITKA: Muse Desktop v{version} trên {system} ({machine}) ===")

    output_dir_abs = os.path.abspath(os.path.join(ROOT, output_dir))
    os.makedirs(output_dir_abs, exist_ok=True)

    # Thư mục data sạch cho bản phát hành
    clean_data_dir = os.path.join(ROOT, ".clean_data_build")
    prepare_data_template(clean_data_dir)

    # Lệnh cơ bản Nuitka
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        "--enable-plugin=pyside6",
        "--follow-imports",
        f"--output-dir={output_dir_abs}",
        f"--include-data-dir={clean_data_dir}=data",
    ]

    # Include extension nếu tồn tại
    ext_dir = os.path.join(ROOT, "extension")
    if os.path.isdir(ext_dir):
        cmd.append(f"--include-data-dir={ext_dir}=extension")

    # Include version.json & admin.html nếu tồn tại
    for f in ("version.json", "admin.html"):
        fp = os.path.join(ROOT, f)
        if os.path.isfile(fp):
            cmd.append(f"--include-data-file={fp}={f}")

    # Cờ riêng theo từng hệ điều hành
    if "windows" in system:
        cmd.extend([
            "--windows-console-mode=disable",
            "--output-filename=MuseDesktop.exe",
        ])
        dist_name = "gui_app.dist"
        archive_name = f"MuseDesktop-Windows-{machine}-v{version}.zip"
    elif "darwin" in system:
        cmd.extend([
            "--macos-create-app-bundle",
            "--macos-app-name=MuseDesktop",
            "--macos-signed-app-name=com.muse2api.desktop",
            "--output-filename=MuseDesktop",
        ])
        dist_name = "gui_app.app" if os.path.exists(os.path.join(output_dir_abs, "gui_app.app")) else "MuseDesktop.app"
        archive_name = f"MuseDesktop-macOS-{machine}-v{version}.zip"
    else:  # Linux
        cmd.extend([
            "--output-filename=MuseDesktop",
        ])
        dist_name = "gui_app.dist"
        archive_name = f"MuseDesktop-Linux-{machine}-v{version}.zip"

    # File mã nguồn chính
    main_script = os.path.join(ROOT, "gui_app.py")
    cmd.append(main_script)

    print("Đang thực thi:", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    finally:
        shutil.rmtree(clean_data_dir, ignore_errors=True)

    # Tìm thư mục phân phối kết quả
    built_path = None
    for candidate in ("MuseDesktop.app", "gui_app.app", "gui_app.dist", "MuseDesktop.dist"):
        p = os.path.join(output_dir_abs, candidate)
        if os.path.exists(p):
            built_path = p
            break

    if not built_path:
        # Tìm file đơn lẻ
        for candidate in ("MuseDesktop.exe", "gui_app.exe", "MuseDesktop"):
            p = os.path.join(output_dir_abs, candidate)
            if os.path.isfile(p):
                built_path = p
                break

    if not built_path:
        print(f"LỖI: Không tìm thấy sản phẩm build trong {output_dir_abs}")
        sys.exit(1)

    # Đổi tên thư mục phân phối sang tên chuẩn MuseDesktop
    target_folder_name = "MuseDesktop.app" if "darwin" in system else "MuseDesktop"
    target_dist_path = os.path.join(output_dir_abs, target_folder_name)
    if os.path.isdir(built_path) and os.path.abspath(built_path) != os.path.abspath(target_dist_path):
        if os.path.exists(target_dist_path):
            if os.path.isdir(target_dist_path):
                shutil.rmtree(target_dist_path)
            else:
                os.remove(target_dist_path)
        shutil.move(built_path, target_dist_path)
        built_path = target_dist_path

    print(f"Build thành công: {built_path}")

    # Đóng gói thành file zip
    archive_path = os.path.join(output_dir_abs, archive_name)
    print(f"Đang nén thành: {archive_path}")

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(built_path):
            base_dir_name = os.path.basename(built_path)
            for root_dir, _dirs, files in os.walk(built_path):
                for file in files:
                    full_p = os.path.join(root_dir, file)
                    rel_p = os.path.relpath(full_p, os.path.dirname(built_path))
                    zf.write(full_p, rel_p)
        else:
            zf.write(built_path, os.path.basename(built_path))

    print(f"=== HOÀN TẤT: File phát hành sẵn sàng tại: {archive_path} ===")
    return archive_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Muse Desktop with Nuitka")
    parser.add_argument("--output-dir", default="dist", help="Thư mục xuất kết quả")
    args = parser.parse_args()
    build(args.output_dir)
