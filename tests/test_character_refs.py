"""Nhân vật @tag, tối đa 10 ảnh, và bỏ qua ảnh đã đăng cùng tài khoản."""
import os
import tempfile

from refs import RefMemory, compose_prompt, resolve_refs, tags_in


def _png(path: str, payload: bytes):
    with open(path, "wb") as handle:
        handle.write(payload)


def main():
    assert tags_in("@linh đi cùng @Linh và @hoa") == ["linh", "hoa"]
    folder = tempfile.mkdtemp()
    paths = []
    for index in range(11):
        path = os.path.join(folder, f"{index}.png")
        _png(path, b"img-%d" % index)
        paths.append(path)
    characters = [{"name": f"nv{index}", "image": path} for index, path in enumerate(paths)]
    prompt = " ".join(f"@nv{index}" for index in range(11))
    _pairs, error = resolve_refs(prompt, characters)
    assert "10" in error, error
    pairs, error = resolve_refs(" ".join(f"@nv{index}" for index in range(10)), characters)
    assert not error and len(pairs) == 10
    _pairs, error = resolve_refs("@khongco", characters)
    assert "@khongco" in error

    memory = RefMemory(os.path.join(folder, "cache.json"), "acc")
    legend, uploads = memory.plan(pairs[:2])
    assert [item[2] for item in legend] == [True, True]
    assert len(uploads) == 2
    memory.commit("https://muse.ai/thread/abc")
    again, uploads = memory.plan(pairs[:2])
    assert uploads == []
    assert [item[2] for item in again] == [False, False]
    text = compose_prompt("@nv0 nhìn @nv1", "16:9", 15, again, "video")
    assert "[图1]" in text and "[图2]" in text and "必须直接输出成片文件" in text
    print("ok")


if __name__ == "__main__":
    main()
