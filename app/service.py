"""业务逻辑层。所有写操作都在单个事务内完成，校验失败整体回滚。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from . import errors
from .models import Batch, ContaminationReview, IdempotencyKey, Production, ProductionInput
from .schemas import ProductionIn, RawBatchIn, ReviewIn, SuspectIn

# ---------------------------------------------------------------- 幂等支持


def _canonical_hash(payload: dict[str, Any]) -> str:
    """对请求体做规范序列化后取 sha256；Decimal 用原始标量文本表示。"""

    def default(obj: object) -> object:
        if isinstance(obj, Decimal):
            return f"decimal:{normalize_decimal(obj)}"
        raise TypeError(f"不可序列化的类型 {type(obj)!r}")

    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def normalize_decimal(value: Decimal) -> str:
    """1 与 1.00 视为同一内容；去掉多余尾零。"""
    return format(value.normalize(), "f")


def idempotent(
    session: Session,
    *,
    method: str,
    path: str,
    key: str | None,
    payload: dict[str, Any],
    action: Callable[[], tuple[int, Any]],
) -> tuple[int, Any]:
    """以幂等键执行动作。

    - 无 key：直接执行（查询类接口不需要）；
    - 同 key + 同内容（含重试）：返回首次固化的状态码与响应；
    - 同 key + 异内容：409 冲突；
    - 失败请求不落幂等记录，允许客户端修正后以同 key 重试。
    """
    if not key:
        status, body = action()
        return status, body

    request_hash = _canonical_hash(payload)
    stmt = select(IdempotencyKey).where(
        IdempotencyKey.method == method,
        IdempotencyKey.path == path,
        IdempotencyKey.key == key,
    )
    existing = session.execute(stmt).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise AppError_conflict(key)
        return existing.status_code, json.loads(existing.response_body)

    status, body = action()

    record = IdempotencyKey(
        method=method,
        path=path,
        key=key,
        request_hash=request_hash,
        status_code=status,
        response_body=json.dumps(body, ensure_ascii=False, default=str),
    )
    session.add(record)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        # 并发下另一请求已抢先登记该 key
        existing = session.execute(stmt).scalar_one_or_none()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise AppError_conflict(key)
            return existing.status_code, json.loads(existing.response_body)
        raise
    return status, body


def AppError_conflict(key: str) -> errors.AppError:
    return errors.AppError(
        "IDEMPOTENCY_CONFLICT",
        f"幂等键 {key!r} 已用于不同的请求内容，异内容冲突",
        409,
    )


# ---------------------------------------------------------------- 批次操作


def register_raw_batch(session: Session, data: RawBatchIn) -> Batch:
    """登记一个原料批次。编号不得复用。"""
    existing = session.get(Batch, data.batch_no)
    if existing is not None:
        raise errors.batch_exists(data.batch_no)

    batch = Batch(
        batch_no=data.batch_no,
        unit=data.unit,
        initial_qty=data.quantity,
        remaining_qty=data.quantity,
        note=data.note,
    )
    session.add(batch)
    try:
        session.flush()
    except IntegrityError:
        raise errors.batch_exists(data.batch_no) from None
    return batch


def _lock_batch(session: Session, batch_no: str) -> Batch | None:
    """读取批次并加行锁（PostgreSQL FOR UPDATE；SQLite 被写事务锁覆盖）。"""
    stmt = select(Batch).where(Batch.batch_no == batch_no)
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def submit_production(session: Session, data: ProductionIn) -> Production:
    """提交一次生产：校验全部通过后才扣减余量并创建产出批次。

    校验顺序（任一失败整笔拒绝、不改变余量）：
    1. 投入明细编号不重复；
    2. 每个来源批次存在、单位一致、未隔离；
    3. 产出编号未被占用；
    4. 每笔用量为正且不超过该批次余量；
    5. 产出总量不超过投入总量。
    """
    source_nos = [item.batch_no for item in data.inputs]
    if len(set(source_nos)) != len(source_nos):
        raise errors.AppError(
            "DUPLICATE_INPUT",
            "同一生产单中来源批次不得重复",
            422,
        )

    sources: dict[str, Batch] = {}
    total_input = Decimal("0")
    # 先一次性锁定所有来源批次（PostgreSQL 行锁；SQLite 为立即写锁）
    for item in data.inputs:
        batch = _lock_batch(session, item.batch_no)
        if batch is None:
            raise errors.batch_not_found(item.batch_no)
        sources[item.batch_no] = batch

    for item in data.inputs:
        batch = sources[item.batch_no]
        if batch.unit != data.unit:
            raise errors.unit_mismatch(item.batch_no, batch.unit, data.unit)
        if batch.is_quarantined:
            raise errors.batch_quarantined(item.batch_no)

    if session.get(Batch, data.output_batch_no) is not None:
        raise errors.batch_exists(data.output_batch_no)

    for item in data.inputs:
        batch = sources[item.batch_no]
        if item.quantity > batch.remaining_qty:
            raise errors.insufficient(
                item.batch_no, item.quantity, batch.remaining_qty
            )
        total_input += item.quantity

    if data.output_quantity > total_input:
        raise errors.AppError(
            "OUTPUT_EXCEEDS_INPUT",
            f"产出量 {normalize_decimal(data.output_quantity)} 超过投入总量 "
            f"{normalize_decimal(total_input)}",
            422,
            details={
                "output_quantity": normalize_decimal(data.output_quantity),
                "total_input": normalize_decimal(total_input),
            },
        )

    output = Batch(
        batch_no=data.output_batch_no,
        unit=data.unit,
        initial_qty=data.output_quantity,
        remaining_qty=data.output_quantity,
    )
    session.add(output)
    try:
        session.flush()  # 让产出批次主键可用，并尽早暴露编号冲突
    except IntegrityError:
        raise errors.batch_exists(data.output_batch_no) from None

    production = Production(
        output_no=output.batch_no, output_qty=data.output_quantity
    )
    session.add(production)
    session.flush()

    for item in data.inputs:
        batch = sources[item.batch_no]
        batch.remaining_qty -= item.quantity
        session.add(
            ProductionInput(
                production_id=production.id,
                source_no=batch.batch_no,
                qty=item.quantity,
            )
        )

    session.flush()
    # 重新加载聚合，便于响应组装
    return session.execute(
        select(Production)
        .options(selectinload(Production.inputs))
        .where(Production.id == production.id)
    ).scalar_one()


# ---------------------------------------------------------------- 谱系与污染

# 边方向：source -> output（原料指向产出）
_EDGE_CTE = """
SELECT pi.source_no AS parent_no, p.output_no AS child_no
FROM production_inputs pi JOIN productions p ON p.id = pi.production_id
"""


def _descendant_rows(session: Session, batch_no: str) -> list[dict[str, Any]]:
    """递归求全部后代（直接/间接使用该批次的产出），带层数。"""
    sql = f"""
    WITH RECURSIVE walk(batch_no, depth, path) AS (
        SELECT :root, 0, :root
        UNION
        SELECT e.child_no, w.depth + 1, w.path || '>' || e.child_no
        FROM walk w JOIN ({_EDGE_CTE}) e ON e.parent_no = w.batch_no
    )
    SELECT batch_no, depth FROM walk WHERE depth > 0 ORDER BY depth, batch_no
    """
    return [dict(r._mapping) for r in session.execute(text(sql), {"root": batch_no})]


def _ancestor_rows(session: Session, batch_no: str) -> list[dict[str, Any]]:
    """递归求全部祖先（直接/间接被其消耗的原料），带层数。"""
    sql = f"""
    WITH RECURSIVE walk(batch_no, depth) AS (
        SELECT :root, 0
        UNION
        SELECT e.parent_no, w.depth + 1
        FROM walk w JOIN ({_EDGE_CTE}) e ON e.child_no = w.batch_no
    )
    SELECT batch_no, depth FROM walk WHERE depth > 0 ORDER BY depth, batch_no
    """
    return [dict(r._mapping) for r in session.execute(text(sql), {"root": batch_no})]


def _load_batches_map(session: Session, nos: list[str]) -> dict[str, Batch]:
    if not nos:
        return {}
    rows = session.execute(select(Batch).where(Batch.batch_no.in_(nos))).scalars().all()
    return {b.batch_no: b for b in rows}


def lineage(session: Session, batch_no: str) -> dict[str, Any]:
    root = session.get(Batch, batch_no)
    if root is None:
        raise errors.batch_not_found(batch_no)

    up_rows = _ancestor_rows(session, batch_no)
    down_rows = _descendant_rows(session, batch_no)

    up_map = _load_batches_map(session, [r["batch_no"] for r in up_rows])
    down_map = _load_batches_map(session, [r["batch_no"] for r in down_rows])

    def node(r: dict[str, Any], b: Batch) -> dict[str, Any]:
        return {
            "batch_no": b.batch_no,
            "unit": b.unit,
            "remaining_qty": b.remaining_qty,
            "is_suspect": b.is_suspect,
            "is_quarantined": b.is_quarantined,
            "depth": r["depth"],
        }

    upstream = [node(r, up_map[r["batch_no"]]) for r in up_rows if r["batch_no"] in up_map]
    downstream = [
        node(r, down_map[r["batch_no"]]) for r in down_rows if r["batch_no"] in down_map
    ]

    involved = set(up_map) | set(down_map) | {batch_no}
    edges = session.execute(
        select(ProductionInput.source_no, Production.output_no)
        .join(Production, Production.id == ProductionInput.production_id)
        .where(
            ProductionInput.source_no.in_(involved),
            Production.output_no.in_(involved),
        )
    ).all()
    return {
        "batch_no": batch_no,
        "upstream": upstream,
        "downstream": downstream,
        "edges": [{"from": s, "to": o} for s, o in edges],
    }


def mark_suspect(session: Session, data: SuspectIn) -> dict[str, Any]:
    """标记批次疑似污染，并隔离其全部后代产出批次。

    混料规则：一个产出若混有受污染来源（直接或间接），该产出整体隔离；
    隔离批次不得再投料（提交生产时拦截）。
    """
    batch = _lock_batch(session, data.batch_no)
    if batch is None:
        raise errors.batch_not_found(data.batch_no)

    batch.is_suspect = True
    batch.is_quarantined = True
    if data.note:
        batch.note = data.note

    rows = _descendant_rows(session, data.batch_no)
    descendant_nos = [r["batch_no"] for r in rows]
    newly: list[str] = []
    if descendant_nos:
        targets = (
            session.execute(select(Batch).where(Batch.batch_no.in_(descendant_nos)))
            .scalars()
            .all()
        )
        for target in targets:
            if not target.is_quarantined:
                newly.append(target.batch_no)
            target.is_quarantined = True
    newly.sort()

    session.flush()
    return {
        "source": data.batch_no,
        "is_suspect": True,
        "affected_batch_nos": descendant_nos,
        "newly_quarantined": newly,
    }


# ---------------------------------------------------------------- 质检复核


def _lock_subtree_batches(
    session: Session, nos: list[str]
) -> dict[str, Batch]:
    """按编号排序锁定一批批次行（PG FOR UPDATE；SQLite 由写事务锁覆盖）。

    固定加锁顺序避免跨事务死锁；必须在这些行上持锁后才能重算隔离状态，
    否则与生产/标记并发时可能发生丢失更新。
    """
    if not nos:
        return {}
    ordered = sorted(set(nos))
    stmt = select(Batch).where(Batch.batch_no.in_(ordered))
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    rows = session.execute(stmt).scalars().all()
    return {b.batch_no: b for b in rows}


# 在目标集合 S（复核批次 + 其全部后代）内重算隔离：
# 自身疑似，或沿谱系上溯能到达任一疑似批次（该祖先可在 S 之外，例如混料的
# 另一疑似来源），即为隔离。
_QUARANTINE_IN_SET_SQL = f"""
WITH RECURSIVE
  seed(x) AS (
      SELECT batch_no FROM batches WHERE batch_no IN :nos
  ),
  up(x) AS (
      SELECT x FROM seed
      UNION
      SELECT e.parent_no FROM up u JOIN ({_EDGE_CTE}) e ON e.child_no = u.x
  ),
  hit(x) AS (
      SELECT u.x FROM up u JOIN batches b ON b.batch_no = u.x
      WHERE b.is_suspect = true
  ),
  down(x) AS (
      SELECT x FROM hit
      UNION
      SELECT e.child_no FROM down d JOIN ({_EDGE_CTE}) e ON e.parent_no = d.x
  )
