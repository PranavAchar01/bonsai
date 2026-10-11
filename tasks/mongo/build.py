"""Build the nursery question set: a seeded dataset, sixteen questions, and their answers.

Not a benchmark. Each question is answered with one MongoDB aggregation pipeline, and the answer is
checked against a result computed here in plain Python, so there is nothing to argue about.

  python tasks/mongo/build.py            writes data, questions and expected answers
  python tasks/mongo/build.py --load     also loads the data into the local MongoDB (needs pymongo)
"""

import datetime
import json
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
MONGO_URL = os.getenv("BONSAI_MONGO_URL", "mongodb://127.0.0.1:27018")
R = random.Random(20261011)

STYLES = ["chokkan", "moyogi", "shakan", "kengai", "bunjin"]
SPECIES = ["black pine", "white pine", "juniper", "trident maple", "japanese maple", "zelkova", "hornbeam", "azalea",
           "ume", "larch", "cedar", "hinoki", "quince", "wisteria"]  # fmt: skip
REGIONS = ["Kanto", "Kansai", "Kyushu", "Tohoku", "Hokkaido"]
COUPONS = ["SPRING10", "MOSS5", "FIRSTTREE"]

trees = [{"_id": s, "style": STYLES[i % 5], "age_years": R.randint(4, 40), "price": R.randrange(6000, 90000, 500),
          "in_stock": R.random() < 0.8} for i, s in enumerate(SPECIES)]  # fmt: skip
price = {t["_id"]: t["price"] for t in trees}
SOLD = SPECIES[:11]  # hinoki, quince and wisteria never appear in an order
people = [(f"{a} {b}", f"{a}.{b}@example.jp".lower()) for a in
          ["Aiko", "Ren", "Yui", "Sora", "Mei", "Kaito", "Hana", "Riku", "Emi", "Daichi", "Nao", "Tomo"]
          for b in ["Sato", "Mori", "Abe", "Ito", "Ueda", "Kato", "Noda", "Oka", "Seki", "Baba"]]  # fmt: skip

orders = []
for n in range(1, 601):
    name, email = R.choice(people[:90] if n > 40 else people)
    if R.random() < 0.15:
        email = email.upper() if R.random() < 0.5 else email.capitalize()
    status = R.choices(
        ["paid", "PAID", "Paid", "refunded", "cancelled"], [70, 4, 4, 12, 10]
    )[0]
    day = datetime.date(2025, 7, 1) + datetime.timedelta(days=R.randrange(365))
    items = []
    for _ in range(R.choices([1, 2, 3, 4, 5], [30, 30, 20, 12, 8])[0]):
        s = R.choice(
            SOLD[:10] if status.lower() == "paid" else SOLD
        )  # cedar sells only in orders that were not paid
        discounted = R.random() < 0.2
        items.append({"species": s, "qty": R.choices([1, 2, 3], [70, 22, 8])[0],
                      "unit_price": price[s] - R.randrange(500, 3000, 500) if discounted else price[s]})  # fmt: skip
    doc = {"_id": f"ord-{n:04d}", "customer": {"name": name, "email": email}, "region": R.choice(REGIONS),
           "status": status, "placed": day.isoformat(), "items": items, "shipping_fee": R.choice([0, 800, 1500, 2400])}  # fmt: skip
    c = R.random()
    if c < 0.25:
        doc["coupon"] = R.choice(COUPONS)
    elif c < 0.45:
        doc["coupon"] = None
    orders.append(doc)

paid = [o for o in orders if o["status"].lower() == "paid"]
total = lambda o: sum(i["qty"] * i["unit_price"] for i in o["items"])  # noqa: E731
units = lambda o: sum(i["qty"] for i in o["items"])  # noqa: E731
mail = lambda o: o["customer"]["email"].lower()  # noqa: E731


def grouped(rows, key, value):
    out = defaultdict(int)
    for r in rows:
        out[key(r)] += value(r)
    return out


