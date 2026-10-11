#!/bin/sh
# Assemble the static half of the Vercel deploy from the repo, then deploy:
#   sh deploy/build.sh && cd deploy && vercel deploy --prod
# Copies web/, each chess game's game.json and the saved replays. Never copies tasks/*/expected.json.
set -e
cd "$(dirname "$0")"
rm -rf public
mkdir -p public/web public/tasks/chess
cp -R ../web/. public/web/
rm -f public/web/lineage.json  # served live from Atlas instead
for g in ../tasks/chess/game-*/; do
  n=$(basename "$g")
  mkdir -p "public/tasks/chess/$n"
  cp "$g/game.json" "public/tasks/chess/$n/"
done
if [ -d ../tasks/chess/_replays ]; then
  cp -R ../tasks/chess/_replays public/tasks/chess/_replays
  mkdir -p public/web/replays && cp -R ../tasks/chess/_replays public/web/replays/chess
fi
# The page sends the team key (from ?key= once, then remembered) on requests that start or stop runs.
python3 - <<'EOF'
from pathlib import Path
p = Path("public/web/index.html")
key = """<script>(() => {
  const q = new URLSearchParams(location.search).get("key");
  let k = q;
  try { if (q) localStorage.setItem("bonsaiKey", q); k = k || localStorage.getItem("bonsaiKey"); } catch {}
  const f = window.fetch.bind(window);
  window.fetch = (u, o = {}) => (k && o.method && o.method !== "GET" ? f(u, { ...o, headers: { ...(o.headers || {}), "x-bonsai-key": k } }) : f(u, o));
})();</script>
"""
s = p.read_text()
p.write_text(s.replace("<head>", "<head>\n" + key, 1))
EOF
echo "public/ ready: $(find public -type f | wc -l | tr -d ' ') files"
