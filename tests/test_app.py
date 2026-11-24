"""关键操作自测：上传校验、键计算、判定/复核、停供恢复、回滚、不重扫原表。"""
import io
import json
import sqlite3

import pytest

from app import services
from app.keyfind import find_min_keys
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        {
            "DATABASE": str(tmp_path / "t.db"),
            "SECRET_KEY": "t",
            "TESTING": True,
            "PROPAGATE_EXCEPTIONS": False,  # 与线上一致：异常返回 500 而非抛出
        }
    )
    return app.test_client()


# ---------------------------------------------------------------- 工具

def csv_bytes(header, rows):
    lines = [",".join(header)] + [",".join(map(str, r)) for r in rows]
    return "\n".join(lines).encode("utf-8")


def upload(client, a, b):
    return client.post(
        "/api/upload",
        data={
            "file_a": (io.BytesIO(a), "a.csv"),
            "file_b": (io.BytesIO(b), "b.csv"),
        },
        content_type="multipart/form-data",
    )


def make_pair():
    """两表均有单列键 idc、bc；note 含空值（应被排除）。

    冲突对：旧表第2行 P2/T2，新表第2行 P2/T9 —— idc 相同、bc 不同。
    """
    header = ["idc", "bc", "note"]
    a = [["P1", "T1", "x"], ["P2", "T2", "x"], ["P3", "T3", ""]]
    b = [["P1", "T1", "x"], ["P2", "T9", "x"], ["P4", "T4", "x"]]
    return csv_bytes(header, a), csv_bytes(header, b)


def setup_pair(client):
    a, b = make_pair()
    assert upload(client, a, b).status_code == 200
    assert client.post("/api/columns", json={"columns": ["idc", "bc", "note"]}).status_code == 200
    keys = client.post("/api/keys/compute").get_json()["keys"]
    return {tuple(k["cols"]): k["id"] for k in keys}


def state(client):
    return client.get("/api/state").get_json()


# ---------------------------------------------------------------- 上传

def test_upload_ok(client):
    a, b = make_pair()
    r = upload(client, a, b)
    assert r.status_code == 200
    body = r.get_json()
    assert body["rows_a"] == 3 and body["rows_b"] == 3
    assert body["columns"] == ["idc", "bc", "note"]


def test_upload_header_mismatch(client):
    a = csv_bytes(["x", "y"], [["1", "2"]])
    b = csv_bytes(["x", "z"], [["1", "2"]])
    r = upload(client, a, b)
    assert r.status_code == 400
    assert "表头不一致" in r.get_json()["error"]


def test_upload_row_limit(client):
    header = ["u"]
    a = csv_bytes(header, [[f"a{i}"] for i in range(20001)])
    b = csv_bytes(header, [["b1"]])
    r = upload(client, a, b)
    assert r.status_code == 400
    assert "20000" in r.get_json()["error"]


def test_upload_bad_row_width(client):
    a = csv_bytes(["x", "y"], [["1", "2"], ["3"]])
    b = csv_bytes(["x", "y"], [["1", "2"]])
    assert upload(client, a, b).status_code == 400


# ---------------------------------------------------------------- 备选列

def test_columns_validation(client):
    a, b = make_pair()
    upload(client, a, b)
    assert client.post("/api/columns", json={"columns": []}).status_code == 400
    assert (
        client.post("/api/columns", json={"columns": [f"c{i}" for i in range(11)]}).status_code
        == 400
    )
    assert client.post("/api/columns", json={"columns": ["idc", "idc"]}).status_code == 400
    assert client.post("/api/columns", json={"columns": ["nope"]}).status_code == 400
    assert client.post("/api/columns", json={"columns": ["idc", "bc"]}).status_code == 200


# ---------------------------------------------------------------- 键计算

def test_find_min_keys_unit():
    rows_a = [{"u": "1", "e": "", "x": "p"}, {"u": "2", "e": "v", "x": "p"}]
    rows_b = [{"u": "a", "e": "v", "x": "q"}, {"u": "b", "e": "v", "x": "q"}]
    # e 含空值被排除；x 两表均重复；u 唯一 → 唯一的最小键
    assert find_min_keys(["u", "e", "x"], rows_a, rows_b) == [("u",)]


