"""浮选取样、土袋余量与按地层组汇总的密度统计。

术语约定：

- **土袋**：按遗址登记的袋号、出土层位（``stratum_id``）与原始体积（毫升）。
- **浮选批**：一个批号下可以有若干袋土，各袋登记本批实际取用的毫升数，
  并给出整批筛出的籽粒总数。同一袋可被多个批分次取用。
- **地层组**：由对接关系形成的连通组（见 ``solver``），不是原始层位。
  分组不落库，查询时与浮选记录在同一把写锁、同一已提交状态下现场重算，
  因此撤销/新增对接后下一次查询立即按新分组判定。

统计口径：

- 每组的 ``used_ml``（耗用）计入该组所有土袋的全部取用量，包含来自
  跨组批次的取用；
- 籽粒数只能归给“整批土都来自本组”的批次。跨多个组的批次不纳入任何组
  的密度统计（不贡献籽粒数，也不贡献密度的分母），只在 ``mixed_batches``
  中列出批号与涉及的组；
- ``density_used_ml`` 是纳入密度统计的用土量（仅整批同组的批次），
  ``seeds_per_liter = total_seed_count / (density_used_ml / 1000)``。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Optional

from . import db
from .solver import Solution
from .service import load_and_solve

ML_PER_LITER = 1000.0
# 毫升数使用浮点存储，比较余量时容忍极小的浮点误差
EPS = 1e-9


def round_ml(value: float) -> float:
    return round(float(value), 6)


def density_per_liter(seed_count: int, used_ml: float) -> Optional[float]:
    """每升粒数；用土量为 0 时无法计算，返回 None。"""
    if used_ml <= EPS:
        return None
    return round(seed_count / (used_ml / ML_PER_LITER), 6)


def _group_id(solution: Solution, stratum_id: int) -> int:
    gid = solution.group_of.get(stratum_id)
    if gid is None:  # 正常情况下不会发生：土袋的外键保证层位存在
        raise RuntimeError(f"层位 {stratum_id} 不在求解结果中")
    return gid


def usage_map(
    conn: sqlite3.Connection, bag_ids: Iterable[int]
) -> dict[int, dict[str, Any]]:
    unique = sorted(set(bag_ids))
    raw = db.bag_usage_rows(conn, unique)
    return {
        bid: raw.get(bid, {"used_ml": 0.0, "batch_count": 0}) for bid in unique
    }


def serialize_bag(
    row: sqlite3.Row,
    usage: Optional[dict[str, Any]] = None,
    solution: Optional[Solution] = None,
) -> dict[str, Any]:
    """土袋视图：原始体积、累计取用量与当前余量。"""
    volume = float(row["volume_ml"])
    used = float(usage["used_ml"]) if usage is not None else 0.0
    batch_count = int(usage["batch_count"]) if usage is not None else 0
    remaining = volume - used
    if -EPS <= remaining < 0:
        remaining = 0.0
    payload: dict[str, Any] = {
        "id": row["id"],
        "site_id": row["site_id"],
        "bag_no": row["bag_no"],
        "stratum_id": row["stratum_id"],
        "volume_ml": round_ml(volume),
        "used_ml": round_ml(used),
        "remaining_ml": round_ml(max(remaining, 0.0)),
        "batch_count": batch_count,
        "note": row["note"],
        "created": row["created"],
    }
    if solution is not None:
        payload["group_id"] = _group_id(solution, row["stratum_id"])
    return payload


def serialize_bags(
    conn: sqlite3.Connection,
    site_id: int,
    solution: Optional[Solution] = None,
) -> list[dict[str, Any]]:
    rows = db.list_sample_bags(conn, site_id)
    usage = usage_map(conn, [r["id"] for r in rows])
    return [serialize_bag(r, usage[r["id"]], solution) for r in rows]


def serialize_bags_by_id(
    conn: sqlite3.Connection,
    bag_ids: Iterable[int],
    solution: Optional[Solution] = None,
) -> list[dict[str, Any]]:
    """按给定顺序返回土袋视图（用于浮选批写入后的“当次余量”）。"""
    ids = list(dict.fromkeys(bag_ids))
    usage = usage_map(conn, ids)
    out: list[dict[str, Any]] = []
    for bag_id in ids:
        row = db.get_sample_bag(conn, bag_id)
        if row is not None:
            out.append(serialize_bag(row, usage[bag_id], solution))
    return out


def serialize_batch_item(
    row: sqlite3.Row, solution: Optional[Solution] = None
) -> dict[str, Any]:
    item = {
        "bag_id": row["bag_id"],
        "bag_no": row["bag_no"],
        "used_ml": round_ml(float(row["used_ml"])),
        "stratum_id": row["stratum_id"],
        "trench": row["trench"],
        "layer_no": row["layer_no"],
    }
    if solution is not None:
        item["group_id"] = _group_id(solution, row["stratum_id"])
    return item


def serialize_batch(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    solution: Optional[Solution] = None,
) -> dict[str, Any]:
    items = db.list_batch_items(conn, row["id"])
    return {
        "id": row["id"],
        "site_id": row["site_id"],
        "batch_no": row["batch_no"],
        "seed_count": int(row["seed_count"]),
        "note": row["note"],
        "created": row["created"],
        "total_used_ml": round_ml(sum(float(i["used_ml"]) for i in items)),
        "items": [serialize_batch_item(i, solution) for i in items],
    }


def batch_content_matches(
    existing_items: list[sqlite3.Row],
    requested: list[tuple[int, float]],
    existing_seed_count: int,
    seed_count: int,
) -> bool:
    """判断重发内容是否与已登记的批完全一致（与明细顺序无关）。

    只比较实质内容（各袋及取用量、籽粒总数）；备注不参与判定，避免
    重发时省略备注被误判为“改了内容”。
    """
    if int(existing_seed_count) != int(seed_count):
        return False
    left = sorted(
        (int(i["bag_id"]), round(float(i["used_ml"]), 6)) for i in existing_items
    )
    right = sorted(
        (int(bag_id), round(float(used_ml), 6)) for bag_id, used_ml in requested
    )
    return left == right


def build_report(conn: sqlite3.Connection, site_id: int) -> dict[str, Any]:
    """整站浮选报告：按当前对接分组汇总各地层组。

    分组（由原始记录现场求解）与浮选记录在同一个读事务快照内读取，
    保证二者来自同一次已提交状态。
    """
    with db.read_snapshot(conn):
        return _build_report(conn, site_id)


def _build_report(conn: sqlite3.Connection, site_id: int) -> dict[str, Any]:
    solution = load_and_solve(conn, site_id)
    version = db.get_version(conn)

    strata = {r["id"]: r for r in db.list_strata(conn, site_id)}
    bag_rows = db.list_sample_bags(conn, site_id)
    bag_by_id = {r["id"]: r for r in bag_rows}
    items = db.items_for_site(conn, site_id)

    group_ids = [g.group_id for g in solution.groups]
    acc: dict[int, dict[str, Any]] = {
        gid: {
            "group_id": gid,
            "stratum_ids": list(solution_members),
            "strata": [
                {
                    "stratum_id": sid,
                    "trench": strata[sid]["trench"],
                    "layer_no": strata[sid]["layer_no"],
                }
                for sid in solution_members
            ],
            "bag_ids": [],
            "bag_count": 0,
            "volume_ml": 0.0,
            "total_seed_count": 0,
            "used_ml": 0.0,
            "density_used_ml": 0.0,
            "seeds_per_liter": None,
            "batch_count": 0,
            "density_batch_count": 0,
            "batches": [],
            "mixed_batches": [],
        }
        for gid, solution_members in (
            (g.group_id, g.member_ids) for g in solution.groups
        )
    }

    # 土袋所属的组（袋的出土层位 -> 当前连通组）
    bag_group: dict[int, int] = {}
    for bag in bag_rows:
        gid = _group_id(solution, bag["stratum_id"])
        bag_group[bag["id"]] = gid
        acc[gid]["bag_ids"].append(bag["id"])
        acc[gid]["bag_count"] += 1
        acc[gid]["volume_ml"] += float(bag["volume_ml"])

    # 按批次汇总：一袋的用量归入其所属组；籽粒数只归给整批同组的批次
    batches: dict[int, dict[str, Any]] = {}
    for item in items:
        bag = bag_by_id.get(item["bag_id"])
        if bag is None:  # 无法归属的明细不应出现（外键约束）
            continue
        gid = bag_group[bag["id"]]
        entry = batches.setdefault(
            item["batch_id"],
            {
                "batch_id": item["batch_id"],
                "batch_no": item["batch_no"],
                "seed_count": int(item["seed_count"]),
                "group_ids": set(),
                "total_used_ml": 0.0,
                "items": [],
            },
        )
        entry["group_ids"].add(gid)
        entry["total_used_ml"] += float(item["used_ml"])
        entry["items"].append(
            {
                "bag_id": item["bag_id"],
                "bag_no": item["bag_no"],
                "used_ml": round_ml(float(item["used_ml"])),
                "stratum_id": item["stratum_id"],
                "trench": item["trench"],
                "layer_no": item["layer_no"],
                "group_id": gid,
            }
        )
        acc[gid]["used_ml"] += float(item["used_ml"])

    mixed_batches: list[dict[str, Any]] = []
    for batch_id in sorted(batches):
        entry = batches[batch_id]
        gids = sorted(entry["group_ids"])
        summary = {
            "batch_id": entry["batch_id"],
            "batch_no": entry["batch_no"],
            "seed_count": entry["seed_count"],
            "group_ids": gids,
            "total_used_ml": round_ml(entry["total_used_ml"]),
        }
        for gid in gids:
            acc[gid]["batch_count"] += 1
        if len(gids) == 1:
            gid = gids[0]
            acc[gid]["total_seed_count"] += entry["seed_count"]
            acc[gid]["density_used_ml"] += entry["total_used_ml"]
            acc[gid]["density_batch_count"] += 1
            acc[gid]["batches"].append(summary)
        else:
            mixed = dict(summary)
            mixed["groups"] = [
                {"group_id": gid, "stratum_ids": acc[gid]["stratum_ids"]}
                for gid in gids
            ]
            mixed["items"] = entry["items"]
            mixed_batches.append(mixed)
            for gid in gids:
                acc[gid]["mixed_batches"].append(
                    {
                        "batch_id": entry["batch_id"],
                        "batch_no": entry["batch_no"],
                        "group_ids": gids,
                    }
                )

    groups: list[dict[str, Any]] = []
    for gid in group_ids:
        row = acc[gid]
        row["used_ml"] = round_ml(row["used_ml"])
        row["density_used_ml"] = round_ml(row["density_used_ml"])
        row["volume_ml"] = round_ml(row["volume_ml"])
        row["seeds_per_liter"] = density_per_liter(
            row["total_seed_count"], row["density_used_ml"]
        )
        row["bag_ids"].sort()
        groups.append(row)

    total_seed_count = sum(g["total_seed_count"] for g in groups)
    total_used_ml = sum(g["used_ml"] for g in groups)
    density_used_ml = sum(g["density_used_ml"] for g in groups)
    total_volume_ml = sum(g["volume_ml"] for g in groups)

    return {
        "site_id": site_id,
        "version": version,
        "feasible": solution.feasible,
        "groups": groups,
        "mixed_batches": mixed_batches,
        "totals": {
            "total_seed_count": total_seed_count,
            "used_ml": round_ml(total_used_ml),
            "density_used_ml": round_ml(density_used_ml),
            "volume_ml": round_ml(total_volume_ml),
            "seeds_per_liter": density_per_liter(
                total_seed_count, density_used_ml
            ),
            "mixed_batch_count": len(mixed_batches),
        },
    }
