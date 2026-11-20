"""浮选取样、余量、批号幂等与按地层组密度统计的接口测试。"""

import concurrent.futures

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "flotation.db"))
    with TestClient(app) as c:
        yield c


def _site(client, name="贾湖遗址"):
    r = client.post("/sites", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["record"]["id"]


def _stratum(client, site_id, trench, layer_no, years=(1, 1000)):
    r = client.post(
        f"/sites/{site_id}/strata",
        json={
            "trench": trench,
            "layer_no": layer_no,
            "intervals": [{"year_from": years[0], "year_to": years[1]}],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["record"]["id"]


def _bag(client, site_id, bag_no, volume, *, stratum_id=None, trench=None, layer=None):
    body = {"bag_no": bag_no, "volume_ml": volume}
    if stratum_id is not None:
        body["stratum_id"] = stratum_id
    else:
        body["trench"] = trench
        body["layer_no"] = layer
    r = client.post(f"/sites/{site_id}/bags", json=body)
    assert r.status_code == 201, r.text
    return r.json()["record"]


def _batch(client, site_id, batch_no, seed_count, items, expect=201):
    r = client.post(
        f"/sites/{site_id}/flotations",
        json={"batch_no": batch_no, "seed_count": seed_count, "items": items},
    )
    assert r.status_code == expect, r.text
    return r.json()


def _correlate(client, site_id, a, b):
    r = client.post(
        f"/sites/{site_id}/correlations",
        json={"stratum_a_id": a, "stratum_b_id": b},
    )
    assert r.status_code == 201, r.text


# ---------- 土袋登记 ----------


def test_bag_registration_by_site_bag_number_layer_and_volume(client):
    sid = _site(client)
    s1 = _stratum(client, sid, "T1", "L3")
    bag = _bag(client, sid, "B-01", 500, stratum_id=s1)
    assert bag["bag_no"] == "B-01"
    assert bag["stratum_id"] == s1
    assert bag["volume_ml"] == 500
    assert bag["used_ml"] == 0
    assert bag["remaining_ml"] == 500

    # 同遗址袋号唯一
    r = client.post(
        f"/sites/{sid}/bags", json={"bag_no": "B-01", "volume_ml": 100, "stratum_id": s1}
    )
    assert r.status_code == 409
    # 另一遗址可用相同袋号
    sid2 = _site(client, "跨湖桥遗址")
    s2 = _stratum(client, sid2, "T9", "L1")
    _bag(client, sid2, "B-01", 300, stratum_id=s2)
    # 出土层位也可用 trench + layer 指定
    bag2 = _bag(client, sid, "B-02", 800, trench="T1", layer="L3")
    assert bag2["stratum_id"] == s1
    # 只给层位号：遗址内唯一时可直接解析
    bag3 = _bag(client, sid, "B-05", 100, layer="L3")
    assert bag3["stratum_id"] == s1
    # 体积必须为正
    r = client.post(
        f"/sites/{sid}/bags", json={"bag_no": "B-03", "volume_ml": 0, "stratum_id": s1}
    )
    assert r.status_code == 422
    # 未登记层位不允许登记土袋
    r = client.post(
        f"/sites/{sid}/bags",
        json={"bag_no": "B-04", "volume_ml": 100, "trench": "T7", "layer_no": "L9"},
    )
    assert r.status_code == 409


# ---------- 分批取用与余量 ----------


def test_one_bag_split_across_batches_and_remaining_returned(client):
    sid = _site(client)
    s1 = _stratum(client, sid, "T1", "L3")
    bag = _bag(client, sid, "B-01", 500, stratum_id=s1)

    out = _batch(client, sid, "F-1", 30, [{"bag_no": "B-01", "used_ml": 200}])
    assert out["idempotent"] is False
    assert out["bags"][0]["used_ml"] == 200
    assert out["bags"][0]["remaining_ml"] == 300
    assert out["record"]["seed_count"] == 30
    assert out["record"]["total_used_ml"] == 200

    out2 = _batch(client, sid, "F-2", 10, [{"bag_id": bag["id"], "used_ml": 300}])
    assert out2["bags"][0]["used_ml"] == 500
    assert out2["bags"][0]["remaining_ml"] == 0

    # 余量耗尽后再取用被拒绝
    r = client.post(
        f"/sites/{sid}/flotations",
        json={
            "batch_no": "F-3",
            "seed_count": 1,
            "items": [{"bag_no": "B-01", "used_ml": 1}],
        },
    )
    assert r.status_code == 409
    body = r.json()
    assert body["remaining_ml"] == 0
    assert client.get(f"/bags/{bag['id']}").json()["used_ml"] == 500


def test_multiple_bags_combined_into_one_batch(client):
    sid = _site(client)
    s1 = _stratum(client, sid, "T1", "L3")
    _bag(client, sid, "B-01", 500, stratum_id=s1)
    _bag(client, sid, "B-02", 400, stratum_id=s1)
    _bag(client, sid, "B-03", 300, stratum_id=s1)

    out = _batch(
        client,
        sid,
        "F-1",
        88,
        [
            {"bag_no": "B-01", "used_ml": 100},
            {"bag_no": "B-02", "used_ml": 250},
            {"bag_no": "B-03", "used_ml": 50},
        ],
    )
    assert out["record"]["total_used_ml"] == 400
    assert len(out["record"]["items"]) == 3
    assert {b["bag_no"]: b["remaining_ml"] for b in out["bags"]} == {
        "B-01": 400,
        "B-02": 150,
        "B-03": 250,
    }
    # 同一袋在同一批中重复登记直接报 422
    r = client.post(
        f"/sites/{sid}/flotations",
        json={
            "batch_no": "F-2",
            "seed_count": 1,
            "items": [
                {"bag_no": "B-01", "used_ml": 10},
                {"bag_no": "B-01", "used_ml": 20},
            ],
        },
    )
    assert r.status_code == 422


def test_capacity_across_concurrent_batches(client):
    sid = _site(client)
    s1 = _stratum(client, sid, "T1", "L3")
    _bag(client, sid, "B-01", 100, stratum_id=s1)

    def fire(i):
        return client.post(
            f"/sites/{sid}/flotations",
            json={
                "batch_no": f"F-{i}",
                "seed_count": 1,
                "items": [{"bag_no": "B-01", "used_ml": 30}],
            },
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fire, range(8)))
    accepted = [r for r in results if r.status_code == 201]
    rejected = [r for r in results if r.status_code == 409]
    assert len(accepted) == 3  # 3 x 30 <= 100，第 4 批起超量
    assert len(rejected) == 5
    bag = client.get(f"/sites/{sid}/bags").json()[0]
    assert bag["used_ml"] == 90
    assert bag["remaining_ml"] == 10


# ---------- 批号幂等与冲突 ----------


def test_same_batch_resend_consumes_once(client):
    sid = _site(client)
    s1 = _stratum(client, sid, "T1", "L3")
    _bag(client, sid, "B-01", 500, stratum_id=s1)
    payload = {
        "batch_no": "F-1",
        "seed_count": 42,
        "items": [{"bag_no": "B-01", "used_ml": 200}],
    }
    first = client.post(f"/sites/{sid}/flotations", json=payload)
    assert first.status_code == 201
    version = first.json()["report"]["version"]

    again = client.post(f"/sites/{sid}/flotations", json=payload)
    assert again.status_code == 201
    assert again.json()["idempotent"] is True
    assert again.json()["record"]["id"] == first.json()["record"]["id"]
    # 明细顺序不同、仍属原样重发
    swapped = client.post(
        f"/sites/{sid}/flotations",
        json={
            "batch_no": "F-1",
            "seed_count": 42,
            "items": [{"bag_id": first.json()["record"]["items"][0]["bag_id"], "used_ml": 200}],
        },
    )
    assert swapped.status_code == 201

    bag = client.get(f"/sites/{sid}/bags").json()[0]
    assert bag["used_ml"] == 200
    assert bag["batch_count"] == 1
    # 幂等重发不产生新提交
    assert client.get(f"/sites/{sid}/flotation-report").json()["version"] == version

    # 同批号改内容 -> 409
    r = client.post(
        f"/sites/{sid}/flotations",
        json={
            "batch_no": "F-1",
            "seed_count": 43,
            "items": [{"bag_no": "B-01", "used_ml": 200}],
        },
    )
    assert r.status_code == 409
    r = client.post(
        f"/sites/{sid}/flotations",
        json={
            "batch_no": "F-1",
            "seed_count": 42,
            "items": [{"bag_no": "B-01", "used_ml": 201}],
        },
    )
    assert r.status_code == 409
    assert client.get(f"/sites/{sid}/bags").json()[0]["used_ml"] == 200


def test_failed_registration_keeps_balances_and_report(client):
    sid = _site(client)
    s1 = _stratum(client, sid, "T1", "L3")
    _bag(client, sid, "B-01", 100, stratum_id=s1)
    _batch(client, sid, "F-1", 7, [{"bag_no": "B-01", "used_ml": 60}])
    before = client.get(f"/sites/{sid}/flotation-report").json()

    r = client.post(
        f"/sites/{sid}/flotations",
        json={
            "batch_no": "F-2",
            "seed_count": 3,
            "items": [{"bag_no": "B-01", "used_ml": 60}],  # 60+60 > 100
        },
    )
    assert r.status_code == 409

    bag = client.get(f"/sites/{sid}/bags").json()[0]
    assert bag["used_ml"] == 60 and bag["remaining_ml"] == 40
    assert client.get(f"/sites/{sid}/flotation-report").json() == before
    assert len(client.get(f"/sites/{sid}/flotations").json()) == 1


# ---------- 按地层组统计 ----------


def _two_group_site(client):
    """T1L1/T2L1 对接为一组；T1L2 独立为另一组。"""
    sid = _site(client)
    a = _stratum(client, sid, "T1", "L1")
    b = _stratum(client, sid, "T2", "L1")
    c = _stratum(client, sid, "T1", "L2")
    _correlate(client, sid, a, b)
    return sid, a, b, c


def test_report_by_current_groups(client):
    sid, a, b, c = _two_group_site(client)
    _bag(client, sid, "B-A", 1000, stratum_id=a)
    _bag(client, sid, "B-B", 500, stratum_id=b)
    _bag(client, sid, "B-C", 1000, stratum_id=c)

    _batch(
        client,
        sid,
        "F-1",
        120,
        [{"bag_no": "B-A", "used_ml": 400}, {"bag_no": "B-B", "used_ml": 100}],
    )
    _batch(client, sid, "F-2", 30, [{"bag_no": "B-C", "used_ml": 500}])

    report = client.get(f"/sites/{sid}/flotation-report").json()
    assert report["feasible"] is True
    by_members = {tuple(g["stratum_ids"]): g for g in report["groups"]}
    g_ab = by_members[(a, b)]
    g_c = by_members[(c,)]
    assert g_ab["total_seed_count"] == 120
    assert g_ab["used_ml"] == 500
    assert g_ab["density_used_ml"] == 500
    assert g_ab["seeds_per_liter"] == 240.0
    assert g_ab["bag_count"] == 2

    assert g_c["total_seed_count"] == 30
    assert g_c["used_ml"] == 500
    assert g_c["seeds_per_liter"] == 60.0
    assert report["mixed_batches"] == []
    assert report["totals"]["total_seed_count"] == 150
    assert report["totals"]["used_ml"] == 1000


def test_batch_spanning_groups_excluded_from_density_but_counted_as_usage(client):
    sid, a, b, c = _two_group_site(client)
    _bag(client, sid, "B-A", 1000, stratum_id=a)
    _bag(client, sid, "B-C", 1000, stratum_id=c)

    _batch(
        client,
        sid,
        "F-1",
        99,
        [{"bag_no": "B-A", "used_ml": 300}, {"bag_no": "B-C", "used_ml": 200}],
    )
    _batch(client, sid, "F-2", 40, [{"bag_no": "B-C", "used_ml": 100}])

    report = client.get(f"/sites/{sid}/flotation-report").json()
    by_members = {tuple(g["stratum_ids"]): g for g in report["groups"]}
    g_ab, g_c = by_members[(a, b)], by_members[(c,)]

    # 整批不纳入密度统计：粒数为 0、密度的分母也不含该批用量
    assert g_ab["total_seed_count"] == 0
    assert g_ab["density_used_ml"] == 0
    assert g_ab["seeds_per_liter"] is None
    # 但各袋取用量照常计入耗用
    assert g_ab["used_ml"] == 300
    assert g_c["total_seed_count"] == 40
    assert g_c["density_used_ml"] == 100
    assert g_c["seeds_per_liter"] == 400.0
    assert g_c["used_ml"] == 300  # 200（跨组批）+ 100（整组批）

    mixed = report["mixed_batches"]
    assert len(mixed) == 1
    assert mixed[0]["batch_no"] == "F-1"
    assert mixed[0]["seed_count"] == 99
    assert sorted(mixed[0]["group_ids"]) == sorted([g_ab["group_id"], g_c["group_id"]])
    # 组视图也列出涉及的批次
    assert g_ab["mixed_batches"][0]["batch_no"] == "F-1"
    assert g_c["mixed_batches"][0]["batch_no"] == "F-1"


def test_grouping_change_applies_on_next_query_and_undo(client):
    sid = _site(client)
    a = _stratum(client, sid, "T1", "L1")
    b = _stratum(client, sid, "T2", "L1")
    c = _stratum(client, sid, "T3", "L1")
    _bag(client, sid, "B-A", 1000, stratum_id=a)
    _bag(client, sid, "B-B", 500, stratum_id=b)
    _bag(client, sid, "B-C", 900, stratum_id=c)
    _batch(client, sid, "F-1", 50, [{"bag_no": "B-A", "used_ml": 100}])
    _batch(client, sid, "F-2", 25, [{"bag_no": "B-B", "used_ml": 100}])
    _batch(client, sid, "F-3", 10, [{"bag_no": "B-C", "used_ml": 100}])

    before = client.get(f"/sites/{sid}/flotation-report").json()
    assert len(before["groups"]) == 3
    assert before["mixed_batches"] == []

    # 对接 a-b：两组合并，两个整组批现在同属一组 —— 仍整组同组
    _correlate(client, sid, a, b)
    after = client.get(f"/sites/{sid}/flotation-report").json()
    assert len(after["groups"]) == 2
    g_ab = next(g for g in after["groups"] if a in g["stratum_ids"])
    assert g_ab["stratum_ids"] == [a, b]
    assert g_ab["total_seed_count"] == 75
    assert g_ab["used_ml"] == 200
    assert g_ab["seeds_per_liter"] == 375.0

    # 再对接 c：三个层位同组，且此前各批仍在组内 -> 粒数合并
    _correlate(client, sid, b, c)
    merged = client.get(f"/sites/{sid}/flotation-report").json()
    assert len(merged["groups"]) == 1
    assert merged["groups"][0]["total_seed_count"] == 85
    assert merged["groups"][0]["used_ml"] == 300

    # 撤销一条对接：下一次查询立即按剩余对接分组
    corr_id = client.get(f"/sites/{sid}/correlations").json()[-1]["id"]
    client.delete(f"/correlations/{corr_id}")
    split = client.get(f"/sites/{sid}/flotation-report").json()
    assert len(split["groups"]) == 2


def test_batch_becomes_mixed_after_correlation_merges_groups(client):
    """两个整组批的组被对接合并后，各批仍是整组批，不会变成跨组批。"""
    sid = _site(client)
    a = _stratum(client, sid, "T1", "L1")
    b = _stratum(client, sid, "T2", "L1")
    _bag(client, sid, "B-A", 1000, stratum_id=a)
    _bag(client, sid, "B-B", 1000, stratum_id=b)
    _batch(client, sid, "F-A", 10, [{"bag_no": "B-A", "used_ml": 100}])
    _batch(client, sid, "F-B", 20, [{"bag_no": "B-B", "used_ml": 100}])
    _correlate(client, sid, a, b)
    report = client.get(f"/sites/{sid}/flotation-report").json()
    assert len(report["groups"]) == 1
    assert report["groups"][0]["total_seed_count"] == 30
    assert report["mixed_batches"] == []

    # 若把独立层位 x 的对接加入，使某个原跨组批的构成发生变化也按新分组判定
    sid2 = _site(client, "另一遗址")
    x = _stratum(client, sid2, "T1", "L1")
    z = _stratum(client, sid2, "T2", "L1")
    w = _stratum(client, sid2, "T3", "L1")
    _correlate(client, sid2, z, w)
    _bag(client, sid2, "B-1", 1000, stratum_id=x)
    _bag(client, sid2, "B-2", 1000, stratum_id=z)
    _batch(
        client,
        sid2,
        "F-1",
        5,
        [{"bag_no": "B-1", "used_ml": 100}, {"bag_no": "B-2", "used_ml": 100}],
    )
    mixed = client.get(f"/sites/{sid2}/flotation-report").json()
    assert len(mixed["mixed_batches"]) == 1
    # x 并进 z 的组之后，该批变为整组批
    _correlate(client, sid2, x, w)
    report2 = client.get(f"/sites/{sid2}/flotation-report").json()
    assert len(report2["groups"]) == 1
    assert report2["mixed_batches"] == []
    assert report2["groups"][0]["total_seed_count"] == 5


def test_delete_batch_restores_balance_and_report(client):
    sid = _site(client)
    s1 = _stratum(client, sid, "T1", "L3")
    _bag(client, sid, "B-01", 500, stratum_id=s1)
    rec = _batch(client, sid, "F-1", 42, [{"bag_no": "B-01", "used_ml": 200}])
    batch_id = rec["record"]["id"]

    deleted = client.delete(f"/flotations/{batch_id}")
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["bags"][0]["used_ml"] == 0
    assert body["bags"][0]["remaining_ml"] == 500
    assert body["report"]["groups"][0]["total_seed_count"] == 0

    # 撤销后可重新使用同批号登记不同内容
    _batch(client, sid, "F-1", 9, [{"bag_no": "B-01", "used_ml": 50}])
    assert client.get(f"/sites/{sid}/bags").json()[0]["used_ml"] == 50


def test_stats_persist_across_restart(tmp_path):
    db_path = str(tmp_path / "persist-flotation.db")
    with TestClient(create_app(db_path=db_path)) as c1:
        sid = _site(c1)
        s1 = _stratum(c1, sid, "T1", "L3")
        _bag(c1, sid, "B-01", 500, stratum_id=s1)
        _batch(c1, sid, "F-1", 20, [{"bag_no": "B-01", "used_ml": 250}])
        before = c1.get(f"/sites/{sid}/flotation-report").json()
        bag_before = c1.get(f"/sites/{sid}/bags").json()[0]

    with TestClient(create_app(db_path=db_path)) as c2:
        after = c2.get(f"/sites/{sid}/flotation-report").json()
        assert after == before
        bag_after = c2.get(f"/sites/{sid}/bags").json()[0]
        assert bag_after["used_ml"] == 250
        assert bag_after["remaining_ml"] == 250
        # 重启后余量与幂等判定仍然生效
        again = c2.post(
            f"/sites/{sid}/flotations",
            json={
                "batch_no": "F-1",
                "seed_count": 20,
                "items": [{"bag_no": "B-01", "used_ml": 250}],
            },
        )
        assert again.status_code == 201
        assert again.json()["idempotent"] is True
        assert c2.get(f"/sites/{sid}/bags").json()[0]["used_ml"] == 250