SELECT DISTINCT d.x AS batch_no
FROM down d JOIN seed s ON s.x = d.x
"""


def _quarantined_nos_within(session: Session, nos: list[str]) -> set[str]:
    """返回集合 nos 中重算后应处于隔离状态的批次编号。"""
    if not nos:
        return set()
    stmt = text(_QUARANTINE_IN_SET_SQL).bindparams(
        bindparam("nos", value=nos, expanding=True)
    )
    return {r._mapping["batch_no"] for r in session.execute(stmt).all()}


def review_false_positive(session: Session, data: ReviewIn) -> dict[str, Any]:
    """质检复核：确认疑似批次为误报，撤销疑似标记并重算子树隔离状态。

    - 只接受当前仍被标记疑似的批次：不存在报 404，已撤销（非疑似）报 409，
      两种失败都不改变任何状态（失败也不固化幂等记录）；
    - 先撤销该批次疑似标记，再对“该批次 + 全部后代”按
      “自身或任一祖先疑似即隔离”重算；混料后代若另有疑似来源仍保持隔离；
    - 全程行锁串行化，与污染标记、生产投料的并发结果等同某一串行顺序。
    """
    root = _lock_batch(session, data.batch_no)
    if root is None:
        raise errors.batch_not_found(data.batch_no)
    if not root.is_suspect:
        raise errors.batch_not_suspect(data.batch_no)

    # 取后代集合 → 锁行 → 再取一次：与并发提交的生产收敛到稳定闭包
    # （PG 下其在 S 中的来源行也被本事务锁住，提交时等待；第二次 CTE 即可见）。
    locked: dict[str, Batch] = {root.batch_no: root}
    while True:
        descendant_rows = _descendant_rows(session, data.batch_no)
        descendant_nos = [r["batch_no"] for r in descendant_rows]
        subtree = _lock_subtree_batches(session, descendant_nos)
        new_rows = {no for no in subtree if no not in locked}
        locked.update(subtree)
        if not new_rows:
            break

    descendant_nos = sorted(descendant_nos)
    subtree_nos = [data.batch_no, *descendant_nos]

    # 撤销疑似标记后重算：root 自身的更新必须先落库（autoflush 对原生 SQL 不生效），
    # 后续 CTE 才能读到最新状态
    root.is_suspect = False
    session.flush()
    quarantined = _quarantined_nos_within(session, subtree_nos)

    released: list[str] = []
    for no in subtree_nos:
        batch = locked[no]
        should = no in quarantined
        if batch.is_quarantined and not should:
            released.append(no)
        batch.is_quarantined = should

    session.add(
        ContaminationReview(batch_no=data.batch_no, note=data.note)
    )
    session.flush()
    released.sort()

    return {
        "reviewed": {
            "batch_no": root.batch_no,
            "is_suspect": root.is_suspect,
            "is_quarantined": root.is_quarantined,
            "review_note": data.note,
        },
        "affected_descendant_nos": descendant_nos,
        "released_batch_nos": released,
    }
