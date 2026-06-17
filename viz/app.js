/* app.js — a narrated brokerage story. Plays precomputed frames from window.VIZ_DATA. */
(function () {
  const D = window.VIZ_DATA;
  if (!D) { document.body.innerHTML = "<p style='padding:24px;font:16px sans-serif'>data.js not loaded — run <code>python export_viz.py</code>.</p>"; return; }

  const FOCUS = "Pakistan";
  const AXIS = new Set(["US", "Iran", "Pakistan"]);
  const PRINCIPALS = new Set(["US", "Iran", "Israel", "Hezbollah", "Lebanon"]);  // combatants, not brokers
  const BROKER_VERBS = ["broker", "mediate", "facilitate", "host", "convene", "relay", "urge", "offer", "pitch"];
  // verbs that mark a genuine mediating act (matched as substrings of raw_verb)
  const MED_VERBS = ["broker", "mediat", "facilitat", "host", "conven", "relay", "shuttle",
    "intermediar", "go-between", "go between", "bring together", "urge", "call for", "called for",
    "call on", "called on", "push for", "pressing", "press for", "negotiat", "de-escalat",
    "de escalat", "deescalat", "dialogue", "resume talks", "return to", "engage", "restraint", "talks between"];
  const META = D.meta, NW = META.num_weeks;
  const EDGE_CAP = 24;
  const bWeek = META.week_dates.findIndex(d => d >= META.phase_boundary);

  const state = { outlet: "dawn", mode: "window", pol: "all", week: 0, playing: false, speed: 2, storyMode: true, pin: null, hover: null, chapId: null, heroKey: null, holding: false };
  let COLORS = {};

  // ---------- bloc-axis layout: US (left) — Pakistan (center) — Iran (right) ----------
  const BLOC_ANCHOR = { US_bloc: [0.14, 0.50], Iran_bloc: [0.86, 0.46], Broker: [0.50, 0.50], Mediators: [0.50, 0.86], E3: [0.28, 0.16], Arab_states: [0.82, 0.84], International: [0.66, 0.13] };
  const PRIMARY = { US_bloc: "US", Iran_bloc: "Iran", Broker: "Pakistan" };
  const pos = new Map();
  const meta = new Map(D.nodes_meta.map(n => [n.id, n]));
  (function layout() {
    for (const [bloc, members] of d3.group(D.nodes_meta, n => n.bloc)) {
      const [ax, ay] = BLOC_ANCHOR[bloc] || [0.5, 0.5];
      const prim = PRIMARY[bloc], hasPrim = members.some(m => m.id === prim);
      const others = members.filter(m => m.id !== prim);
      if (hasPrim) pos.set(prim, { x: ax, y: ay });
      const n = others.length, r = Math.min(0.13, 0.055 + 0.012 * n);
      others.forEach((m, i) => {
        const a = hasPrim ? (-Math.PI / 2) + (i + 1) * (2 * Math.PI / (n + 1)) : i * (2 * Math.PI / Math.max(1, n)) - Math.PI / 2;
        let x = ax + r * Math.cos(a), y = ay + r * Math.sin(a) * 1.05;
        pos.set(m.id, { x: Math.max(.05, Math.min(.95, x)), y: Math.max(.10, Math.min(.88, y)) });
      });
    }
  })();

  const sizeScale = d3.scaleSqrt().domain([0, META.max_size]).range([5, 30]);
  const ringScale = d3.scaleSqrt().domain([0, 1]).range([0, 15]);
  const edgeW = d3.scaleSqrt().domain([1, 40]).range([1, 7]).clamp(true);

  // ---------- svg ----------
  const svg = d3.select("#graph");
  const defs = svg.append("defs");
  ["coop", "conflict", "neutral"].forEach(k => {
    defs.append("marker").attr("id", "arr-" + k).attr("viewBox", "0 0 10 10").attr("refX", 8).attr("refY", 5)
      .attr("markerUnits", "userSpaceOnUse")        // fixed-size arrowheads, not scaled by stroke width
      .attr("markerWidth", 11).attr("markerHeight", 11).attr("orient", "auto-start-reverse")
      .append("path").attr("d", "M0,1.5 L9,5 L0,8.5 z").attr("opacity", .85);
  });
  const gBloc = svg.append("g"), gEdges = svg.append("g"), gFlow = svg.append("g"), gNodes = svg.append("g"), gFlash = svg.append("g");
  let W = 0, H = 0; const PADL = 300, PADR = 300, PADT = 130, PADB = 150;  // reserve room for side panels
  const X = x => PADL + x * (W - PADL - PADR), Y = y => PADT + y * (H - PADT - PADB);

  function readColors() {
    const cs = getComputedStyle(document.body);
    COLORS = { coop: cs.getPropertyValue("--coop").trim(), conflict: cs.getPropertyValue("--conflict").trim(), neutral: cs.getPropertyValue("--neutral").trim() };
    defs.selectAll("marker path").attr("fill", function () { return COLORS[this.parentNode.id.replace("arr-", "")]; });
  }

  function resize() { const r = document.getElementById("stage").getBoundingClientRect(); W = r.width; H = r.height; svg.attr("viewBox", `0 0 ${W} ${H}`); drawBlocLabels(); render(true); }
  function drawBlocLabels() {
    const labels = { Broker: "Broker", US_bloc: "US bloc", Iran_bloc: "Iran bloc", E3: "Europe / E3", Mediators: "Gulf mediators", Arab_states: "Arab states", International: "UN · IAEA" };
    const sel = gBloc.selectAll("text").data(Object.entries(BLOC_ANCHOR), d => d[0]);
    sel.enter().append("text").attr("class", "bloc-label").attr("text-anchor", "middle").merge(sel)
      .attr("x", d => X(d[1][0])).attr("y", d => Y(d[1][1]) - (d[0] === "Broker" ? 64 : 52)).text(d => labels[d[0]] || d[0]);
  }

  const frame = () => D.frames[`${state.outlet}|${state.mode}|${state.week}`];
  const weekDate = w => META.week_dates[w];
  const polOk = p => state.pol === "all" || state.pol === p;
  const dyadKey = (a, b) => a < b ? a + "|" + b : b + "|" + a;
  const valPol = e => e.valence === "coop" ? "coop" : e.valence === "conflict" ? "conflict" : "neutral";
  const pretty = s => (s || "").replace(/_/g, " ");
  const esc = s => (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const chapterAt = w => D.chapters.slice().reverse().find(c => w >= c.start_week) || D.chapters[0];
  const leadBroker = () => { const c = chapterAt(state.week); return (c[state.outlet] || {}).lead_broker_g; };

  function visibleEdges(fr) {
    let es = fr.edges.filter(e => polOk(e.pol) && pos.has(e.s) && pos.has(e.t));
    es.sort((a, b) => b.w - a.w);
    const top = es.slice(0, EDGE_CAP), have = new Set(top.map(e => e.s + ">" + e.t));
    const lb = leadBroker();
    es.forEach(e => { if ((e.s === FOCUS || e.t === FOCUS || e.s === lb || e.t === lb) && !have.has(e.s + ">" + e.t)) { top.push(e); have.add(e.s + ">" + e.t); } });
    return top;
  }
  function geomFn(sizeById) {
    return e => {
      const a = pos.get(e.s), b = pos.get(e.t);
      const x1 = X(a.x), y1 = Y(a.y), x2 = X(b.x), y2 = Y(b.y);
      let dx = x2 - x1, dy = y2 - y1; const len = Math.hypot(dx, dy) || 1; dx /= len; dy /= len;
      const rs = sizeScale(sizeById.get(e.s) || 0) + 2, rt = sizeScale(sizeById.get(e.t) || 0) + 8;
      const sx = x1 + dx * rs, sy = y1 + dy * rs, ex = x2 - dx * rt, ey = y2 - dy * rt;
      const mx = (sx + ex) / 2, my = (sy + ey) / 2, curve = Math.min(60, len * 0.13);
      return `M${sx.toFixed(1)},${sy.toFixed(1)} Q${(mx - dy * curve).toFixed(1)},${(my + dx * curve).toFixed(1)} ${ex.toFixed(1)},${ey.toFixed(1)}`;
    };
  }

  function render(instant) {
    const fr = frame(); if (!fr) return;
    const dur = instant ? 0 : 520;
    const nodeData = fr.nodes.filter(n => pos.has(n.id));
    const sizeById = new Map(nodeData.map(n => [n.id, n.size]));
    const edgeData = visibleEdges(fr);
    const geom = geomFn(sizeById);
    const lb = leadBroker();
    const focusNode = state.pin?.type === "node" ? state.pin.id : state.hover;
    const focusDyad = state.pin?.type === "dyad" ? state.pin.key : null;
    const edgeActive = e => focusDyad ? dyadKey(e.s, e.t) === focusDyad : focusNode ? (e.s === focusNode || e.t === focusNode) : true;

    // edges
    const ek = e => e.s + ">" + e.t;
    const edges = gEdges.selectAll("path.edge").data(edgeData, ek);
    edges.exit().transition().duration(dur).attr("stroke-opacity", 0).remove();
    const enter = edges.enter().append("path").attr("class", "edge").attr("d", geom).attr("stroke-opacity", 0)
      .attr("stroke-dasharray", function () { const L = this.getTotalLength(); return L + " " + L; })
      .attr("stroke-dashoffset", function () { return this.getTotalLength(); })
      .on("click", (ev, e) => { ev.stopPropagation(); pinDyad(e.s, e.t); });
    enter.transition().duration(700).attr("stroke-dashoffset", 0);
    enter.merge(edges).attr("stroke", e => COLORS[e.pol]).attr("marker-end", e => "url(#arr-" + e.pol + ")")
      .classed("dim", e => !edgeActive(e)).transition().duration(dur)
      .attr("d", geom).attr("stroke-width", e => edgeW(e.w))
      .attr("stroke-opacity", e => edgeActive(e) ? (e.pol === "neutral" ? .3 : .58) : .1);

    // flow overlay on the lead broker's cooperation ties
    const flowData = edgeData.filter(e => e.pol === "coop" && (e.s === lb || e.t === lb) && edgeActive(e));
    const flow = gFlow.selectAll("path.flow").data(flowData, ek);
    flow.exit().remove();
    flow.enter().append("path").attr("class", "flow").merge(flow).attr("d", geom);

    // nodes
    const nodes = gNodes.selectAll("g.node").data(nodeData, d => d.id);
    nodes.exit().transition().duration(dur).style("opacity", 0).attr("transform", d => `translate(${X(pos.get(d.id).x)},${Y(pos.get(d.id).y)}) scale(.1)`).remove();
    const nEnter = nodes.enter().append("g").attr("class", "node")
      .attr("transform", d => `translate(${X(pos.get(d.id).x)},${Y(pos.get(d.id).y)}) scale(.1)`).style("opacity", 0)
      .on("mouseover", (ev, d) => { state.hover = d.id; highlight(); })
      .on("mouseout", () => { state.hover = null; highlight(); })
      .on("click", (ev, d) => { ev.stopPropagation(); pinNode(d.id); });
    nEnter.append("circle").attr("class", "node-ring");
    nEnter.append("circle").attr("class", "node-dot");
    nEnter.append("text").attr("class", "node-label").attr("text-anchor", "middle");
    nEnter.each(d => flash(pos.get(d.id)));

    const all = nEnter.merge(nodes);
    all.classed("dim", d => !nodeActiveFn(d.id, edgeData, focusNode, focusDyad));
    all.transition().duration(dur).style("opacity", 1).attr("transform", d => `translate(${X(pos.get(d.id).x)},${Y(pos.get(d.id).y)}) scale(1)`);
    all.select("circle.node-dot").attr("fill", d => meta.get(d.id).color).transition().duration(dur).attr("r", d => sizeScale(d.size));
    all.select("circle.node-ring").attr("stroke", d => d.id === FOCUS ? "#ffb733" : meta.get(d.id).color).attr("stroke-opacity", d => d.id === FOCUS ? .95 : .8)
      .transition().duration(dur).attr("r", d => sizeScale(d.size) + 3 + ringScale(d.broker_g)).attr("stroke-width", d => 1 + ringScale(d.broker_g));
    all.select("text.node-label").classed("pak", d => d.id === FOCUS)
      .style("font-size", d => (d.id === FOCUS ? 15 : AXIS.has(d.id) ? 13 : 11) + "px")
      .text(d => (d.id === FOCUS || AXIS.has(d.id) || d.size >= META.max_size * 0.04 || d.id === focusNode || d.id === lb) ? pretty(d.id) : "")
      .transition().duration(dur).attr("dy", d => -(sizeScale(d.size) + 7 + ringScale(d.broker)));

    updateChapter(); updateDate(); drawBoard(fr, dur); updateSpotlight(); drawMentions(); updateTimeline();
  }
  function nodeActiveFn(id, edgeData, fn, fd) {
    if (fd) { const [a, b] = fd.split("|"); return id === a || id === b; }
    if (fn) { if (id === fn) return true; return edgeData.some(e => (e.s === fn && e.t === id) || (e.t === fn && e.s === id)); }
    return true;
  }
  function flash(p) {
    gFlash.append("circle").attr("cx", X(p.x)).attr("cy", Y(p.y)).attr("r", 6).attr("fill", "none")
      .attr("stroke", COLORS.coop).attr("stroke-width", 2).attr("opacity", .9)
      .transition().duration(900).ease(d3.easeCubicOut).attr("r", 42).attr("opacity", 0).remove();
  }
  function highlight() {
    const fr = frame(); if (!fr) return;
    const focusNode = state.pin?.type === "node" ? state.pin.id : state.hover;
    const focusDyad = state.pin?.type === "dyad" ? state.pin.key : null;
    const edgeData = visibleEdges(fr);
    const ea = e => focusDyad ? dyadKey(e.s, e.t) === focusDyad : focusNode ? (e.s === focusNode || e.t === focusNode) : true;
    gEdges.selectAll("path.edge").classed("dim", e => !ea(e)).attr("stroke-opacity", e => ea(e) ? (e.pol === "neutral" ? .3 : .58) : .1);
    gNodes.selectAll("g.node").classed("dim", d => !nodeActiveFn(d.id, edgeData, focusNode, focusDyad));
  }

  // ---------- chapter narration ----------
  const rankWord = r => r == null ? "unranked" : "#" + r;
  function updateChapter() {
    const c = chapterAt(state.week);
    const sig = c.id + "|" + state.outlet;
    if (sig === state.chapId) return;
    state.chapId = sig;
    document.getElementById("ch-kicker").textContent = `Chapter ${c.id} of ${D.chapters.length}`;
    document.getElementById("ch-title").textContent = c.title;
    document.getElementById("ch-lead").textContent = c.lead;
    const me = c[state.outlet], lb = me.lead_broker_g;
    document.getElementById("ch-broker").innerHTML = lb
      ? `Lead broker in <b style="color:var(--ink)">${state.outlet[0].toUpperCase() + state.outlet.slice(1)}</b>: <b>${pretty(lb)}</b>` : "";
    const dr = c.dawn.pak_rank_g, gr = c.guardian.pak_rank_g;
    document.getElementById("ch-stat").innerHTML =
      `<span>Pakistan as broker — <b>Dawn</b> <span class="pakhi">${rankWord(dr)}</span></span>` +
      `<span><b>Guardian</b> <span class="${gr && gr <= 5 ? "pakhi" : ""}">${rankWord(gr)}</span></span>`;
    ["ch-title", "ch-lead", "ch-broker", "ch-stat"].forEach(id => { const el = document.getElementById(id); el.classList.remove("fade-swap"); void el.offsetWidth; el.classList.add("fade-swap"); });
  }
  function updateDate() {
    document.getElementById("curdate").textContent = new Date(weekDate(state.week)).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
    const b = document.getElementById("phasebadge");
    const ph = (state.week === bWeek - 1 || state.week === bWeek) ? "during" : (weekDate(state.week) < META.phase_boundary ? "pre" : "post");
    b.textContent = ph === "during" ? "During the talks" : ph === "pre" ? "Before the talks" : "After the talks";
    b.className = ph;
  }

  // ---------- brokerage power (third parties only) ----------
  function drawBoard(fr, dur) {
    const meds = fr.nodes.filter(n => !PRINCIPALS.has(n.id) && n.broker_g > 0).sort((a, b) => b.broker_g - a.broker_g).slice(0, 5)
      .map((n, i) => ({ id: n.id, score: n.broker_g, rank: i + 1 }));
    const max = d3.max(meds, d => d.score) || 1;
    const sel = d3.select("#leaderboard").selectAll(".lbrow").data(meds, d => d.id);
    sel.exit().remove();
    const en = sel.enter().append("div").attr("class", "lbrow").on("click", (ev, d) => pinNode(d.id));
    en.append("div").attr("class", "lbbar"); en.append("span").attr("class", "lbflag"); en.append("span").attr("class", "lbname"); en.append("span").attr("class", "lbpct");
    const rows = en.merge(sel).classed("pak", d => d.id === FOCUS);
    rows.select(".lbbar").transition().duration(dur).style("width", d => (10 + 90 * d.score / max) + "%");
    rows.select(".lbflag").style("background", d => meta.get(d.id) ? meta.get(d.id).color : "#888");
    rows.select(".lbname").text(d => pretty(d.id));
    rows.select(".lbpct").text(d => Math.round(100 * d.score / max));
    d3.select("#leaderboard").selectAll(".lbrow").sort((a, b) => a.rank - b.rank);
    drawSpark();
  }
  function drawSpark() {
    const s = d3.select("#spark"), w = s.node().clientWidth || 220, h = 38; s.attr("viewBox", `0 0 ${w} ${h}`);
    const series = d3.range(NW).map(wk => { const n = D.frames[`${state.outlet}|${state.mode}|${wk}`].nodes.find(x => x.id === FOCUS); return { wk, v: n ? n.broker_g : 0 }; });
    const x = d3.scaleLinear().domain([0, NW - 1]).range([2, w - 2]);
    const y = d3.scaleLinear().domain([0, d3.max(series, d => d.v) || 1]).range([h - 3, 4]);
    const cs = getComputedStyle(document.body), acc = cs.getPropertyValue("--accent").trim();
    let ar = s.selectAll("path.ar").data([series]); ar = ar.enter().append("path").attr("class", "ar").merge(ar).attr("fill", "color-mix(in srgb," + acc + " 18%, transparent)").attr("d", d3.area().x(d => x(d.wk)).y0(h).y1(d => y(d.v)));
    let pa = s.selectAll("path.ln").data([series]); pa = pa.enter().append("path").attr("class", "ln").attr("fill", "none").attr("stroke-width", 1.6).merge(pa).attr("stroke", acc).attr("d", d3.line().x(d => x(d.wk)).y(d => y(d.v)));
    let bl = s.selectAll("line.b").data([bWeek]); bl = bl.enter().append("line").attr("class", "b").attr("stroke", "var(--faint)").attr("stroke-dasharray", "2,2").merge(bl).attr("x1", x(bWeek)).attr("x2", x(bWeek)).attr("y1", 0).attr("y2", h);
    let dot = s.selectAll("circle.c").data([series[state.week]]); dot = dot.enter().append("circle").attr("class", "c").attr("r", 3).merge(dot).attr("fill", acc).attr("cx", d => x(d.wk)).attr("cy", d => y(d.v));
  }

  // ---------- citation spotlight (GENUINE mediation only) ----------
  // a real brokering act: a third party (not a combatant) taking a mediating action.
  const isMediation = e => !PRINCIPALS.has(e.source) && e.polarity === "positive" &&
    MED_VERBS.some(v => (e.verb || "").toLowerCase().includes(v));
  const qsort = (a, b) => {
    const ga = (a.quote.length >= 45 && a.quote.length <= 220) ? 0 : 1, gb = (b.quote.length >= 45 && b.quote.length <= 220) ? 0 : 1;
    return ga - gb || b.date.localeCompare(a.date);
  };
  function pickHero(pool, focus) {
    const med = pool.filter(isMediation);
    if (!med.length) return null;
    const strict = med.filter(e => PRINCIPALS.has(e.target));   // mediating TOWARD a combatant
    let cand = strict.length ? strict : med;
    if (focus) { const f = cand.filter(e => e.source === focus || e.target === focus); if (f.length) cand = f; }
    return cand.slice().sort(qsort)[0];
  }
  function heroForWeek() {
    if (state.pin) {  // pinned: show that actor/tie's best mediation, else its latest positive line
      const f = state.pin.type === "node" ? (e => e.source === state.pin.id || e.target === state.pin.id)
        : (() => { const [a, b] = state.pin.key.split("|"); return e => (e.source === a && e.target === b) || (e.source === b && e.target === a); })();
      const pool = D.events.filter(e => e.outlet === state.outlet && e.week <= state.week && f(e) && polOk(valPol(e)));
      return pickHero(pool, state.pin.type === "node" ? state.pin.id : null)
        || pool.filter(e => e.polarity === "positive").sort((a, b) => b.date.localeCompare(a.date))[0] || null;
    }
    const wk = D.events.filter(e => e.outlet === state.outlet && e.week === state.week && polOk(valPol(e)));
    // most relevant mediation this week; else the most recent genuine mediation so far
    return pickHero(wk, leadBroker())
      || pickHero(D.events.filter(e => e.outlet === state.outlet && e.week <= state.week && polOk(valPol(e))), leadBroker());
  }
  function updateSpotlight() {
    const h = heroForWeek();
    const key = h ? (h.date + h.source + h.target + h.quote.slice(0, 16)) : null;
    if (key === state.heroKey) return;
    state.heroKey = key;
    const wrap = document.getElementById("spotlight");
    if (!h) { wrap.innerHTML = ""; return; }
    wrap.innerHTML = `<div class="card ${valPol(h)} spot-anim">
      <div class="topline"><span class="dyad">${esc(pretty(h.source))}<span class="arrow">→</span>${esc(pretty(h.target))}</span>
      <span class="verb">${esc(h.verb)}</span>
      <span class="when">${new Date(h.date).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })} · ${h.outlet[0].toUpperCase() + h.outlet.slice(1)}</span></div>
      <div class="quote">&ldquo;${esc(h.quote)}&rdquo;</div>
      ${h.url ? `<div class="src"><a href="${esc(h.url)}" target="_blank" rel="noopener">${esc((h.headline || "Read the source").slice(0, 90))} ↗</a></div>` : ""}</div>`;
  }

  // ---------- other mentions (side list) ----------
  function drawMentions() {
    const lo = state.mode === "window" ? Math.max(0, state.week - 3) : 0;
    let evs = D.events.filter(e => e.outlet === state.outlet && e.week <= state.week && e.week >= lo && polOk(valPol(e)));
    if (state.pin?.type === "node") evs = evs.filter(e => e.source === state.pin.id || e.target === state.pin.id);
    if (state.pin?.type === "dyad") { const [a, b] = state.pin.key.split("|"); evs = evs.filter(e => (e.source === a && e.target === b) || (e.source === b && e.target === a)); }
    evs = evs.sort((a, b) => b.date.localeCompare(a.date)).slice(0, 14);
    document.getElementById("ment-clear").textContent = state.pin ? "clear ✕" : "";
    const sel = d3.select("#mentlist").selectAll(".ment").data(evs, d => d.date + d.source + d.target + d.quote.slice(0, 10));
    sel.exit().remove();
    sel.enter().append("div").merge(sel).attr("class", d => "ment " + valPol(d))
      .html(d => `<div class="d">${esc(pretty(d.source))}<span class="a"> → </span>${esc(pretty(d.target))}</div><div class="m">${esc(d.verb)} · ${new Date(d.date).toLocaleDateString("en-GB", { day: "numeric", month: "short" })}</div>`)
      .on("click", (ev, d) => pinDyad(d.source, d.target));
    d3.select("#mentlist").selectAll(".ment").sort((a, b) => b.date.localeCompare(a.date));
  }

  function pinNode(id) { state.pin = (state.pin?.type === "node" && state.pin.id === id) ? null : { type: "node", id }; state.heroKey = null; highlight(); render(true); }
  function pinDyad(a, b) { const k = dyadKey(a, b); state.pin = (state.pin?.type === "dyad" && state.pin.key === k) ? null : { type: "dyad", key: k }; state.heroKey = null; highlight(); render(true); }
  function clearPin() { if (state.pin) { state.pin = null; state.heroKey = null; highlight(); render(true); } }

  // ---------- timeline ----------
  function buildTimeline() {
    const seg = document.getElementById("chapseg"); seg.innerHTML = "";
    D.chapters.forEach(c => {
      const t = document.createElement("div"); t.className = "chaptick"; t.style.left = (100 * c.start_week / (NW - 1)) + "%"; seg.appendChild(t);
      const l = document.createElement("div"); l.className = "chaplabel"; l.id = "cl-" + c.id; l.style.left = (100 * c.start_week / (NW - 1)) + "%"; l.textContent = c.title;
      l.onclick = () => { state.week = c.start_week; sync(); render(false); }; seg.appendChild(l);
    });
    const bm = document.createElement("div"); bm.className = "boundary-mark"; bm.style.left = (100 * bWeek / (NW - 1)) + "%"; bm.innerHTML = '<span class="lab">8 Apr · talks</span>'; seg.appendChild(bm);
  }
  function updateTimeline() {
    const p = 100 * state.week / (NW - 1);
    document.getElementById("progress").style.width = p + "%";
    document.getElementById("playhead").style.left = p + "%";
    const cur = chapterAt(state.week).id;
    D.chapters.forEach(c => document.getElementById("cl-" + c.id).classList.toggle("active", c.id === cur));
  }

  // ---------- controls ----------
  function sync() { document.getElementById("scrubber").value = state.week; }
  document.querySelectorAll(".seg").forEach(seg => seg.addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    seg.querySelectorAll("button").forEach(x => x.classList.toggle("active", x === b));
    const g = seg.dataset.group, v = b.dataset.val;
    if (g === "outlet") { state.outlet = v; state.chapId = null; document.getElementById("spotlight").innerHTML = ""; }
    else if (g === "mode") state.mode = v;
    else if (g === "pol") state.pol = v;
    else if (g === "run") { state.storyMode = v === "story"; if (state.playing) play(true); return; }
    else if (g === "speed") { state.speed = +v; if (state.playing) play(true); return; }
    state.heroKey = null; render(false);
  }));
  const scrub = document.getElementById("scrubber"); scrub.max = NW - 1;
  scrub.addEventListener("input", () => { state.week = +scrub.value; render(false); });
  const playBtn = document.getElementById("play");
  let timer = null;
  const baseInterval = () => (state.storyMode ? 1500 : 600) / state.speed;
  function emphasizeBroker() { const lb = leadBroker(); if (lb && pos.has(lb)) { flash(pos.get(lb)); setTimeout(() => flash(pos.get(lb)), 320); } }
  function tick() {
    const prevChap = chapterAt(state.week).id;
    state.week = state.week >= NW - 1 ? 0 : state.week + 1;
    sync(); render(false);
    if (state.playing && state.storyMode && chapterAt(state.week).id !== prevChap && !state.holding) {  // pause on a new chapter so you can read it
      state.holding = true; clearInterval(timer); timer = null; emphasizeBroker();
      setTimeout(() => { state.holding = false; if (state.playing) timer = setInterval(tick, baseInterval()); }, 3200);
    }
  }
  function play(on) { state.playing = on; playBtn.classList.toggle("playing", on); playBtn.innerHTML = on ? "&#10073;&#10073;&nbsp; Pause" : "&#9654;&nbsp; Play"; if (timer) clearInterval(timer); timer = on ? setInterval(tick, baseInterval()) : null; }
  playBtn.addEventListener("click", () => play(!state.playing));
  svg.on("click", clearPin);
  document.getElementById("ment-clear").addEventListener("click", clearPin);
  window.addEventListener("keydown", e => {
    if (e.code === "Space") { e.preventDefault(); play(!state.playing); }
    else if (e.code === "ArrowRight") { state.week = Math.min(NW - 1, state.week + 1); sync(); render(false); }
    else if (e.code === "ArrowLeft") { state.week = Math.max(0, state.week - 1); sync(); render(false); }
  });

  // ---------- theme (light default) ----------
  function setTheme(t) {
    document.body.setAttribute("data-theme", t);
    try { localStorage.setItem("viz-theme", t); } catch (e) {}
    document.getElementById("theme").innerHTML = t === "dark" ? "&#9790;" : "&#9728;";
    readColors(); render(true);
  }
  document.getElementById("theme").addEventListener("click", () => setTheme(document.body.getAttribute("data-theme") === "dark" ? "light" : "dark"));

  // ---------- legend ----------
  (function legend() {
    const order = [["Broker", "Pakistan"], ["US_bloc", "US bloc"], ["Iran_bloc", "Iran bloc"], ["E3", "Europe"], ["Mediators", "Mediators"], ["Arab_states", "Arab states"], ["International", "UN/IAEA"]];
    const el = document.getElementById("legend");
    order.forEach(([b, lab]) => { const d = document.createElement("div"); d.innerHTML = `<span class="sw" style="background:${META.bloc_colors[b]}"></span>${lab}`; el.appendChild(d); });
  })();

  let saved = "light"; try { saved = localStorage.getItem("viz-theme") || "light"; } catch (e) {}
  document.body.setAttribute("data-theme", saved);
  document.getElementById("theme").innerHTML = saved === "dark" ? "&#9790;" : "&#9728;";
  readColors();
  buildTimeline();
  window.addEventListener("resize", resize);
  resize();
})();
