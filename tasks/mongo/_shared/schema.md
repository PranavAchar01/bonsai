# The nursery database

Two collections. The examples in `sample/` are a small slice; the real collections are larger.

## orders (600 documents)
- `_id`: order id, like "ord-0042"
- `customer`: `{name, email}`. The same person's email is not always typed in the same case.
- `region`: one of Kanto, Kansai, Kyushu, Tohoku, Hokkaido
- `status`: paid, refunded or cancelled. Older rows spell paid as "PAID" or "Paid"; they all count as paid.
- `placed`: the date as a string, "YYYY-MM-DD"
- `items`: a list of lines, each `{species, qty, unit_price}`. A species can appear on more than one line.
  An order's revenue is the sum of qty times unit_price over its lines. Prices are whole yen.
- `shipping_fee`: whole yen, not part of revenue unless a question says so
- `coupon`: a code, or null, or missing. Null and missing both mean no coupon.

## trees (14 documents): the catalogue
- `_id`: the species name, the same string used in `items.species`
- `style`, `age_years`, `price` (catalogue price in whole yen), `in_stock`

## Your answer
Write `pipeline.json` as `{"collection": "orders" or "trees", "pipeline": [ ...aggregation stages... ]}`.
The pipeline's output is compared with the right answer: same documents, same fields, same order.
A field you were not asked for (including `_id`) makes the answer wrong.

`check.py` runs your pipeline against the sample slice and prints what comes back:
`uv run --no-project --with pymongo python check.py`