def test_compute_min_keys(client):
    keys = setup_pair(client)
    assert set(keys) == {("idc",), ("bc",)}  # note 含空值被排除


def test_compute_size2_when_no_single(client):
    header = ["x", "y"]
    a = csv_bytes(header, [["p", "1"], ["p", "2"], ["q", "1"]])
    b = csv_bytes(header, [["m", "1"], ["m", "2"], ["n", "1"]])
    assert upload(client, a, b).status_code == 200
    client.post("/api/columns", json={"columns": ["x", "y"]})
    keys = client.post("/api/keys/compute").get_json()["keys"]
    assert [k["cols"] for k in keys] == [["x", "y"]]


def test_compute_no_valid_key(client):
    header = ["x"]
    a = csv_bytes(header, [["p"], ["p"]])
    b = csv_bytes(header, [["m"]])
    upload(client, a, b)
    client.post("/api/columns", json={"columns": ["x"]})
    keys = client.post("/api/keys/compute").get_json()["keys"]
    assert keys == []


# ---------------------------------------------------------------- 选键与下载

def test_download_requires_serving(client):
    setup_pair(client)
    assert client.get("/download.csv").status_code == 409


def test_select_and_download(client):
    keys = setup_pair(client)
    assert client.post("/api/keys/select", json={"key_id": keys[("idc",)]}).status_code == 200
    r = client.get("/download.csv")
    assert r.status_code == 200
    assert "attachment" in r.headers["Content-Disposition"]
    body = r.get_data(as_text=True)
    assert "P1,1,1,配对" in body
    assert "P2,2,2,配对" in body
    assert "P3,3,,仅旧表" in body
    assert "P4,,3,仅新表" in body


def test_select_incompatible_key_rejected(client):
    keys = setup_pair(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)]})
    # 异次判定使 idc 键失效
    client.post("/api/judgments", json={"row_a": 2, "row_b": 2, "type": "diff"})
    r = client.post("/api/keys/select", json={"key_id": keys[("idc",)]})
    assert r.status_code == 409


# ---------------------------------------------------------------- 判定 / 复核

def test_judgment_flow_full(client):
    keys = setup_pair(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)]})

    # 同次判定（两键值均相同）：不失效，继续供数
    r = client.post("/api/judgments", json={"row_a": 1, "row_b": 1, "type": "same"}).get_json()
    assert r["serving"] and r["invalidated"] == []
    assert client.get("/download.csv").status_code == 200

    # 异次判定落在 idc 相同的行对上：idc 键失效并停供
    r = client.post("/api/judgments", json={"row_a": 2, "row_b": 2, "type": "diff"}).get_json()
    assert not r["serving"] and r["invalidated"] == [keys[("idc",)]]
    assert client.get("/download.csv").status_code == 409

    st = state(client)
    k = {tuple(x["cols"]): x["compatible"] for x in st["keys"]}
    assert k == {("idc",): False, ("bc",): True}

    # 另选 bc 键恢复供数；冲突对按判定拆成两条单边
    client.post("/api/keys/select", json={"key_id": keys[("bc",)]})
    body = client.get("/download.csv").get_data(as_text=True)
    assert "T2,2,,仅旧表" in body and "T9,,2,仅新表" in body

    # 撤销异次判定：idc 恢复合规；在用 bc 键仍合规 → 继续供数
    diff_id = next(j["id"] for j in state(client)["judgments"] if j["typ"] == "diff")
    r = client.post("/api/judgments/revoke", json={"id": diff_id}).get_json()
    assert keys[("idc",)] in r["restored"]
    st = state(client)
    assert st["serving"] is True
    assert client.get("/download.csv").status_code == 200

    # 重新选用 idc 键亦可
    client.post("/api/keys/select", json={"key_id": keys[("idc",)]})
    assert client.get("/download.csv").status_code == 200


