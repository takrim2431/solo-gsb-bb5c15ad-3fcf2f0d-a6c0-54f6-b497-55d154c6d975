"""质检复核（撤销误报）端到端测试：撤销疑似、子树隔离重算、混料来源、幂等、查询同步。"""
from __future__ import annotations


def _raw(client, batch_no, qty, unit="kg", key=None, note=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": qty, "note": note},
        headers={"Idempotency-Key": key or f"key-{batch_no}"},
    )


def _produce(client, out_no, inputs, out_qty=None, unit="kg", key=None):
    from decimal import Decimal

    total = sum((Decimal(str(q)) for _, q in inputs), Decimal(0))
    return client.post(
        "/productions",
        json={
            "output_batch_no": out_no,
            "unit": unit,
            "output_quantity": str(out_qty if out_qty is not None else total),
            "inputs": [{"batch_no": b, "quantity": str(q)} for b, q in inputs],
        },
        headers={"Idempotency-Key": key or f"key-{out_no}"},
    )


def _flag(client, batch_no, key, note=None):
    return client.post(
        "/contamination",
        json={"batch_no": batch_no, "note": note},
        headers={"Idempotency-Key": key},
    )


def _review(client, batch_no, note, key):
    return client.post(
        "/reviews",
        json={"batch_no": batch_no, "note": note},
        headers={"Idempotency-Key": key},
    )


def _states(client, nos):
    out = {}
    for no in nos:
        b = client.get(f"/batches/{no}").json()
        out[no] = (b["is_suspect"], b["is_quarantined"])
    return out


def _build_mixed_chain(client):
    """RM-A + RM-B -> MIX-1 -> FG-1；另有 RM-C -> CLEAN-1（与 A 无关）。"""
    _raw(client, "RM-A", "100")
    _raw(client, "RM-B", "100")
    _raw(client, "RM-C", "100")
    _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "60")], key="p1")
    _produce(client, "FG-1", [("MIX-1", "50")], key="p2")
    _produce(client, "CLEAN-1", [("RM-C", "10")], key="p3")


def test_review_releases_self_and_all_descendants(client):
    _build_mixed_chain(client)
    _flag(client, "RM-A", "f1")
    assert _states(client, ["RM-A", "MIX-1", "FG-1"]) == {
        "RM-A": (True, True),
        "MIX-1": (False, True),
        "FG-1": (False, True),
    }

    r = _review(client, "RM-A", "复检合格，农残未超标", "rv1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reviewed"] == {
        "batch_no": "RM-A",
        "is_suspect": False,
        "is_quarantined": False,
        "review_note": "复检合格，农残未超标",
    }
    # 涉及的全部后代
    assert body["affected_descendant_nos"] == ["FG-1", "MIX-1"]
    # 本次解除隔离：复核批次自身 + 两个后代
    assert body["released_batch_nos"] == ["FG-1", "MIX-1", "RM-A"]

    assert _states(client, ["RM-A", "MIX-1", "FG-1", "RM-B", "RM-C", "CLEAN-1"]) == {
        "RM-A": (False, False),
        "MIX-1": (False, False),
        "FG-1": (False, False),
        "RM-B": (False, False),
        "RM-C": (False, False),
        "CLEAN-1": (False, False),
    }


def test_review_mixed_descendant_kept_by_other_suspect_source(client):
    """混料后代不得因一个来源解除而忽略另一疑似来源。"""
    _build_mixed_chain(client)
    _flag(client, "RM-A", "f1")
    _flag(client, "RM-B", "f2")

    r = _review(client, "RM-A", "RM-A 复检合格", "rv1")
    assert r.status_code == 200, r.text
    # 仅 RM-A 自身解除；MIX-1/FG-1 仍因 RM-B 隔离
    assert r.json()["released_batch_nos"] == ["RM-A"]
    assert _states(client, ["RM-A", "RM-B", "MIX-1", "FG-1", "CLEAN-1"]) == {
        "RM-A": (False, False),
        "RM-B": (True, True),
        "MIX-1": (False, True),
        "FG-1": (False, True),
        "CLEAN-1": (False, False),
    }

    # 另一来源也解除后，混料后代才整体解除
    r2 = _review(client, "RM-B", "RM-B 复检合格", "rv2")
    assert set(r2.json()["released_batch_nos"]) == {"RM-B", "MIX-1", "FG-1"}
    assert client.get("/batches/MIX-1").json()["is_quarantined"] is False
    assert client.get("/batches/FG-1").json()["is_quarantined"] is False


def test_review_intermediate_batch_with_suspect_ancestor_stays_quarantined(client):
    """复核中间批次：自身疑似撤销，但若其祖先仍疑似，它与后代继续隔离。"""
    _build_mixed_chain(client)
    _flag(client, "RM-A", "f1")
    _flag(client, "MIX-1", "f2")

    r = _review(client, "MIX-1", "MIX-1 抽检复测合格", "rv1")
    assert r.status_code == 200, r.text
    # MIX-1 疑似已撤销，但仍被祖先 RM-A 连带隔离；无批次本次解除
    assert r.json()["released_batch_nos"] == []
    assert _states(client, ["RM-A", "MIX-1", "FG-1"]) == {
        "RM-A": (True, True),
        "MIX-1": (False, True),
        "FG-1": (False, True),
    }

    # 祖先解除后全部解除
    r2 = _review(client, "RM-A", "RM-A 误报", "rv2")
    assert set(r2.json()["released_batch_nos"]) == {"RM-A", "MIX-1", "FG-1"}


