"""FastAPI 应用工厂与路由。

并发语义：所有写操作在进程内同一把锁内、同一个 SQLite 事务中完成
“校验 -> 写入 -> 重算求解 -> 提交”，按提交顺序串行核算。若求解无解，
事务回滚，库内保留原有对接与年代结论（查询仍返回上一版可行结果）。
"""

from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import db, flotation
from .schemas import (
    BatchItemIn,
    CorrelationCreate,
    FlotationBatchCreate,
    SampleBagCreate,
    SiteCreate,
    StratumCreate,
    SuperpositionCreate,
)
from .service import load_and_solve, serialize_solution, serialize_stratum

MAX_STRATA_PER_SITE = 40


class ConflictError(HTTPException):
    """语义冲突（409）：约束无解或违反业务规则。"""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        content: dict[str, Any] = {"detail": message}
        if details:
            content.update(details)
        super().__init__(status_code=409, detail=content)


class _Infeasible(Exception):
    """事务内部使用：新记录使整站约束无解，触发回滚。"""


def _default_db_path() -> str:
    configured = os.environ.get("DB_PATH")
    if configured:
        return configured
    # 容器内 /data 由卷挂载；本地裸跑时回退到工作目录下的 data/
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/archaeo.db"
    return os.path.join(os.getcwd(), "data", "archaeo.db")


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or _default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    conn = db.connect(db_path)
    db.init_db(conn)
    write_lock = threading.Lock()

    app = FastAPI(
        title="考古地层对接与浮选取样 API",
        version="1.1.0",
        description=(
            "录入同一遗址（最多 40 个层位）的探方、层位编号与可能沉积年代段"
            "（整年闭区间，可含空档），登记跨探方层位对接与下层→上层叠压关系；"
            "直接或间接对接的层位视为同一次沉积并共用一个年份，自动完成分组、"
            "成环检测与可行年代段计算。\n\n"
            "浮选部分：按遗址登记土袋（袋号、出土层位、原始体积毫升）；登记由"
            "一袋或多袋组成的浮选批（各袋实际取用毫升数 + 整批籽粒总数），"
            "成功当次返回各袋余量；同一批号原样重发只记一次用量，改内容返回 409；"
            "同一袋可被多批分次取用，累计不得超过原始体积。按遗址查询各地层组"
            "（当前对接分组）的总粒数、用土毫升数与每升粒数；一批土跨多个地层组时"
            "整批不纳入密度统计，各袋取用量仍计入耗用。"
        ),
    )
    app.state.conn = conn
    app.state.write_lock = write_lock

    # ---------- 内部辅助 ----------

    def _require_site(site_id: int):
        site = db.get_site(conn, site_id)
        if site is None:
            raise HTTPException(404, f"遗址 {site_id} 不存在")
        return site

    def _require_stratum(stratum_id: int, site_id: int):
        row = db.get_stratum(conn, stratum_id)
        if row is None or row["site_id"] != site_id:
            raise HTTPException(
                404, f"层位 {stratum_id} 不属于遗址 {site_id} 或不存在"
            )
        return row

    def _solution_payload(site_id: int) -> dict[str, Any]:
        solution = load_and_solve(conn, site_id)
        return serialize_solution(solution, db.get_version(conn))

    def _contradiction_payload(site_id: int) -> dict[str, Any]:
        """读取当前连接视图（未提交的新记录）中的矛盾见证。"""
        solution = load_and_solve(conn, site_id)
        if solution.contradiction is None:  # 理论上不会发生
            return {"reason": "UNKNOWN", "message": "约束不成立", "witness": []}
        c = solution.contradiction
        return {
            "reason": c.reason,
            "message": c.message,
            "witness": [
                {"kind": w.kind, "record_id": w.record_id, "detail": w.detail}
                for w in c.witness
            ],
        }

    def _reject_infeasible(site_id: int, message: str) -> None:
        """事务内调用：先抓取新视图中的矛盾，再抛异常触发事务回滚。"""
        contradiction = _contradiction_payload(site_id)
        raise _Infeasible((message, contradiction))

    # ---------- 遗址 ----------

    @app.post("/sites", status_code=201, tags=["sites"])
    def create_site(body: SiteCreate):
        with write_lock:
            if db.get_site_by_name(conn, body.name) is not None:
                raise ConflictError(f"遗址名称 {body.name!r} 已存在")
            with db.transaction(conn):
                site = db.create_site(conn, body.name)
                db.bump_version(conn)
            return {"record": dict(site), "solution": _solution_payload(site["id"])}

    @app.get("/sites", tags=["sites"])
    def list_sites():
        return [dict(r) for r in db.list_sites(conn)]

    @app.get("/sites/{site_id}", tags=["sites"])
    def get_site(site_id: int):
        return dict(_require_site(site_id))

    # ---------- 层位 ----------

    @app.post("/sites/{site_id}/strata", status_code=201, tags=["strata"])
    def create_stratum(site_id: int, body: StratumCreate):
        _require_site(site_id)
        intervals = [(iv.year_from, iv.year_to) for iv in body.intervals]
        with write_lock:
            if db.count_strata(conn, site_id) >= MAX_STRATA_PER_SITE:
                raise ConflictError(
                    f"同一遗址最多 {MAX_STRATA_PER_SITE} 个层位，已达上限"
                )
            if db.find_stratum(conn, site_id, body.trench, body.layer_no) is not None:
                raise ConflictError(
                    f"探方 {body.trench} 的层位 {body.layer_no} 已存在"
                )
            try:
                with db.transaction(conn):
                    row = db.create_stratum(
                        conn, site_id, body.trench, body.layer_no, body.note, intervals
                    )
                    db.bump_version(conn)
                    if not load_and_solve(conn, site_id).feasible:
                        _reject_infeasible(
                            site_id, "新层位的年代段与现有约束无共同解，已拒绝录入"
                        )
            except _Infeasible as exc:
                message, contradiction = exc.args[0]
                raise ConflictError(
                    message,
                    {
                        "contradiction": contradiction,
                        "previous_solution": _solution_payload(site_id),
                    },
                )
            return {
                "record": serialize_stratum(conn, row),
                "solution": _solution_payload(site_id),
            }

    @app.get("/sites/{site_id}/strata", tags=["strata"])
    def list_strata(site_id: int):
        _require_site(site_id)
        return [serialize_stratum(conn, r) for r in db.list_strata(conn, site_id)]

    @app.get("/strata/{stratum_id}", tags=["strata"])
    def get_stratum(stratum_id: int):
        row = db.get_stratum(conn, stratum_id)
        if row is None:
            raise HTTPException(404, f"层位 {stratum_id} 不存在")
        return serialize_stratum(conn, row)

    # ---------- 对接 ----------

    @app.post("/sites/{site_id}/correlations", status_code=201, tags=["correlations"])
    def create_correlation(site_id: int, body: CorrelationCreate):
        _require_site(site_id)
        if body.stratum_a_id == body.stratum_b_id:
            raise ConflictError("对接的两个层位不能相同")
        with write_lock:
            row_a = _require_stratum(body.stratum_a_id, site_id)
            row_b = _require_stratum(body.stratum_b_id, site_id)
            if row_a["trench"] == row_b["trench"]:
                raise ConflictError(
                    "对接只能登记不同探方的层位（同一探方层位编号天然分层）"
                )
            if db.find_correlation(
                conn, body.stratum_a_id, body.stratum_b_id
            ) is not None:
                raise ConflictError("这两个层位之间的对接已存在")
            try:
                with db.transaction(conn):
                    row = db.create_correlation(
                        conn, site_id, body.stratum_a_id, body.stratum_b_id
                    )
                    db.bump_version(conn)
                    if not load_and_solve(conn, site_id).feasible:
                        _reject_infeasible(
                            site_id, "该对接会使约束无解（无法共用年份或叠压成环），已拒绝"
                        )
            except _Infeasible as exc:
                message, contradiction = exc.args[0]
                raise ConflictError(
                    message,
                    {
                        "contradiction": contradiction,
                        "previous_solution": _solution_payload(site_id),
                    },
                )
            return {"record": dict(row), "solution": _solution_payload(site_id)}

    @app.get("/sites/{site_id}/correlations", tags=["correlations"])
    def list_correlations(site_id: int):
        _require_site(site_id)
        return [dict(r) for r in db.list_correlations(conn, site_id)]

    @app.delete("/correlations/{correlation_id}", tags=["correlations"])
    def delete_correlation(correlation_id: int):
        with write_lock:
            row = db.get_correlation(conn, correlation_id)
            if row is None:
                raise HTTPException(404, f"对接 {correlation_id} 不存在")
            site_id = row["site_id"]
            with db.transaction(conn):
                db.delete_correlation(conn, correlation_id)
                db.bump_version(conn)
            # 撤销只会放松约束，按剩余对接重算必然可行
            return {"deleted": correlation_id, "solution": _solution_payload(site_id)}

    # ---------- 叠压 ----------

    @app.post("/sites/{site_id}/superpositions", status_code=201, tags=["superpositions"])
    def create_superposition(site_id: int, body: SuperpositionCreate):
        _require_site(site_id)
        if body.lower_id == body.upper_id:
            raise ConflictError("下层与上层不能是同一个层位")
        with write_lock:
            _require_stratum(body.lower_id, site_id)
            _require_stratum(body.upper_id, site_id)
            if db.find_superposition(conn, body.lower_id, body.upper_id) is not None:
                raise ConflictError("该叠压关系已存在")
            try:
                with db.transaction(conn):
                    row = db.create_superposition(
                        conn, site_id, body.lower_id, body.upper_id
                    )
                    db.bump_version(conn)
                    if not load_and_solve(conn, site_id).feasible:
                        _reject_infeasible(
                            site_id, "该叠压关系无解（成环或下层沉积年晚于上层），已拒绝"
                        )
            except _Infeasible as exc:
                message, contradiction = exc.args[0]
                raise ConflictError(
                    message,
                    {
                        "contradiction": contradiction,
                        "previous_solution": _solution_payload(site_id),
                    },
                )
            return {"record": dict(row), "solution": _solution_payload(site_id)}

    @app.get("/sites/{site_id}/superpositions", tags=["superpositions"])
    def list_superpositions(site_id: int):
        _require_site(site_id)
        return [dict(r) for r in db.list_superpositions(conn, site_id)]

    @app.delete("/superpositions/{superposition_id}", tags=["superpositions"])
    def delete_superposition(superposition_id: int):
        with write_lock:
            row = db.get_superposition(conn, superposition_id)
            if row is None:
                raise HTTPException(404, f"叠压关系 {superposition_id} 不存在")
            site_id = row["site_id"]
            with db.transaction(conn):
                db.delete_superposition(conn, superposition_id)
                db.bump_version(conn)
            return {"deleted": superposition_id, "solution": _solution_payload(site_id)}

    # ---------- 浮选取样 ----------

    def _resolve_bag_location(site_id: int, body: SampleBagCreate) -> int:
        """把 stratum_id 或 trench+layer_no 解析成层位 id，并校验属于该遗址。"""
        if body.stratum_id is not None:
            return _require_stratum(body.stratum_id, site_id)["id"]
        assert body.layer_no is not None
        if body.trench is not None:
            stratum = db.find_stratum(conn, site_id, body.trench, body.layer_no)
            if stratum is None:
                raise ConflictError(
                    f"遗址 {site_id} 中没有探方 {body.trench} 的层位 {body.layer_no}"
                    "，请先登记层位"
                )
            return stratum["id"]
        # 只给层位号：要求在该遗址内唯一
        matches = [
            r
            for r in db.list_strata(conn, site_id)
            if r["layer_no"] == body.layer_no
        ]
        if not matches:
            raise ConflictError(
                f"遗址 {site_id} 中没有层位 {body.layer_no}，请先登记层位"
            )
        if len(matches) > 1:
            trenches = "、".join(sorted(r["trench"] for r in matches))
            raise ConflictError(
                f"层位 {body.layer_no} 在多个探方（{trenches}）中都存在，"
                "请同时给出 trench 或改用 stratum_id"
            )
        return matches[0]["id"]

    def _resolve_batch_items(
        site_id: int, item_models: list[BatchItemIn]
    ) -> list[tuple[int, float]]:
        """把明细解析成 (bag_id, used_ml)，并保证同一袋只出现一次。"""
        resolved: list[tuple[int, float]] = []
        seen: set[int] = set()
        for item in item_models:
            if item.bag_id is not None:
                bag = db.get_sample_bag(conn, item.bag_id)
                if bag is None or bag["site_id"] != site_id:
                    raise HTTPException(
                        404, f"土袋 {item.bag_id} 不属于遗址 {site_id} 或不存在"
                    )
            else:
                bag_no = item.bag_no.strip()
                bag = db.find_sample_bag(conn, site_id, bag_no)
                if bag is None:
                    raise ConflictError(f"遗址 {site_id} 中不存在袋号 {bag_no!r} 的土袋")
            if bag["id"] in seen:
                raise ConflictError(f"袋号 {bag['bag_no']!r} 在本批中重复登记取用量")
            seen.add(bag["id"])
            resolved.append((bag["id"], float(item.used_ml)))
        return resolved

    def _check_capacity(
        site_id: int,
        resolved: list[tuple[int, float]],
        exclude_batch_id: int | None = None,
    ) -> None:
        """累计取用不得超过各袋原始体积（可排除本批自身，用于原样重发）。"""
        for bag_id, used_ml in resolved:
            bag = db.get_sample_bag(conn, bag_id)
            assert bag is not None
            already = db.total_used_ml(
                conn, bag_id, exclude_batch_id=exclude_batch_id
            )
            capacity = float(bag["volume_ml"])
            if already + used_ml > capacity + flotation.EPS:
                raise ConflictError(
                    f"袋号 {bag['bag_no']!r} 原始体积 {capacity:g} 毫升，"
                    f"已取用 {already:g} 毫升，本批再取 {used_ml:g} 毫升将超出",
                    {
                        "bag_no": bag["bag_no"],
                        "volume_ml": flotation.round_ml(capacity),
                        "used_ml": flotation.round_ml(already),
                        "requested_ml": flotation.round_ml(used_ml),
                        "remaining_ml": flotation.round_ml(
                            max(capacity - already, 0.0)
                        ),
                    },
                )

    @app.post("/sites/{site_id}/bags", status_code=201, tags=["flotation"])
    def create_sample_bag(site_id: int, body: SampleBagCreate):
        """按遗址登记土袋：袋号、出土层位与原始体积（毫升）。"""
        _require_site(site_id)
        with write_lock:
            stratum_id = _resolve_bag_location(site_id, body)
            if db.find_sample_bag(conn, site_id, body.bag_no) is not None:
                raise ConflictError(f"遗址 {site_id} 中袋号 {body.bag_no!r} 已存在")
            with db.transaction(conn):
                row = db.create_sample_bag(
                    conn,
                    site_id,
                    stratum_id,
                    body.bag_no,
                    float(body.volume_ml),
                    body.note,
                )
                db.bump_version(conn)
            solution = load_and_solve(conn, site_id)
            usage = flotation.usage_map(conn, [row["id"]])[row["id"]]
            return {
                "record": flotation.serialize_bag(row, usage, solution),
                "solution": serialize_solution(solution, db.get_version(conn)),
            }

    @app.get("/sites/{site_id}/bags", tags=["flotation"])
    def list_sample_bags(site_id: int):
        """遗址全部土袋：原始体积、累计取用量与当前余量。"""
        _require_site(site_id)
        with write_lock:
            solution = load_and_solve(conn, site_id)
            return flotation.serialize_bags(conn, site_id, solution)

    @app.get("/bags/{bag_id}", tags=["flotation"])
    def get_sample_bag(bag_id: int):
        row = db.get_sample_bag(conn, bag_id)
        if row is None:
            raise HTTPException(404, f"土袋 {bag_id} 不存在")
        with write_lock:
            solution = load_and_solve(conn, row["site_id"])
            usage = flotation.usage_map(conn, [bag_id])[bag_id]
            return flotation.serialize_bag(row, usage, solution)

    @app.post("/sites/{site_id}/flotations", status_code=201, tags=["flotation"])
    def create_flotation_batch(site_id: int, body: FlotationBatchCreate):
        """登记浮选批。成功当次返回所用土袋的余量；原样重发只记一次用量。"""
        _require_site(site_id)
        with write_lock:
            resolved = _resolve_batch_items(site_id, body.items)
            existing = db.find_batch(conn, site_id, body.batch_no)
            if existing is not None:
                existing_items = db.list_batch_items(conn, existing["id"])
                if not flotation.batch_content_matches(
                    existing_items,
                    resolved,
                    existing["seed_count"],
                    body.seed_count,
                ):
                    raise ConflictError(
                        f"批号 {body.batch_no!r} 已登记且内容不同，不能覆盖",
                        {
                            "batch_id": existing["id"],
                            "batch_no": existing["batch_no"],
                            "existing": flotation.serialize_batch(
                                conn, existing, load_and_solve(conn, site_id)
                            ),
                        },
                    )
                # 原样重发：幂等返回，不重复计量、不新增版本
                solution = load_and_solve(conn, site_id)
                return {
                    "record": flotation.serialize_batch(conn, existing, solution),
                    "bags": flotation.serialize_bags_by_id(
                        conn, [b for b, _ in resolved], solution
                    ),
                    "report": flotation.build_report(conn, site_id),
                    "idempotent": True,
                }

            _check_capacity(site_id, resolved)
            with db.transaction(conn):
                batch = db.create_batch(
                    conn, site_id, body.batch_no, int(body.seed_count), body.note
                )
                for bag_id, used_ml in resolved:
                    db.add_batch_item(conn, batch["id"], bag_id, used_ml)
                db.bump_version(conn)
            solution = load_and_solve(conn, site_id)
            return {
                "record": flotation.serialize_batch(conn, batch, solution),
                "bags": flotation.serialize_bags_by_id(
                    conn, [b for b, _ in resolved], solution
                ),
                "report": flotation.build_report(conn, site_id),
                "idempotent": False,
            }

    @app.get("/sites/{site_id}/flotations", tags=["flotation"])
    def list_flotation_batches(site_id: int):
        _require_site(site_id)
        with write_lock:
            solution = load_and_solve(conn, site_id)
            return [
                flotation.serialize_batch(conn, row, solution)
                for row in db.list_batches(conn, site_id)
            ]

    @app.get("/flotations/{batch_id}", tags=["flotation"])
    def get_flotation_batch(batch_id: int):
        row = db.get_batch(conn, batch_id)
        if row is None:
            raise HTTPException(404, f"浮选批 {batch_id} 不存在")
        with write_lock:
            return flotation.serialize_batch(
                conn, row, load_and_solve(conn, row["site_id"])
            )

    @app.delete("/flotations/{batch_id}", tags=["flotation"])
    def delete_flotation_batch(batch_id: int):
        """撤销浮选批：取用量退回各袋，统计按剩余记录重算。"""
        with write_lock:
            row = db.get_batch(conn, batch_id)
            if row is None:
                raise HTTPException(404, f"浮选批 {batch_id} 不存在")
            site_id = row["site_id"]
            bag_ids = [i["bag_id"] for i in db.list_batch_items(conn, batch_id)]
            with db.transaction(conn):
                db.delete_batch(conn, batch_id)
                db.bump_version(conn)
            solution = load_and_solve(conn, site_id)
            return {
                "deleted": batch_id,
                "batch_no": row["batch_no"],
                "bags": flotation.serialize_bags_by_id(conn, bag_ids, solution),
                "report": flotation.build_report(conn, site_id),
            }

    @app.get("/sites/{site_id}/flotation-report", tags=["flotation"])
    def get_flotation_report(site_id: int):
        """按遗址按地层组统计：总粒数、用土毫升数与每升粒数。"""
        _require_site(site_id)
        with write_lock:  # 分组与浮选记录读取同一已提交状态
            return flotation.build_report(conn, site_id)

    # ---------- 查询 ----------

    @app.get("/sites/{site_id}/solution", tags=["query"])
    def get_solution(site_id: int):
        """整站分组、组间叠压边与每组可同时成立的全部年代段。"""
        _require_site(site_id)
        with write_lock:  # 读也排队在写之后，保证读到的是完整已提交状态
            return _solution_payload(site_id)

    @app.get("/strata/{stratum_id}/correlation-view", tags=["query"])
    def get_stratum_correlation_view(stratum_id: int):
        """按原始层位查询：同组全部成员 + 该组可与其他层位同时成立的全部年代段。"""
        row = db.get_stratum(conn, stratum_id)
        if row is None:
            raise HTTPException(404, f"层位 {stratum_id} 不存在")
        site_id = row["site_id"]
        with write_lock:
            solution = load_and_solve(conn, site_id)
            version = db.get_version(conn)
        gid = solution.group_of[stratum_id]
        group = next(g for g in solution.groups if g.group_id == gid)
        members = [
            serialize_stratum(conn, db.get_stratum(conn, sid))
            for sid in group.member_ids
        ]
        return {
            "stratum_id": stratum_id,
            "group_id": gid,
            "version": version,
            "feasible": solution.feasible,
            "members": members,
            "group_domain": [[lo, hi] for lo, hi in group.domain],
            "feasible_years": [[lo, hi] for lo, hi in group.feasible_years],
            "earliest_year": group.earliest_year,
            "latest_year": group.latest_year,
            "contradiction": serialize_solution(solution, version)["contradiction"],
        }

    @app.exception_handler(ConflictError)
    def _conflict_handler(_request, exc: ConflictError):
        return JSONResponse(status_code=409, content=exc.detail)

    return app


app = create_app()
