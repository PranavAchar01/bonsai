// The hosted app's only server code. Runs stay on the team laptop; MongoDB Atlas carries everything between.
//   bridge.py (next to server.py on the laptop) mirrors /api/state, web/lineage.json and runs/*-versions.jsonl
//   into bonsai.live, and answers every other request (Grow, Stop, watch, replays) through bonsai.rpc.
// Requests that change anything need the team key (BONSAI_KEY) in the x-bonsai-key header: a run spends model credit.
import { MongoClient } from "mongodb";

const client = (globalThis.bonsaiMongo ??= new MongoClient(process.env.MONGODB_URI, { serverSelectionTimeoutMS: 8000 }));
const db = () => client.db("bonsai");
const MIRRORED = /^\/(api\/state|lineage\.json|runs\/[\w.-]+\.jsonl)$/;
const STATE_FRESH_MS = 15000; // older than this, the laptop is offline and the page goes view-only
const RPC_WAIT_MS = 9000;

const sleep = ms => new Promise(r => setTimeout(r, ms));

// With nothing mirrored yet, the main tree is the newest run stored in bonsai.versions.
async function lineageFromVersions() {
  const versions = db().collection("versions");
  const seed = await versions.find({ generation: 0 }).sort({ ts: -1 }).limit(1).next();
  if (!seed) return null;
  const docs = await versions.find(seed.run ? { run: seed.run } : { _id: seed._id }, { projection: { _id: 0 } }).sort({ ts: 1 }).toArray();
  return JSON.stringify({ versions: docs });
}

export default async function handler(req, res) {
  const { p = "", rest: _rest, ver: _ver, game: _game, file: _file, ...query } = req.query;
  const qs = new URLSearchParams(query).toString();
  res.setHeader("Cache-Control", "no-store");
  try {
    if (req.method === "GET" && MIRRORED.test(p)) {
      const doc = await db().collection("live").findOne({ _id: p });
      if (p === "/api/state" && (!doc || Date.now() - doc.ts > STATE_FRESH_MS)) {
        return res.status(503).json({ error: "the team laptop is offline, so this page shows the last saved tree" });
      }
      const body = doc ? doc.body : p === "/lineage.json" ? await lineageFromVersions() : null;
      if (body == null) return res.status(404).json({ error: `nothing saved yet for ${p}` });
      res.setHeader("Content-Type", doc ? doc.type : "application/json");
      return res.status(200).send(body);
    }
    if (req.method !== "GET" && (!process.env.BONSAI_KEY || req.headers["x-bonsai-key"] !== process.env.BONSAI_KEY)) {
      return res.status(403).json({ error: "starting or stopping runs needs the team key: open the page once with ?key=<team key>" });
    }
    // Everything else is answered by the laptop: post the request to Atlas and wait for the bridge's reply.
    const rpc = db().collection("rpc");
    const body = req.method === "GET" ? null : typeof req.body === "string" ? req.body : JSON.stringify(req.body ?? {});
    const { insertedId } = await rpc.insertOne({ method: req.method, path: p + (qs ? `?${qs}` : ""), body, status: "pending", ts: Date.now() });
    for (const until = Date.now() + RPC_WAIT_MS; Date.now() < until; ) {
      await sleep(250);
      const done = await rpc.findOne({ _id: insertedId, status: "done" });
      if (done) {
        await rpc.deleteOne({ _id: insertedId });
        res.setHeader("Content-Type", done.type || "application/json");
        return res.status(done.code).send(done.body);
      }
    }
    await rpc.deleteOne({ _id: insertedId });
    return res.status(504).json({ error: "the team laptop did not answer: is bridge.py running next to server.py?" });
  } catch (e) {
    return res.status(502).json({ error: `could not reach MongoDB Atlas: ${String(e.message).slice(0, 200)}` });
  }
}
