"""复核、污染标记与生产投料的并发测试：结果必须等同某一串行顺序。

- 隔离批次不得被投料：复核与并发投料竞争时，每个生产要么在“隔离”状态前被接受，
  要么收到 BATCH_QUARANTINED，绝无超用/脏状态；
- 并发标记与复核后，每个批次的隔离状态必须等于图上某个合法最终状态。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from sqlalchemy import text

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import Batch
from app.schemas import ProductionIn, RawBatchIn, ReviewIn, SuspectIn


def _raw(s, no: str, qty: str = "100") -> None:
    service.register_raw_batch(s, RawBatchIn(batch_no=no, unit="kg", quantity=Decimal(qty)))


def _produce(s, out: str, src: str, qty: str) -> None:
    service.submit_production(
        s,
        ProductionIn(
            output_batch_no=out,
            unit="kg",
            output_quantity=Decimal(qty),
            inputs=[{"batch_no": src, "quantity": Decimal(qty)}],
        ),
    )


def _build_graph() -> None:
    """RM-1(100) -> MIX(100)。"""
    with session_scope() as s:
        _raw(s, "RM-1")
        _produce(s, "MIX", "RM-1", "100")


def test_review_vs_productions_equivalent_to_some_serial_order():
    _build_graph()
    # 先把 RM-1 标记疑似：MIX 被隔离，此时不能投料
    with session_scope() as s:
        service.mark_suspect(s, SuspectIn(batch_no="RM-1"))

    n_producers = 8
    barrier = threading.Barrier(n_producers + 1)

    def reviewer() -> str:
        barrier.wait()
        try:
            with session_scope() as s:
                service.review_false_positive(
                    s, ReviewIn(batch_no="RM-1", note="复检合格，误报")
                )
            return "reviewed"
        except AppError as exc:  # 被并发标记抢先等可预期失败
            return exc.code
        except Exception as exc:  # 锁错误等必须暴露
            return f"ERROR:{type(exc).__name__}:{exc}"

    def producer(idx: int) -> str:
        barrier.wait()
        try:
            with session_scope() as s:
                _produce(s, f"FG-{idx}", "MIX", "10")
            return "ok"
        except AppError as exc:
            return exc.code
        except Exception as exc:
            return f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=n_producers + 1) as pool:
        futures = [pool.submit(reviewer)]
        futures += [pool.submit(producer, i) for i in range(n_producers)]
        results = [f.result() for f in futures]

    assert not [r for r in results if r.startswith("ERROR")], results
    assert results[0] == "reviewed", results
    accepted = results.count("ok")
    rejected = results.count("BATCH_QUARANTINED")
    assert accepted + rejected == n_producers, results

    with session_scope() as s:
        assert s.get(Batch, "RM-1").is_suspect is False
        # 复核最终生效：接受数与余量必须自洽，等同“先隔离后解除”这一串行顺序
        mix = s.get(Batch, "MIX")
        assert mix.remaining_qty == Decimal("100.0000") - Decimal(accepted * 10)
        assert not mix.is_quarantined
        outputs = s.execute(
            text("SELECT count(*) FROM batches WHERE batch_no LIKE 'FG-%'")
        ).scalar_one()
        assert outputs == accepted


def test_concurrent_flag_and_review_end_in_consistent_state(engine):
    """标记与复核并发：无论谁先提交，最终状态必须等于两种串行顺序之一。

    - 先标记后复核：疑似撤销、全部解除隔离；
    - 复核先到（当时未疑似）：复核以 BATCH_NOT_SUSPECT 失败，标记随后生效、整体隔离。
    """
    for _ in range(5):
        with session_scope() as s:
            s.execute(text("DELETE FROM production_inputs"))
            s.execute(text("DELETE FROM productions"))
            s.execute(text("DELETE FROM contamination_reviews"))
            s.execute(text("DELETE FROM batches"))
        _build_graph()

        barrier = threading.Barrier(2)

        def flag_worker() -> str:
            barrier.wait()
            try:
                with session_scope() as s:
                    service.mark_suspect(s, SuspectIn(batch_no="RM-1"))
                return "flagged"
            except Exception as exc:  # noqa: BLE001
                return f"ERROR:{type(exc).__name__}:{exc}"

        def review_worker() -> str:
            barrier.wait()
            try:
                with session_scope() as s:
                    service.review_false_positive(
                        s, ReviewIn(batch_no="RM-1", note="误报")
                    )
                return "reviewed"
            except AppError as exc:
                return exc.code
            except Exception as exc:  # noqa: BLE001
                return f"ERROR:{type(exc).__name__}:{exc}"

        with ThreadPoolExecutor(max_workers=2) as pool:
            rf = pool.submit(review_worker)
            ff = pool.submit(flag_worker)
            r_result, f_result = rf.result(), ff.result()

        assert not r_result.startswith("ERROR"), (r_result, f_result)
        assert not f_result.startswith("ERROR"), (r_result, f_result)
        assert f_result == "flagged", (r_result, f_result)

        with session_scope() as s:
            root = s.get(Batch, "RM-1")
            mix = s.get(Batch, "MIX")
            if r_result == "reviewed":
                # 先标记后复核：全部解除
                assert not root.is_suspect and not root.is_quarantined
                assert not mix.is_quarantined
            else:
                # 复核先到、被拒绝（批次当时未疑似）：标记生效，整体隔离
                assert r_result == "BATCH_NOT_SUSPECT", r_result
                assert root.is_suspect and root.is_quarantined
                assert mix.is_quarantined
