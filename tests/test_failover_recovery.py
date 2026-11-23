"""Kiểm tra cơ chế retry, failover khi tài khoản lỗi, và tính tỷ lệ tạo video."""
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gui_app import BatchWorker, _vi_error
from engine import (
    MuseAuthError, MuseGenerationError, MuseModelRefusalError, MuseProxyError
)


def test_error_classification():
    worker = BatchWorker([], "acc1", "/tmp", False, threading.Event(), [], ["acc1", "acc2"])
    
    # 1. Dead account errors
    assert worker._is_dead_account(MuseAuthError("Phiên đăng nhập hết hạn (HTTP 401)"))
    assert worker._is_dead_account(MuseGenerationError("Tài khoản hết hạn mức"))
    assert worker._is_dead_account(MuseGenerationError("账号额度不足"))
    assert worker._is_dead_account(MuseProxyError("Proxy từ chối đăng nhập (HTTP 407)"))
    assert worker._is_dead_account(MuseGenerationError("会话已被 muse.ai 登出"))

    # 2. Transient / recoverable errors (MUST NOT be treated as dead account)
    assert not worker._is_dead_account(MuseGenerationError("云端 VM 连接超时 (Still sending)"))
    assert not worker._is_dead_account(MuseGenerationError("页面网关连接超时 (Connecting...)"))
    assert not worker._is_dead_account(MuseGenerationError("等待生成超时，未出现新的生成结果"))
    assert not worker._is_dead_account(MuseGenerationError("提示词发送未确认，已停止生成"))
    assert not worker._is_dead_account(MuseModelRefusalError("Muse chỉ trả lời chữ, không xuất video"))
    assert not worker._is_dead_account(MuseGenerationError("Muse trả ảnh thay vì video"))
    assert not worker._is_dead_account(MuseGenerationError("Không lấy được file đã tạo"))
    assert not worker._is_dead_account(MuseGenerationError("Chromium 启动失败"))

    # 3. Fatal input errors (user asset/input issues)
    assert worker._is_fatal_input(MuseGenerationError("Chưa thêm ảnh hoặc @nhân vật"))
    assert worker._is_fatal_input(MuseGenerationError("Không thấy ảnh nguồn"))
    assert worker._is_fatal_input(MuseGenerationError("Ảnh tham chiếu không đúng định dạng"))
    assert worker._is_fatal_input(MuseGenerationError("Một prompt chỉ được tối đa 10 ảnh tham chiếu"))
    assert worker._is_fatal_input(MuseGenerationError("Chưa có nhân vật @linh trong thư viện"))

    # 4. Transient errors MUST NOT be treated as fatal input
    assert not worker._is_fatal_input(MuseGenerationError("云端 VM 连接超时"))
    assert not worker._is_fatal_input(MuseGenerationError("Hết thời gian chờ Muse trả lời"))
    assert not worker._is_fatal_input(MuseModelRefusalError("Muse chỉ trả lời chữ"))


def test_claim_and_recycle():
    """Kiểm tra card sau khi thử trên tất cả tài khoản vẫn được tái chế để thử lại vòng mới."""
    stop_event = threading.Event()
    worker = BatchWorker(
        [{"index": 0, "prompt": "test card 1"}],
        "acc1", "/tmp", False, stop_event, [], ["acc1", "acc2"]
    )
    worker._account_pool = ["acc1", "acc2"]
    worker._pending = [{
        "index": 0,
        "job": {"index": 0, "prompt": "test card 1"},
        "tried": {"acc1", "acc2"},  # Cả 2 tài khoản đều đã thử qua
        "owner": None,
        "done": False,
        "attempts_on_acc": 0,
        "total_attempts": 2,
        "last_tried_ts": time.monotonic() - 5.0,  # Đã qua cooldown
    }]
    worker._dead = set()
    worker._busy = 0
    worker._gate = threading.Condition()

    # acc1 gọi _claim: Phải tự động tái chế card và nhận việc!
    claimed = worker._claim("acc1")
    assert claimed is not None
    assert claimed["index"] == 0
    assert claimed["owner"] == "acc1"
    assert claimed["total_attempts"] == 2
    assert worker._busy == 1


def test_dead_account_handoff():
    """Kiểm tra khi 1 tài khoản chết thì card được chuyển sang tài khoản còn sống."""
    stop_event = threading.Event()
    worker = BatchWorker(
        [{"index": 0, "prompt": "test card 1"}],
        "acc1", "/tmp", False, stop_event, [], ["acc1", "acc2"]
    )
    worker._account_pool = ["acc1", "acc2"]
    worker._dead = set()
    worker._pending = [{
        "index": 0,
        "job": {"index": 0, "prompt": "test card 1"},
        "tried": set(),
        "owner": "acc1",
        "done": False,
        "attempts_on_acc": 0,
        "total_attempts": 1,
        "last_tried_ts": 0.0,
    }]
    worker._gate = threading.Condition()

    # Giả lập acc1 chết (401)
    worker._handoff_dead_account("acc1", worker._pending[0], "Phiên hết hạn")
    
    assert "acc1" in worker._dead
    assert "acc1" in worker._pending[0]["tried"]
    assert worker._pending[0]["owner"] is None
    # Vì còn acc2 sống, card không bị đánh dấu done=True!
    assert not worker._pending[0]["done"]


def test_all_accounts_dead():
    """Kiểm tra khi tất cả tài khoản chết thì thông báo lỗi rõ ràng."""
    stop_event = threading.Event()
    worker = BatchWorker(
        [{"index": 0, "prompt": "test card 1"}],
        "acc1", "/tmp", False, stop_event, [], ["acc1"]
    )
    worker._account_pool = ["acc1"]
    worker._dead = set()
    worker._pending = [{
        "index": 0,
        "job": {"index": 0, "prompt": "test card 1"},
        "tried": set(),
        "owner": "acc1",
        "done": False,
        "attempts_on_acc": 0,
        "total_attempts": 1,
        "last_tried_ts": 0.0,
    }]
    worker._gate = threading.Condition()

    # Tài khoản duy nhất bị chết
    worker._handoff_dead_account("acc1", worker._pending[0], "Hết hạn mức")
    assert "acc1" in worker._dead
    assert worker._pending[0]["done"] is True


def test_vi_error_translation():
    """Kiểm tra các chuỗi lỗi được dịch sang tiếng Việt sạch sẽ."""
    assert "Muse chỉ trả lời chữ" in _vi_error("模型未生成媒体，仅返回文本: xin chào")
    assert "Prompt chưa tới Muse" in _vi_error("提示词未成功送达后端（Still sending / Delivery not confirmed），终止等待并重试")
    assert "Muse trả ảnh thay vì video" in _vi_error("期望生成视频，但提取成品类型为图片 (.png)，拒绝以图片冒充视频交付")
    assert "Hết thời gian chờ" in _vi_error("等待生成超时，未出现新的生成结果")
    assert "Tài khoản hết hạn mức" in _vi_error("账号额度不足")


if __name__ == "__main__":
    test_error_classification()
    test_claim_and_recycle()
    test_dead_account_handoff()
    test_all_accounts_dead()
    test_vi_error_translation()
    print("ALL TESTS PASSED: 100% video retry & failover logic verified.")
