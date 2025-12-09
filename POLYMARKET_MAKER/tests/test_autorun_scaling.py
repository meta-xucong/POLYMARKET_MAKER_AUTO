import math
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 在导入 poly_maker_autorun 之前打桩 requests，避免缺失依赖导致提前退出。
if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace()

from poly_maker_autorun import _scale_order_size_by_volume


def test_scale_order_size_high_volume_is_dampened():
    size = _scale_order_size_by_volume(
        base_size=10,
        total_volume=3_000_000,
        base_volume=10_000,
    )
    assert 10 < size < 30
    assert math.isclose(size, 19.51, rel_tol=1e-3)


def test_scale_order_size_near_base_volume_is_gentle():
    size = _scale_order_size_by_volume(
        base_size=10,
        total_volume=12_000,
        base_volume=10_000,
    )
    assert 10 < size < 12
    assert size > 10.8
