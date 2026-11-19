"""A small order export that runs the same way with or without LDR."""

import asyncio
import csv
import hashlib
import json
import os
import sqlite3
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl


def read_orders():
    with open("orders.csv", encoding="utf-8", newline="") as source:
        return [
            (row["order_id"], int(row["quantity"]), int(row["cents"]))
            for row in csv.DictReader(source)
        ]


def store_orders(orders):
    with sqlite3.connect(":memory:") as database:
        database.execute(
            "CREATE TABLE orders (order_id TEXT PRIMARY KEY, quantity INTEGER, cents INTEGER)"
        )
        database.executemany("INSERT INTO orders VALUES (?, ?, ?)", orders)
        return list(database.execute("SELECT * FROM orders ORDER BY order_id"))


def line_amount(order, rate):
    order_id, quantity, cents = order
    return order_id, quantity * cents * rate // 100


async def pricing_options():
    await asyncio.sleep(0)
    export_key = os.environ["EXPORT_API_KEY"]
    query = f"rate={os.environ.get('EXPORT_RATE', '100')}&api_key={export_key}"
    parameters = dict(parse_qsl(query))
    return int(parameters["rate"])


def make_export(orders, rate):
    totals = {}
    for order in orders:
        order_id, cents = line_amount(order, rate)
        totals[order_id] = cents
    return {"orders": totals, "total_cents": sum(totals.values())}


def write_export(report):
    encoded = json.dumps(report, sort_keys=True).encode()
    compressed = zlib.compress(encoded)
    Path("orders.json.zlib").write_bytes(compressed)
    return hashlib.sha256(encoded).hexdigest()


def main():
    orders = store_orders(read_orders())
    with ThreadPoolExecutor(max_workers=1) as pool:
        rate = asyncio.run(pricing_options())
        report = pool.submit(make_export, orders, rate).result()
    print("EXPORT", json.dumps({**report, "digest": write_export(report)}))


if __name__ == "__main__":
    main()
