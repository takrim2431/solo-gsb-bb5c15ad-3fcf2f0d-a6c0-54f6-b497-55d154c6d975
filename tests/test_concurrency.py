"""并发提交测试：多线程同时消耗同一批次，绝不能超用余量。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import Batch
from app.schemas import ProductionIn, RawBatchIn


def _produce_payload(idx: int, qty: str) -> ProductionIn:
    return ProductionIn(
        output_batch_no=f"FG-{idx}",
        unit="kg",
        output_quantity=Decimal(qty),
        inputs=[{"batch_no": "RM-1", "quantity": Decimal(qty)}],  # type: ignore[list-item]
    )


def test_concurrent_productions_never_overuse(engine):
    # 原料余量 100，6 个线程各消耗 15：6 个全部成功，最终余量恰为 10
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))

    results: list[str] = []
    barrier = threading.Barrier(6)

    def worker(idx: int) -> None:
        barrier.wait()  # 尽量让所有线程同时进入事务
        try:
            with session_scope() as s:
                service.submit_production(s, _produce_payload(idx, "15"))
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:  # 锁错误等也必须暴露，不能吞掉
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))

    assert results == ["ok"] * 6, results
    with session_scope() as s:
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("10.0000")


def test_concurrent_overallocation_rejects_excess(engine):
    # 余量 20，4 个线程各消耗 10（共 40）：恰好 2 成功、2 拒绝，余量归 0
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="20"))

    results: list[str] = []
    barrier = threading.Barrier(4)

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(s, _produce_payload(idx, "10"))
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(4)))

    assert results.count("ok") == 2, results
    assert results.count("INSUFFICIENT_BALANCE") == 2, results
    assert not [r for r in results if r.startswith("ERROR")], results

    with session_scope() as s:
        assert s.get(Batch, "RM-1").remaining_qty == 0
