"""Thư viện nhân vật @tag và bộ nhớ ảnh đã đăng theo tài khoản."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile

MAX_REFERENCE_IMAGES = 10
TAG_RE = re.compile(r"@([A-Za-z0-9_\u00C0-\u024F\u1EA0-\u1EF9]+)")


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tags_in(prompt: str) -> list[str]:
    seen: list[str] = []
    folded = set()
    for match in TAG_RE.finditer(prompt or ""):
        name = match.group(1)
        key = name.lower()
        if key not in folded:
            folded.add(key)
            seen.append(name)
    return seen


def replace_tags(prompt: str, index_of: dict[str, int]) -> str:
    folded = {name.lower(): index for name, index in index_of.items()}

    def swap(match: re.Match) -> str:
        index = folded.get(match.group(1).lower())
        return f"[图{index}]" if index else match.group(0)

    return TAG_RE.sub(swap, prompt or "")


def resolve_refs(prompt: str, characters: list[dict], extra_path: str = "",
                 extra_paths: list[str] | None = None) -> tuple[list[tuple[str, str]], str]:
    """Trả về [(tên, đường dẫn)] theo thứ tự xuất hiện, hoặc lỗi."""
    by_name = {}
    for item in characters:
        name = (item.get("name") or "").strip()
        path = item.get("image") or ""
        if name:
            by_name[name.lower()] = (name, path)
    pairs: list[tuple[str, str]] = []
    seen_hash: set[str] = set()

    def add(name: str, path: str) -> str:
        if not path or not os.path.isfile(path):
            return f"Không thấy ảnh của @{name}" if name else "Không thấy ảnh nguồn"
        digest = file_hash(path)
        if digest in seen_hash:
            return ""
        seen_hash.add(digest)
        pairs.append((name, path))
        if len(pairs) > MAX_REFERENCE_IMAGES:
            return f"Một prompt chỉ được tối đa {MAX_REFERENCE_IMAGES} ảnh tham chiếu"
        return ""

    paths = list(extra_paths or [])
    if extra_path and extra_path not in paths:
        paths.insert(0, extra_path)
    for number, path in enumerate(paths, 1):
        error = add(f"ảnh {number}", path)
        if error:
            return [], error
    for tag in tags_in(prompt):
        found = by_name.get(tag.lower())
        if not found:
            return [], f"Chưa có nhân vật @{tag} trong thư viện"
        error = add(found[0], found[1])
        if error:
            return [], error
    return pairs, ""


def compose_prompt(user: str, aspect: str, duration: int, legend: list[tuple[int, str, bool]],
                   kind: str) -> str:
    """Lệnh tạo media. Câu spec suông khiến Muse chỉ trả lời chữ, không xuất file."""
    index_of = {name: index for index, name, _is_new in legend if name != "ảnh nguồn"}
    text = replace_tags(user, index_of)
    mapping = "，".join(
        f"[图{index}]是{'起始帧' if name == 'ảnh nguồn' else '角色' + name}"
        for index, name, _is_new in legend)
    must = "必须直接输出成片文件，不要只回复文字、不要复述参数。"
    if kind == "image":
        frame = "严格9:16竖屏、720x1280" if aspect == "9:16" else "16:9横屏"
        return (
            f"基于我本次上传的参考图片（{mapping}；只使用这些图片，严禁使用历史图片）："
            f"输出一张{frame}的图片。{must}{text}"
        )
    if aspect == "9:16":
        order = (
            "基于我本次上传的参考图片附件作为第一帧参考图"
            f"（{mapping}；严禁使用历史图片或任何其他图像，必须严格以本次上传的图片为起始帧）："
            "生成一个严格9:16竖屏手机满屏的动态图生视频"
            "（9:16 vertical portrait video，720x1280，高大于宽的手机全屏竖版画面，"
            f"严禁生成横屏或黑边，时长严格为 {duration} 秒）。{must}"
        )
    else:
        order = (
            "基于我本次上传的参考图片附件作为第一帧参考图"
            f"（{mapping}；严禁使用历史图片或任何其他图像，必须严格以本次上传的图片为起始帧）："
            "生成一个16:9宽屏横屏图生视频"
            f"（16:9 widescreen landscape video，时长严格为 {duration} 秒）。{must}"
        )
    return f"{order}{text}"


class RefMemory:
    """Ảnh đã đăng thành công trên một tài khoản, gắn với một hội thoại Muse."""

    def __init__(self, path: str, account_id: str):
        self.path = path
        self.account_id = account_id
        self.url = ""
        self.images: list[dict] = []
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            data = {}
        saved = data.get(self.account_id) or {}
        self.url = saved.get("url") or ""
        self.images = list(saved.get("images") or [])

    def save(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            data = {}
        data[self.account_id] = {"url": self.url, "images": self.images}
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.remove(temp)

    def _known(self, digest: str) -> dict | None:
        return next((item for item in self.images if item.get("hash") == digest), None)

    def plan(self, pairs: list[tuple[str, str]]) -> tuple[list[tuple[int, str, bool]], list[tuple[str, str]]]:
        """(legend index/tên/mới, các file cần upload theo thứ tự)."""
        legend: list[tuple[int, str, bool]] = []
        uploads: list[tuple[str, str]] = []
        staged: list[dict] = []
        if not self.url:
            self.images = []
        for name, path in pairs:
            digest = file_hash(path)
            found = self._known(digest) or next((item for item in staged if item["hash"] == digest), None)
            if found and self.url:
                legend.append((found["index"], name, False))
                continue
            index = 1 + max([item["index"] for item in self.images] + [item["index"] for item in staged] + [0])
            if not self.url:
                index = len(staged) + 1
            legend.append((index, name, True))
            uploads.append((path, digest))
            staged.append({"hash": digest, "name": name, "index": index, "path": path})
        self._staged = staged
        return legend, uploads

    def commit(self, url: str):
        for item in getattr(self, "_staged", []):
            if not self._known(item["hash"]):
                self.images.append(item)
        if url:
            self.url = url
        self._staged = []
        self.save()

    def reset(self):
        self.url = ""
        self.images = []
        self._staged = []
        self.save()


def load_characters(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_characters(path: str, items: list[dict]):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.remove(temp)
