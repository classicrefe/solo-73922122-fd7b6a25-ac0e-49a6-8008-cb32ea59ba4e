"""业务逻辑：上传、备选列、对照键计算、判定登记/撤销、配对下载。

关键约定：
- 判定（同次/异次）与键的合规关系：同次判定要求两行的键值相同，
  异次判定要求两行的键值不同；违反任一判定的键即"失效"。
- 复核变动后"更新候选"只读 projections（键值投影）与 judgments，
  不重扫 rows_a / rows_b 两张原表。
- 登记/撤销判定与"停供"在同一事务内提交；任一步失败整体回滚，
  原判定与供数状态保持不变。
"""
import csv
import hashlib
import io
import json
from datetime import datetime, timezone

from .db import transaction
from .keyfind import find_min_keys

MAX_ROWS = 20000
MAX_CANDIDATE_COLS = 10

TYP_LABELS = {"same": "同次", "diff": "异次"}


class ServiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------- 基础工具

def get_meta(conn, key, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )


def _load_rows(conn, table):
    return [
        json.loads(r["data"])
        for r in conn.execute(f"SELECT data FROM {table} ORDER BY row_no")
    ]


def _key_hash(values):
    """键值组合 -> 64 位有符号整数散列（仅存投影，不存明文）。"""
    blob = json.dumps(list(values), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(blob, digest_size=8).digest(), "little", signed=True)


# ---------------------------------------------------------------- 上传

def _parse_csv(raw, label):
    if not raw:
        raise ServiceError(f"{label}文件为空")
    text = None
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ServiceError(f"{label}编码无法识别（仅支持 UTF-8 / GB18030）")

    rows = [r for r in csv.reader(io.StringIO(text)) if r]
    if not rows:
        raise ServiceError(f"{label}没有内容")
    header = [c.strip() for c in rows[0]]
    if not header or any(not h for h in header):
        raise ServiceError(f"{label}表头存在空列名")
    if len(set(header)) != len(header):
        raise ServiceError(f"{label}表头存在重复列名")

    data = []
    for i, r in enumerate(rows[1:], start=1):
        if len(r) != len(header):
            raise ServiceError(f"{label}第 {i} 行列数（{len(r)}）与表头（{len(header)}）不一致")
        data.append(dict(zip(header, (c.strip() for c in r))))
        if len(data) > MAX_ROWS:
            raise ServiceError(f"{label}超过 {MAX_ROWS} 行上限")
    if not data:
        raise ServiceError(f"{label}没有数据行")
    return header, data


def load_session(conn, file_a, file_b):
    """上传两份同表头 CSV，重置整个对照会话。"""
    header_a, rows_a = _parse_csv(file_a.read(), "旧表")
    header_b, rows_b = _parse_csv(file_b.read(), "新表")
    if header_a != header_b:
        raise ServiceError("两份 CSV 表头不一致，无法建对照")

    with transaction(conn):
        for t in ("rows_a", "rows_b", "keys", "projections", "judgments"):
            conn.execute(f"DELETE FROM {t}")
        conn.executemany(
            "INSERT INTO rows_a(row_no, data) VALUES(?, ?)",
            [(i, json.dumps(r, ensure_ascii=False)) for i, r in enumerate(rows_a, 1)],
        )
        conn.executemany(
            "INSERT INTO rows_b(row_no, data) VALUES(?, ?)",
            [(i, json.dumps(r, ensure_ascii=False)) for i, r in enumerate(rows_b, 1)],
        )
        set_meta(conn, "header", json.dumps(header_a, ensure_ascii=False))
        set_meta(conn, "n_a", str(len(rows_a)))
        set_meta(conn, "n_b", str(len(rows_b)))
        set_meta(conn, "selected_columns", "[]")
        set_meta(conn, "current_key_id", "")
        set_meta(conn, "serving", "0")
    return header_a, len(rows_a), len(rows_b)


# ---------------------------------------------------------------- 备选列

def set_columns(conn, columns):
    header = json.loads(get_meta(conn, "header", "[]"))
    if not header:
        raise ServiceError("请先上传两份 CSV", 409)
    if not (1 <= len(columns) <= MAX_CANDIDATE_COLS):
        raise ServiceError(f"备选列须为 1~{MAX_CANDIDATE_COLS} 个")
    if len(set(columns)) != len(columns):
        raise ServiceError("备选列存在重复")
    unknown = [c for c in columns if c not in header]
    if unknown:
        raise ServiceError(f"备选列不在表头中：{'、'.join(unknown)}")

    with transaction(conn):
        set_meta(conn, "selected_columns", json.dumps(columns, ensure_ascii=False))
        # 备选列变化后，旧键与投影作废，需重新计算；判定按行号保留
        conn.execute("DELETE FROM keys")
        conn.execute("DELETE FROM projections")
        set_meta(conn, "current_key_id", "")
        set_meta(conn, "serving", "0")


