// Turns the self-improvement loop into growth events for the tree.
//
//   live    server.py is running a job: read web/lineage.json and /api/state every 1.5 s.
//   record  no job, but lineage.json holds a past run: replay it quickly, then the tree just lives.
//   demo    nothing to read: simulate the loop so the page never stands still. Shown as DEMO on screen.
//           Off when opts.liveOnly: then an empty lineage is just the pot ('empty').
//
// Versions already stored when the page opens arrive with instant:true and are drawn grown;
// only versions stored while you watch play their growth or their cut.
//
// Events: {type:'mode', mode} {type:'phase', text} {type:'propose', id, parent}
//         {type:'progress', id, done, total} {type:'verdict', v} {type:'practice', failed} {type:'reset'}

export function startFeed(emit, opts = {}) {
  const seen = new Set(), proposed = new Set(), practiceSeen = new Set(), progressSeen = {};
  let mode = null, demo = null, queue = [], first = true;
  const setMode = m => { if (m !== mode) { mode = m; emit({type:'mode', mode}); } };
  const getJSON = async u => { try { const r = await fetch(u, {cache:'no-store'}); return r.ok ? await r.json() : null; } catch { return null; } };

  function startDemo() { if (!demo) { setMode('demo'); demo = simulate(e => queue.push(e), opts); } }

  async function poll() {
    if (opts.demo) return startDemo();
    if (opts.src) {   // a finished run kept in runs/: grow it from the seed, one version at a time
      const text = await fetch(opts.src, {cache:'no-store'}).then(r => r.ok ? r.text() : '').catch(() => '');
      setMode('record');
      for (const line of text.split('\n')) { let v; try { v = JSON.parse(line); } catch { continue; }
        if (v?.id && !seen.has(v.id)) { seen.add(v.id); queue.push({type:'verdict', v}); } }
      return emit({type:'phase', text:'a finished run, replayed'});
    }
    const [lin, st] = await Promise.all([getJSON('lineage.json'), getJSON('api/state')]);
    const versions = lin?.versions || [], running = st?.job?.status === 'running';
    if (!versions.length && !running) { if (opts.liveOnly) { setMode('empty'); emit({type:'phase', text:'nothing grown yet: start a run'}); return; } return startDemo(); }
    if (demo) { demo.stop(); demo = null; queue = [{type:'reset'}]; seen.clear(); proposed.clear(); }
    setMode(running ? 'live' : 'record');
    for (const v of versions) if (!seen.has(v.id)) { seen.add(v.id); queue.push({type:'verdict', v, instant:first}); }
    first = false;
    if (!running) return emit({type:'phase', text:'resting: no run going'});
    if (st.job.phase) emit({type:'phase', text:st.job.phase});
    const best = [...versions].reverse().find(v => v.kept || !v.parent)?.id || 'g0';
    const agents = st.agents || [];
    for (const a of agents) {
      if (a.role === 'proposer' && !seen.has(a.version) && !proposed.has(a.version)) {
        proposed.add(a.version); queue.push({type:'propose', id:a.version, parent:best});
      }
      if (a.split === 'practice' && a.status === 'failed' && !practiceSeen.has(a.task + a.version)) {
        practiceSeen.add(a.task + a.version); queue.push({type:'practice', failed:1});
      }
    }
    const held = {};
    for (const a of agents) if (a.split === 'held-out' && !seen.has(a.version)) {
      const h = held[a.version] ??= {done:0, total:0}; h.total++;
      if (['passed', 'failed', 'cheat'].includes(a.status)) h.done++;
    }
    for (const [id, h] of Object.entries(held)) {
      if (!proposed.has(id)) { proposed.add(id); queue.push({type:'propose', id, parent:best}); }
      if (progressSeen[id] !== h.done) { progressSeen[id] = h.done; queue.push({type:'progress', id, ...h}); }
    }
  }

  // Drain with pacing: a long history replays fast, single live events land right away.
  (function drain() {
    const e = queue.shift();
    if (e) emit(e);
    setTimeout(drain, e?.instant ? 0 : queue.length > 8 ? 250 : e?.type === 'verdict' ? 700 : 120);
  })();
  poll(); setInterval(poll, 1500);
}

// A stand-in for the loop: practice run, proposal, held-out scoring, keep or prune. Never ends.
// opts.stress = N first grows N generations at once (drawn grown), to see how a long run looks.
function simulate(emit, opts = {}) {
  let stopped = false, quick = +opts.stress || 0;
  const wait = ms => quick ? Promise.resolve() : new Promise(r => setTimeout(r, ms));
  const out = emit; emit = e => quick ? e.type === 'verdict' && out({...e, instant:true}) : out(e);
  const pick = a => a[Math.floor(Math.random() * a.length)];
  const signals = ['claimed pass, never ran', 'same edit repeated', 'gave up early', 'stops before the hard cases', 'ignored the spec', 'tokens up, score down', 'no gain'];
  const changes = ['run the tests before finishing', 're-read the spec on failure', 'edge-case checklist', 'drop an unused tool', 'shorter rules',
    'stop after two failed edits', 'read the test names first', 'plan in one line', 'cap turns at 12', 'check types before running'];
  (async () => {
    let best = {id:'g0', score:.38};
    emit({type:'verdict', v:{id:'g0', parent:null, kept:true, score:best.score, change:'seed harness', signal:null}});
    for (let gen = 1; !stopped; gen++) {
      emit({type:'phase', text:`running ${best.id} on practice problems to find what fails`}); await wait(1600);
      for (let i = 0, n = 1 + Math.floor(Math.random() * 4); i < n; i++) emit({type:'practice', failed:1});
      if (quick && gen >= quick) quick = 0;
      for (let a = 0; a < 2 && !stopped; a++) {
        const id = `g${gen}${'ab'[a]}`;
        emit({type:'phase', text:`proposer is reading the failures and writing ${id}`}); emit({type:'propose', id, parent:best.id});
        await wait(1500);
        emit({type:'phase', text:`scoring ${id} on held-out problems`});
        for (let d = 1; d <= 8 && !stopped; d++) { await wait(420); emit({type:'progress', id, done:d, total:8}); }
        const room = 1 - best.score, cheat = Math.random() < .08, kept = !cheat && Math.random() < .3 + .3 * Math.exp(-gen / 30);
        const score = kept ? best.score + room * (.015 + Math.random() * .045) : cheat ? Math.min(.99, best.score + .05) : Math.max(.05, best.score - Math.random() * .06);
        const v = {id, parent:best.id, kept, score, change:pick(changes), signal:cheat ? 'cheat caught' : pick(signals)};
        emit({type:'verdict', v}); await wait(1000);
        if (kept) { best = v; break; }
      }
    }
  })();
  return { stop() { stopped = true; } };
}
