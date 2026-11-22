"""金饰活动话术与预留有效性 —— 一日模拟。

用法：
    python3 main.py              读取 data/ 三份资料，打印时间线与交班报告
    python3 main.py --data DIR   指定资料目录
"""
from __future__ import annotations

import argparse
from pathlib import Path

from goldstore.models import load_customers, load_inventory, load_policy
from goldstore.pipeline import Simulation
from goldstore.report import render_handover


def main():
    ap = argparse.ArgumentParser(description="金饰活动话术与预留有效性")
    ap.add_argument("--data", default="data", help="资料目录（含 policy/inventory/customers.json）")
    args = ap.parse_args()
    base = Path(args.data)
    policy = load_policy(base / "policy.json")
    items = load_inventory(base / "inventory.json")
    customers = load_customers(base / "customers.json")

    sim = Simulation(policy, items, customers).run()

    print("=" * 64)
    print("一日时间线")
    print("=" * 64)
    for line in sim.log:
        print(line)
    print()
    print(render_handover(sim))


if __name__ == "__main__":
    main()