# ---------------------------------------------------------------- 对照键

def compute_keys(conn):
    """计算列数最少的全部合规键，并为每个键存储键值投影。"""
    selected = json.loads(get_meta(conn, "selected_columns", "[]"))
    if not selected:
        raise ServiceError("请先勾选备选列", 409)
    rows_a = _load_rows(conn, "rows_a")
    rows_b = _load_rows(conn, "rows_b")

    found = find_min_keys(selected, rows_a, rows_b)

    with transaction(conn):
        conn.execute("DELETE FROM keys")
        conn.execute("DELETE FROM projections")
        set_meta(conn, "current_key_id", "")
        set_meta(conn, "serving", "0")
        key_ids = []
        for cols in found:
            cur = conn.execute(
                "INSERT INTO keys(cols, compatible) VALUES(?, 1)",
                (json.dumps(list(cols), ensure_ascii=False),),
            )
            kid = cur.lastrowid
            key_ids.append(kid)
            for table, ds, rows in (("rows_a", "A", rows_a), ("rows_b", "B", rows_b)):
                conn.executemany(
                    "INSERT INTO projections(key_id, ds, row_no, h) VALUES(?, ?, ?, ?)",
                    [
                        (kid, ds, i, _key_hash(tuple(r[c] for c in cols)))
                        for i, r in enumerate(rows, 1)
                    ],
                )
        # 结合既有判定（换列重算时判定保留）刷新候选合规状态
        refresh_candidates(conn)
    return key_ids, [list(c) for c in found]


def refresh_candidates(conn):
    """复核变动后更新候选键合规状态。

    只读 projections 与 judgments 两张小表，不扫描 rows_a / rows_b。
    若在用键因此失效，则在同一事务务内停供（serving=0）。
    """
    judgments = conn.execute(
        "SELECT id, row_a, row_b, typ FROM judgments"
    ).fetchall()
    keys = conn.execute("SELECT id FROM keys").fetchall()

    ra = sorted({j["row_a"] for j in judgments})
    rb = sorted({j["row_b"] for j in judgments})

    def hashes_for(key_id, ds, row_nos):
        if not row_nos:
            return {}
        marks = ",".join("?" * len(row_nos))
        return dict(
            conn.execute(
                f"SELECT row_no, h FROM projections "
                f"WHERE key_id=? AND ds=? AND row_no IN ({marks})",
                [key_id, ds, *row_nos],
            )
        )

    for k in keys:
        ok = True
        if judgments:
            ha = hashes_for(k["id"], "A", ra)
            hb = hashes_for(k["id"], "B", rb)
            for j in judgments:
                equal = ha[j["row_a"]] == hb[j["row_b"]]
                # 同次须同、异次须不同；违反任一判定即失效
                if (j["typ"] == "same") != equal:
                    ok = False
                    break
        conn.execute(
            "UPDATE keys SET compatible=? WHERE id=?",
            (1 if ok else 0, k["id"]),
        )

    current = get_meta(conn, "current_key_id", "")
    if current:
        row = conn.execute(
            "SELECT compatible FROM keys WHERE id=?", (current,)
        ).fetchone()
        if row is None or row["compatible"] == 0:
            # 在用键失效：停供旧对照（判定与本状态同事务保存）
            set_meta(conn, "serving", "0")


def _compatible_map(conn):
    return dict(conn.execute("SELECT id, compatible FROM keys"))


