#!/usr/bin/env python3
"""生成演示用台账：sample_data/old_system.csv 与 new_system.csv。

数据设计（配合 README 的演示流程）：
- 试片编号、条码号：两表内均唯一且非空 → 两个单列合规键；
- 窑炉/班次/烧成日期/检验员：大量重复 → 不能单独成键；
- 备注：部分为空 → 因"键列都须非空"被排除；
- 旧表 P0001..P0200；新表 P0101..P0280 + P9001..P9020
  → 配对 100 对、仅旧表 100 行、仅新表 100 行；
- 冲突对：试片编号 P0150 在新表条码为 T-9150（旧表为 T-0150），
  用于演示"异次判定使 试片编号 键失效、改选 条码号 键恢复供数"。
  对应行号：旧表第 150 行，新表第 50 行。
- 同次对：P0101（旧表第 101 行，新表第 1 行，条码同为 T-0101）。
"""
import csv
import os

HEADER = ["试片编号", "条码号", "窑炉", "班次", "烧成日期", "检验员", "备注"]
KILNS = ["1号窑", "2号窑", "3号窑"]
SHIFTS = ["甲班", "乙班", "丙班"]
INSPECTORS = ["王芳", "李强", "赵敏"]


def row(piece, barcode, i):
    return [
        piece,
        barcode,
        KILNS[i % len(KILNS)],
        SHIFTS[i % len(SHIFTS)],
        f"2026-01-{i % 28 + 1:02d}",
        INSPECTORS[i % len(INSPECTORS)],
        "" if i % 5 == 0 else "复测",  # 每 5 行一个空备注
    ]


def main():
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_data")
    os.makedirs(out_dir, exist_ok=True)

    old_rows = [row(f"P{i:04d}", f"T-{i:04d}", i) for i in range(1, 201)]

    new_rows = []
    for i in range(101, 281):  # P0101..P0280
        barcode = "T-9150" if i == 150 else f"T-{i:04d}"  # 冲突对：P0150 换码
        new_rows.append(row(f"P{i:04d}", barcode, i))
    for i in range(1, 21):  # P9001..P9020 仅新表
        new_rows.append(row(f"P{9000 + i:04d}", f"T-{9000 + i:04d}", i))

    for name, rows in (("old_system.csv", old_rows), ("new_system.csv", new_rows)):
        path = os.path.join(out_dir, name)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(rows)
        print(f"已生成 {path}（{len(rows)} 行）")


if __name__ == "__main__":
    main()