def test_review_nonexistent_batch_404_without_change(client):
    r = _review(client, "GHOST", "复核说明", "rv1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_review_not_suspect_batch_conflicts_without_change(client):
    _build_mixed_chain(client)
    _flag(client, "RM-A", "f1")
    _review(client, "RM-A", "误报", "rv1")

    # 已撤销：重复复核明确报错
    r = _review(client, "RM-A", "再次复核", "rv2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_NOT_SUSPECT"

    # 从未标记过同样拒绝
    r = _review(client, "RM-B", "从未疑似", "rv3")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_NOT_SUSPECT"
    # 状态不被改变
    assert client.get("/batches/RM-B").json()["is_suspect"] is False


def test_review_blank_note_rejected(client):
    _build_mixed_chain(client)
    _flag(client, "RM-A", "f1")
    for note in ("", "   "):
        r = _review(client, "RM-A", note, f"rv-{note!r}")
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 失败不改变疑似/隔离状态
    assert client.get("/batches/RM-A").json()["is_suspect"] is True


def test_review_requires_idempotency_key(client):
    r = client.post("/reviews", json={"batch_no": "RM-A", "note": "误报"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_review_idempotent_replay_returns_first_result(client):
    _build_mixed_chain(client)
    _flag(client, "RM-A", "f1")
    payload = {"batch_no": "RM-A", "note": "复检合格"}

    r1 = client.post("/reviews", json=payload, headers={"Idempotency-Key": "RV1"})
    assert r1.status_code == 200
    # 同键同内容重试（此时批次已非疑似）：仍返回首次结果，不报错、不重复落审计记录
    r2 = client.post("/reviews", json=payload, headers={"Idempotency-Key": "RV1"})
    assert r2.status_code == 200
    assert r2.json() == r1.json()

    # 只落了一条复核记录
    from app import db
    from app.models import ContaminationReview

    with db.session_scope() as s:
        assert s.query(ContaminationReview).count() == 1
        assert s.get(ContaminationReview, 1).note == "复检合格"


def test_review_same_key_different_content_conflicts(client):
    _build_mixed_chain(client)
    _flag(client, "RM-A", "f1")
    _flag(client, "RM-B", "f2")

    assert _review(client, "RM-A", "说明一", "K9").status_code == 200
    r = _review(client, "RM-B", "说明二", "K9")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 冲突请求无副作用：RM-B 仍疑似隔离
    assert client.get("/batches/RM-B").json()["is_suspect"] is True


def test_failed_review_not_stored_same_key_retryable(client):
    _build_mixed_chain(client)
    payload = {"batch_no": "RM-A", "note": "误报"}
    # RM-A 尚未疑似：失败，不固化幂等记录
    r1 = client.post("/reviews", json=payload, headers={"Idempotency-Key": "K1"})
    assert r1.status_code == 409
    # 之后 RM-A 被标记疑似
    _flag(client, "RM-A", "f1")
    # 同键同内容重试：成功
    r2 = client.post("/reviews", json=payload, headers={"Idempotency-Key": "K1"})
    assert r2.status_code == 200, r2.text
    assert client.get("/batches/RM-A").json()["is_suspect"] is False


def test_batch_query_and_lineage_reflect_review(client):
    _build_mixed_chain(client)
    _flag(client, "RM-A", "f1")
    _review(client, "RM-A", "误报", "rv1")

    # 批次查询同步
    assert client.get("/batches/RM-A").json()["is_quarantined"] is False
    assert client.get("/batches/MIX-1").json()["is_quarantined"] is False

    # 谱系结果同步
    lin = client.get("/batches/RM-A/lineage").json()
    assert all(n["is_quarantined"] is False and n["is_suspect"] is False
               for n in lin["downstream"])

    up = client.get("/batches/FG-1/lineage").json()
    assert all(n["is_quarantined"] is False for n in up["upstream"])


def test_released_batch_can_be_consumed_again(client):
    _build_mixed_chain(client)
    _flag(client, "RM-A", "f1")
    # 隔离中投料被拒
    r = _produce(client, "FG-2", [("MIX-1", "5")], key="p4")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"

    _review(client, "RM-A", "误报", "rv1")
    # 解除后可正常投料，且余量只被扣一次（MIX-1 此前余 50，耗 5 后余 45）
    r = _produce(client, "FG-2", [("MIX-1", "5")], key="p4")
    assert r.status_code == 201, r.text
    assert client.get("/batches/MIX-1").json()["remaining_qty"] == 45.0
    assert client.get("/batches/FG-2").json()["remaining_qty"] == 5.0


def test_review_no_descendants_releases_only_self(client):
    _raw(client, "RM-ONLY", "10")
    _flag(client, "RM-ONLY", "f1")
    r = _review(client, "RM-ONLY", "误报", "rv1")
    assert r.status_code == 200
    assert r.json()["affected_descendant_nos"] == []
    assert r.json()["released_batch_nos"] == ["RM-ONLY"]