def select_key(conn, key_id):
    with transaction(conn):
        row = conn.execute(
            "SELECT compatible FROM keys WHERE id=?", (key_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("对照键不存在", 404)
        if row["compatible"] == 0:
            raise ServiceError("该对照键已因复核判定失效，不能选用", 409)
        set_meta(conn, "current_key_id", str(key_id))
        set_meta(conn, "serving", "1")


# ---------------------------------------------------------------- 判定（复核）

def register_judgment(conn, row_a, row_b, typ):
    if typ not in TYP_LABELS:
        raise ServiceError("判定类型须为 same（同次）或 diff（异次）")
    n_a = int(get_meta(conn, "n_a", "0"))
    n_b = int(get_meta(conn, "n_b", "0"))
    if not (1 <= row_a <= n_a):
        raise ServiceError(f"旧表行号须在 1~{n_a} 之间")
    if not (1 <= row_b <= n_b):
        raise ServiceError(f"新表行号须在 1~{n_b} 之间")

    with transaction(conn):
        dup = conn.execute(
            "SELECT id, typ FROM judgments WHERE row_a=? AND row_b=?",
            (row_a, row_b),
        ).fetchone()
        if dup:
            raise ServiceError(
                f"该对行已登记为「{TYP_LABELS[dup['typ']]}」，请先撤销再改", 409
            )
        before = _compatible_map(conn)
        conn.execute(
            "INSERT INTO judgments(row_a, row_b, typ, created_at) VALUES(?, ?, ?, ?)",
            (row_a, row_b, typ, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        refresh_candidates(conn)
        after = _compatible_map(conn)
        serving = get_meta(conn, "serving", "0") == "1"
    invalidated = [k for k in before if before[k] == 1 and after.get(k) == 0]
    restored = [k for k in after if after[k] == 1 and before.get(k) == 0]
    return {"serving": serving, "invalidated": invalidated, "restored": restored}


def revoke_judgment(conn, judgment_id):
    with transaction(conn):
        cur = conn.execute("DELETE FROM judgments WHERE id=?", (judgment_id,))
        if cur.rowcount == 0:
            raise ServiceError("判定不存在", 404)
        before = _compatible_map(conn)
        refresh_candidates(conn)
        after = _compatible_map(conn)
        serving = get_meta(conn, "serving", "0") == "1"
    invalidated = [k for k in before if before[k] == 1 and after.get(k) == 0]
    restored = [k for k in after if after[k] == 1 and before.get(k) == 0]
    return {"serving": serving, "invalidated": invalidated, "restored": restored}


# ---------------------------------------------------------------- 配对下载

def build_mapping(conn):
    """按在用键全外连接两表，返回（键列名, 对照行, 文件名）。"""
    serving = get_meta(conn, "serving", "0") == "1"
    current = get_meta(conn, "current_key_id", "")
    if not (serving and current):
        raise ServiceError("当前未供数：请先选用一个合规对照键", 409)
    row = conn.execute(
        "SELECT cols, compatible FROM keys WHERE id=?", (current,)
    ).fetchone()
    if row is None:
        raise ServiceError("在用对照键不存在，请重新计算并选键", 409)
    if row["compatible"] == 0:
        raise ServiceError("在用对照键已因复核失效并停供，请另选可用键", 409)

    cols = json.loads(row["cols"])
    rows_a = _load_rows(conn, "rows_a")
    rows_b = _load_rows(conn, "rows_b")

    map_a = {tuple(r[c] for c in cols): i for i, r in enumerate(rows_a, 1)}
    map_b = {tuple(r[c] for c in cols): i for i, r in enumerate(rows_b, 1)}

    out = []
    for k in sorted(map_a.keys() & map_b.keys(), key=lambda t: (map_a[t], t)):
        out.append((k, map_a[k], map_b[k], "配对"))
    for k in sorted(map_a.keys() - map_b.keys(), key=lambda t: (map_a[t], t)):
        out.append((k, map_a[k], "", "仅旧表"))
    for k in sorted(map_b.keys() - map_a.keys(), key=lambda t: (map_b[t], t)):
        out.append((k, "", map_b[k], "仅新表"))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return cols, out, f"mapping_{stamp}.csv"


def mapping_csv(cols, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([*cols, "旧表行号", "新表行号", "对照状态"])
    for key_vals, ra, rb, status in rows:
        w.writerow([*key_vals, ra, rb, status])
    # 加 BOM 便于 Excel 直接打开中文
    return "﻿" + buf.getvalue()


# ---------------------------------------------------------------- 状态

def get_state(conn):
    header = json.loads(get_meta(conn, "header", "[]"))
    selected = json.loads(get_meta(conn, "selected_columns", "[]"))
    current = get_meta(conn, "current_key_id", "")
    serving = get_meta(conn, "serving", "0") == "1"

    keys = []
    current_compatible = None
    for r in conn.execute("SELECT id, cols, compatible FROM keys ORDER BY id"):
        in_use = str(r["id"]) == current
        if in_use:
            current_compatible = bool(r["compatible"])
        keys.append(
            {
                "id": r["id"],
                "cols": json.loads(r["cols"]),
                "compatible": bool(r["compatible"]),
                "in_use": in_use,
            }
        )

    judgments = [
        {
            "id": r["id"],
            "row_a": r["row_a"],
            "row_b": r["row_b"],
            "typ": r["typ"],
            "typ_label": TYP_LABELS[r["typ"]],
            "created_at": r["created_at"],
        }
        for r in conn.execute(
            "SELECT id, row_a, row_b, typ, created_at FROM judgments ORDER BY id"
        )
    ]

    return {
        "header": header,
        "selected_columns": selected,
        "n_a": int(get_meta(conn, "n_a", "0")),
        "n_b": int(get_meta(conn, "n_b", "0")),
        "keys": keys,
        "judgments": judgments,
        "current_key_id": int(current) if current else None,
        "serving": serving,
        "can_download": bool(serving and current_compatible),
    }
