#!/usr/bin/env python3
"""端到端演示：选键 → 复核（判定使在用键失效）→ 换键恢复 → 下载。

用法：python3 scripts/demo.py [base_url]   默认 http://127.0.0.1:8000
仅依赖标准库。任一步断言失败即以非零码退出。
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "sample_data")

STEP = 0


def ok(msg):
    print(f"  ✓ {msg}")


def request(method, path, body=None, headers=None, expect=200):
    global STEP
    url = BASE + path
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            status, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    if status != expect:
        raise SystemExit(f"✗ {method} {path} 期望 {expect} 实得 {status}: {payload[:300]!r}")
    return payload


def api(method, path, obj=None, expect=200):
    body = json.dumps(obj).encode() if obj is not None else None
    headers = {"Content-Type": "application/json"} if obj is not None else {}
    return json.loads(request(method, path, body, headers, expect))


def upload(path_a, path_b):
    boundary = "----qc-demo-boundary"
    buf = io.BytesIO()
    for field, path in (("file_a", path_a), ("file_b", path_b)):
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{os.path.basename(path)}"\r\n'
            f"Content-Type: text/csv\r\n\r\n".encode()
        )
        buf.write(open(path, "rb").read())
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return json.loads(
        request(
            "POST",
            "/api/upload",
            buf.getvalue(),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
    )


def step(title):
    global STEP
    STEP += 1
    print(f"\n[{STEP}] {title}")


def main():
    step("上传两份同表头 CSV（各 200 行）")
    r = upload(os.path.join(SAMPLE, "old_system.csv"), os.path.join(SAMPLE, "new_system.csv"))
    assert r["rows_a"] == 200 and r["rows_b"] == 200, r
    ok(f"旧表 {r['rows_a']} 行、新表 {r['rows_b']} 行，表头 {len(r['columns'])} 列")

    step("勾选 7 个备选列（≤10）")
    api("POST", "/api/columns", {"columns": r["columns"]})
    ok("备选列已保存")

    step("计算列数最少的合规对照键")
    keys = api("POST", "/api/keys/compute")["keys"]
    by_cols = {tuple(k["cols"]): k["id"] for k in keys}
    assert set(by_cols) == {("试片编号",), ("条码号",)}, keys
    ok("候选键：" + "；".join("+".join(k["cols"]) for k in keys))

    step("选用「试片编号」键 → 开始供数")
    api("POST", "/api/keys/select", {"key_id": by_cols[("试片编号",)]})
    csv1 = request("GET", "/download.csv").decode("utf-8-sig")
    lines = csv1.strip().splitlines()
    assert len(lines) == 1 + 300, len(lines)  # 100 配对 + 100 仅旧表 + 100 仅新表
    assert "P0101,101,1,配对" in csv1 and "仅旧表" in csv1 and "仅新表" in csv1
    ok(f"下载对照表 {len(lines) - 1} 行（配对 100 / 仅旧表 100 / 仅新表 100）")

    step("复核：登记同次判定（旧表101 ↔ 新表1，P0101 两键值均相同）")
    r = api("POST", "/api/judgments", {"row_a": 101, "row_b": 1, "type": "same"})
    assert r["serving"] and not r["invalidated"], r
    request("GET", "/download.csv")
    ok("在用键仍合规，继续供数")

    step("复核：登记异次判定（旧表150 ↔ 新表50，P0150 但条码不同）")
    r = api("POST", "/api/judgments", {"row_a": 150, "row_b": 50, "type": "diff"})
    assert not r["serving"] and by_cols[("试片编号",)] in r["invalidated"], r
    ok("「试片编号」键失效，保存判定的同时已停供")

    step("停供期间下载被拒（409）")
    msg = request("GET", "/download.csv", expect=409).decode()
    ok(f"409：{msg.strip()}")

    step("撤销异次判定：键恢复合规，但供数不自动恢复（须重新选键）")
    state = api("GET", "/api/state")
    diff_id = next(j["id"] for j in state["judgments"] if j["typ"] == "diff")
    api("POST", "/api/judgments/revoke", {"id": diff_id})
    state = api("GET", "/api/state")
    k_piece = next(k for k in state["keys"] if k["cols"] == ["试片编号"])
    assert k_piece["compatible"] and not state["serving"]
    request("GET", "/download.csv", expect=409)
    ok("键已合规但仍在停供，下载仍 409")

    step("重新登记该异次判定，并另选合规键「条码号」→ 恢复供数")
    api("POST", "/api/judgments", {"row_a": 150, "row_b": 50, "type": "diff"})
    api("POST", "/api/keys/select", {"key_id": by_cols[("条码号",)]})
    csv2 = request("GET", "/download.csv").decode("utf-8-sig")
    assert "T-0150,150,,仅旧表" in csv2 and "T-9150,,50,仅新表" in csv2
    ok("P0150 按判定拆为两条单边记录，对照表恢复下载")

    print("\n全部演示步骤通过 ✅")


if __name__ == "__main__":
    main()