def q_revenue_by_region():
    g = grouped(paid, lambda o: o["region"], total)
    return [
        {"region": k, "revenue": v}
        for k, v in sorted(g.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def q_top_species():
    g = Counter()
    for o in paid:
        for i in o["items"]:
            g[i["species"]] += i["qty"]
    return [
        {"species": k, "units": v}
        for k, v in sorted(g.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    ]


def q_coupon_split():
    w = sum(1 for o in orders if o.get("coupon"))
    return [{"with_coupon": w, "without_coupon": len(orders) - w}]


def q_repeat_customers():
    g = Counter(mail(o) for o in orders if o["status"] != "cancelled")
    return [{"customers": sum(1 for v in g.values() if v >= 3)}]


def q_monthly_revenue():
    g = grouped(
        [o for o in paid if o["placed"] >= "2026-01-01"],
        lambda o: o["placed"][:7],
        total,
    )
    return [{"month": k, "revenue": v} for k, v in sorted(g.items())]


def q_largest_orders():
    rows = sorted(paid, key=lambda o: (-(total(o) + o["shipping_fee"]), o["_id"]))[:3]
    return [{"order_id": o["_id"], "total": total(o) + o["shipping_fee"]} for o in rows]


def q_refund_rate():
    out = []
    for r in sorted(REGIONS):
        p = sum(1 for o in paid if o["region"] == r)
        f = sum(1 for o in orders if o["region"] == r and o["status"] == "refunded")
        out.append({"region": r, "refund_rate": round(f / (p + f), 3)})
    return out


def q_avg_basket():
    rows = [{"region": r, "avg_units": round(sum(units(o) for o in paid if o["region"] == r)
                                            / sum(1 for o in paid if o["region"] == r), 2)} for r in REGIONS]  # fmt: skip
    return sorted(rows, key=lambda d: (-d["avg_units"], d["region"]))


def q_unsold_species():
    sold = {i["species"] for o in paid for i in o["items"]}
    return [{"species": s} for s in sorted(set(SPECIES) - sold)]


def q_coupon_revenue():
    rows = [o for o in paid if o.get("coupon")]
    n, rev = (
        Counter(o["coupon"] for o in rows),
        grouped(rows, lambda o: o["coupon"], total),
    )
    return [
        {"coupon": k, "orders": n[k], "revenue": v}
        for k, v in sorted(rev.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def q_best_customer():
    out = []
    for r in sorted(REGIONS):
        g = grouped([o for o in paid if o["region"] == r], mail, total)
        email, rev = sorted(g.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        out.append({"region": r, "email": email, "revenue": rev})
    return out


def q_discounted_lines():
    g = Counter(
        i["species"]
        for o in paid
        for i in o["items"]
        if i["unit_price"] < price[i["species"]]
    )
    return [
        {"species": k, "discounted_lines": v}
        for k, v in sorted(g.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def q_weekday():
    g = Counter(
        datetime.date.fromisoformat(o["placed"]).isoweekday() % 7 + 1 for o in paid
    )
    return [
        {"weekday": k, "orders": v}
        for k, v in sorted(g.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def q_varied_orders():
    return [
        {"orders": sum(1 for o in paid if len({i["species"] for i in o["items"]}) >= 3)}
    ]


def q_style_revenue():
    style = {t["_id"]: t["style"] for t in trees}
    g = defaultdict(int)
    for o in paid:
        for i in o["items"]:
            g[style[i["species"]]] += i["qty"] * i["unit_price"]
    return [
        {"style": k, "revenue": v}
        for k, v in sorted(g.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def q_new_customers():
    first = {}
    for o in orders:
        first[mail(o)] = min(first.get(mail(o), "9"), o["placed"])
    g = Counter(d[:7] for d in first.values())
    return [{"month": k, "new_customers": v} for k, v in sorted(g.items())]


QUESTIONS = {
    "revenue-by-region": (q_revenue_by_region, "Revenue from paid orders in each region.",
        "One document per region with exactly the fields `region` and `revenue`, highest revenue first."),
    "top-species": (q_top_species, "The five species that sold the most units in paid orders.",
        "Exactly the fields `species` and `units`, most units first. Break ties by species name, A to Z."),
    "coupon-split": (q_coupon_split, "How many orders (any status) used a coupon, and how many did not.",
        "A single document with exactly the fields `with_coupon` and `without_coupon`."),
    "repeat-customers": (q_repeat_customers, "How many customers have three or more orders that were not cancelled. One person is one email address.",
        "A single document with exactly the field `customers`."),
    "monthly-revenue": (q_monthly_revenue, "Revenue from paid orders for each month of 2026.",
        "Exactly the fields `month` (like \"2026-01\") and `revenue`, earliest month first."),
    "largest-orders": (q_largest_orders, "The three largest paid orders, counting the shipping fee in the order's total.",
        "Exactly the fields `order_id` and `total`, largest first. Break ties by order id, A to Z."),
    "refund-rate": (q_refund_rate, "The refund rate in each region: refunded orders divided by paid plus refunded orders. Cancelled orders are left out.",
        "Exactly the fields `region` and `refund_rate` (rounded to 3 decimal places), regions A to Z."),
    "avg-basket": (q_avg_basket, "The average number of units (trees) in a paid order, for each region.",
        "Exactly the fields `region` and `avg_units` (rounded to 2 decimal places), highest first. Break ties by region, A to Z."),
    "unsold-species": (q_unsold_species, "The species in the catalogue that have never been sold in a paid order.",
        "Exactly the field `species`, A to Z."),
    "coupon-revenue": (q_coupon_revenue, "For each coupon code, the number of paid orders that used it and their revenue.",
        "Exactly the fields `coupon`, `orders` and `revenue`, highest revenue first."),
    "best-customer": (q_best_customer, "In each region, the customer who brought in the most revenue from paid orders. One person is one email address.",
        "Exactly the fields `region`, `email` (lower case) and `revenue`, regions A to Z. Break ties by email, A to Z."),
    "discounted-lines": (q_discounted_lines, "For each species, how many order lines in paid orders were sold below the catalogue price.",
        "Exactly the fields `species` and `discounted_lines`, most first. Break ties by species name, A to Z. Leave out species with none."),
    "weekday": (q_weekday, "How many paid orders were placed on each day of the week.",
        "Exactly the fields `weekday` (1 is Sunday, 7 is Saturday) and `orders`, busiest first. Break ties by weekday number."),
    "varied-orders": (q_varied_orders, "How many paid orders contain three or more different species.",
        "A single document with exactly the field `orders`."),
    "style-revenue": (q_style_revenue, "Revenue from paid orders for each bonsai style. A species' style is in the catalogue.",
        "Exactly the fields `style` and `revenue`, highest revenue first."),
    "new-customers": (q_new_customers, "For each month, how many customers placed their first ever order (any status) in that month. One person is one email address.",
        "Exactly the fields `month` (like \"2025-07\") and `new_customers`, earliest month first."),
}  # fmt: skip

SCHEMA = f"""# The nursery database

Two collections. The examples in `sample/` are a small slice; the real collections are larger.

## orders ({len(orders)} documents)
- `_id`: order id, like "ord-0042"
- `customer`: `{{name, email}}`. The same person's email is not always typed in the same case.
- `region`: one of {", ".join(REGIONS)}
- `status`: paid, refunded or cancelled. Older rows spell paid as "PAID" or "Paid"; they all count as paid.
- `placed`: the date as a string, "YYYY-MM-DD"
- `items`: a list of lines, each `{{species, qty, unit_price}}`. A species can appear on more than one line.
  An order's revenue is the sum of qty times unit_price over its lines. Prices are whole yen.
- `shipping_fee`: whole yen, not part of revenue unless a question says so
- `coupon`: a code, or null, or missing. Null and missing both mean no coupon.

## trees ({len(trees)} documents): the catalogue
- `_id`: the species name, the same string used in `items.species`
- `style`, `age_years`, `price` (catalogue price in whole yen), `in_stock`

## Your answer
Write `pipeline.json` as `{{"collection": "orders" or "trees", "pipeline": [ ...aggregation stages... ]}}`.
The pipeline's output is compared with the right answer: same documents, same fields, same order.
A field you were not asked for (including `_id`) makes the answer wrong.

`check.py` runs your pipeline against the sample slice and prints what comes back:
`uv run --no-project --with pymongo python check.py`
"""

CHECK = '''"""Run pipeline.json against the sample slice and print the result."""
import json, os
from pymongo import MongoClient

spec = json.load(open("pipeline.json"))
db = MongoClient(os.getenv("BONSAI_MONGO_URL", "mongodb://127.0.0.1:27018"))["nursery_sample"]
for doc in db[spec["collection"]].aggregate(spec["pipeline"], maxTimeMS=10000):
    print(json.dumps(doc, default=str))
'''


def main() -> None:
    shared = HERE / "_shared"
    (shared / "sample").mkdir(parents=True, exist_ok=True)
    (HERE / "_data.json").write_text(json.dumps({"orders": orders, "trees": trees}))
    (shared / "sample" / "orders.json").write_text(json.dumps(orders[:40], indent=1))
    (shared / "sample" / "trees.json").write_text(json.dumps(trees, indent=1))
    (shared / "schema.md").write_text(SCHEMA)
    (shared / "check.py").write_text(CHECK)
    for name, (fn, question, shape) in QUESTIONS.items():
        d = HERE / name
        (d / ".docs").mkdir(parents=True, exist_ok=True)
        (d / ".docs" / "instructions.md").write_text(
            f"# {name}\n\n{question}\n\n{shape}\n\nRead `schema.md` first. Write your answer to `pipeline.json`.\n"
        )
        (d / "pipeline.json").write_text('{"collection": "orders", "pipeline": []}\n')
        (d / "expected.json").write_text(json.dumps(fn(), indent=1))
        print(f"{name:20} {len(fn())} rows")
    if "--load" in sys.argv:
        from pymongo import MongoClient

        client = MongoClient(MONGO_URL)
        for db, rows in (("nursery", orders), ("nursery_sample", orders[:40])):
            for coll, docs in (("orders", rows), ("trees", trees)):
                client[db][coll].drop()
                client[db][coll].insert_many(docs)
        print("loaded into", MONGO_URL)


if __name__ == "__main__":
    main()