def test_no_auto_resume_after_key_restored(client):
    """在用键失效停供后，即使撤销判定使其恢复合规，也须重新选用才恢复下载。"""
    keys = setup_pair(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)]})
    client.post("/api/judgments", json={"row_a": 2, "row_b": 2, "type": "diff"})
    assert state(client)["serving"] is False

    diff_id = state(client)["judgments"][0]["id"]
    client.post("/api/judgments/revoke", json={"id": diff_id})
    st = state(client)
    k = {tuple(x["cols"]): x["compatible"] for x in st["keys"]}
    assert k[("idc",)] is True            # 键已恢复合规
    assert st["serving"] is False          # 但供数不自动恢复
    assert client.get("/download.csv").status_code == 409

    client.post("/api/keys/select", json={"key_id": keys[("idc",)]})
    assert client.get("/download.csv").status_code == 200


def test_judgment_validation(client):
    setup_pair(client)
    assert (
        client.post("/api/judgments", json={"row_a": 99, "row_b": 1, "type": "same"}).status_code
        == 400
    )
    assert (
        client.post("/api/judgments", json={"row_a": 1, "row_b": 1, "type": "bad"}).status_code
        == 400
    )
    assert (
        client.post("/api/judgments", json={"row_a": 1, "row_b": 1, "type": "same"}).status_code
        == 200
    )
    # 同一对行重复登记（含异次冲突）须先撤销
    assert (
        client.post("/api/judgments", json={"row_a": 1, "row_b": 1, "type": "same"}).status_code
        == 409
    )
    assert (
        client.post("/api/judgments", json={"row_a": 1, "row_b": 1, "type": "diff"}).status_code
        == 409
    )


def test_revoke_unknown_404(client):
    setup_pair(client)
    assert client.post("/api/judgments/revoke", json={"id": 42}).status_code == 404


# ---------------------------------------------------------------- 事务回滚

def test_save_failure_rolls_back(client, monkeypatch):
    keys = setup_pair(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)]})
    client.post("/api/judgments", json={"row_a": 1, "row_b": 1, "type": "same"})

    def boom(conn):
        raise RuntimeError("模拟保存失败")

    monkeypatch.setattr(services, "refresh_candidates", boom)
    r = client.post("/api/judgments", json={"row_a": 2, "row_b": 2, "type": "diff"})
    assert r.status_code == 500

    # 原判定与供数状态保持不变
    st = state(client)
    assert len(st["judgments"]) == 1
    assert st["judgments"][0]["typ"] == "same"
    assert st["serving"] is True and st["can_download"] is True
    assert client.get("/download.csv").status_code == 200


# ---------------------------------------------------------------- 不重扫原表

def test_refresh_without_rescanning_sources(client, tmp_path):
    keys = setup_pair(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)]})

    # 直接清空两张原表：复核更新只能依赖键值投影
    db_path = None
    with client.application.app_context():
        db_path = client.application.config["DATABASE"]
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM rows_a")
    conn.execute("DELETE FROM rows_b")
    conn.commit()
    conn.close()

    # 登记/撤销判定与候选更新照常工作（行号上限来自 meta，不读原表）
    r = client.post("/api/judgments", json={"row_a": 2, "row_b": 2, "type": "diff"})
    assert r.status_code == 200
    assert r.get_json()["invalidated"] == [keys[("idc",)]]
    st = state(client)
    k = {tuple(x["cols"]): x["compatible"] for x in st["keys"]}
    assert k == {("idc",): False, ("bc",): True}

    jid = st["judgments"][0]["id"]
    r = client.post("/api/judgments/revoke", json={"id": jid})
    assert r.status_code == 200
    k = {tuple(x["cols"]): x["compatible"] for x in state(client)["keys"]}
    assert k == {("idc",): True, ("bc",): True}


# ---------------------------------------------------------------- 会话重置

def test_upload_resets_session(client):
    keys = setup_pair(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)]})
    client.post("/api/judgments", json={"row_a": 1, "row_b": 1, "type": "same"})
    a, b = make_pair()
    assert upload(client, a, b).status_code == 200
    st = state(client)
    assert st["keys"] == [] and st["judgments"] == []
    assert st["serving"] is False and st["current_key_id"] is None
