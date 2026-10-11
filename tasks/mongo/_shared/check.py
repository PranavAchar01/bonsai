"""Run pipeline.json against the sample slice and print the result."""
import json, os
from pymongo import MongoClient

spec = json.load(open("pipeline.json"))
db = MongoClient(os.getenv("BONSAI_MONGO_URL", "mongodb://127.0.0.1:27018"))["nursery_sample"]
for doc in db[spec["collection"]].aggregate(spec["pipeline"], maxTimeMS=10000):
    print(json.dumps(doc, default=str))
