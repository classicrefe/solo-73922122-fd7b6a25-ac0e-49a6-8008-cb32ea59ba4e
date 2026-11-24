"""对照键搜索：在备选列中找出"列数最少"的全部合规键。

合规键定义：
  1. 键列在两份表的每一行都非空；
  2. 键列组合值在每份表内唯一（两表分别校验）。

"列数最少"：从 1 列开始逐级扩大，第一级出现合规键即停止，
返回该级的全部合规键（不返回更大的键，也不返回真超集）。
"""
from itertools import combinations


def _factorize(values):
    """把一列的字符串取值编码为整数，便于快速组合比较。"""
    codes = {}
    out = []
    for v in values:
        c = codes.get(v)
        if c is None:
            c = len(codes)
            codes[v] = c
        out.append(c)
    return out


def _is_unique(code_lists):
    """多列编码按行 zip 成组合值，检查是否全表唯一（提前退出）。"""
    seen = set()
    for combo in zip(*code_lists):
        if combo in seen:
            return False
        seen.add(combo)
    return True


def find_min_keys(columns, rows_a, rows_b):
    """返回列数最少的全部合规键（列名元组的列表，按列名字典序）。"""
    usable = []
    codes_a = {}
    codes_b = {}
    for c in columns:
        va = [r[c] for r in rows_a]
        vb = [r[c] for r in rows_b]
        # 键列都须非空：任一表任一行有空值，该列不能参与任何键
        if "" in va or "" in vb:
            continue
        usable.append(c)
        codes_a[c] = _factorize(va)
        codes_b[c] = _factorize(vb)

    for size in range(1, len(usable) + 1):
        found = []
        for subset in combinations(usable, size):
            ok_a = _is_unique([codes_a[c] for c in subset])
            ok_b = _is_unique([codes_b[c] for c in subset]) if ok_a else False
            if ok_a and ok_b:
                found.append(subset)
        if found:
            return found
    return []
