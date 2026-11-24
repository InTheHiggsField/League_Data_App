/*
 * Draft Helper front end. All data comes from data.js (window.SITE_DATA),
 * made by export_site.py. Nothing is sent anywhere. The only outside requests
 * are champion icons from Riot's Data Dragon CDN.
 *
 * Data layout per tier group and role:
 *   combos:   [championId, games, wins, spread, conservativeSpread, matchupsUsed,
 *              physicalShare, magicShare, trueShare]
 *   matchups: [championId, opponentId, games, wins]   (both combos >= 100 games, matchup >= 50)
 *
 * Champion builds live in builds_<tier group>.js (window.BUILD_DATA), loaded only
 * when a champion profile is opened. Per role -> champion -> enemy comp type
 * (gen / ad / ap / mixed), see export_builds() in export_site.py.
 */
(function () {
  "use strict";

  const D = window.SITE_DATA;
  // First picks must have at least this winrate. 0.48 rather than 0.5, because some
  // safe blind picks sit just under 50% because of who plays them (e.g. Ahri at 49.8%).
  const MIN_BLIND_WR = 0.48;
  // How many champions per role get the "Best pick" badge (highest bad-matchup winrate).
  const BEST_PICKS = 5;
  const ROLES = [["top", "Top"], ["jungle", "Jungle"], ["mid", "Mid"], ["bot", "Bot"], ["support", "Support"]];
  const T = D ? D.thresholds : {};
  const $ = (id) => document.getElementById(id);

  if (!D) {
    $("data-info").textContent = "No data found. Run `python export_site.py` to create docs/data.js.";
    return;
  }

  // ------------------------------------------------------------- state
  // Kept in the URL (#role=mid&tier=all&mode=counter&enemy=238, or ...&champ=103 for a
  // champion profile) so a view can be bookmarked, shared, and reached with the back button.
  const COMPS = [["gen", "General"], ["ad", "vs AD-heavy"], ["ap", "vs AP-heavy"], ["mixed", "vs Mixed"]];
  const state = { role: "mid", tier: "all", mode: "first", enemy: null, champ: null, comp: "gen", bestFirst: false };
  function readHash() {
    const h = new URLSearchParams(location.hash.slice(1));
    if (ROLES.some(([r]) => r === h.get("role"))) state.role = h.get("role");
    if (D.groups[h.get("tier")]) state.tier = h.get("tier");
    if (["first", "counter"].includes(h.get("mode"))) state.mode = h.get("mode");
    state.enemy = h.get("enemy") ? Number(h.get("enemy")) : null;
    state.champ = h.get("champ") ? Number(h.get("champ")) : null;
    state.comp = COMPS.some(([c]) => c === h.get("comp")) ? h.get("comp") : "gen";
    state.bestFirst = h.get("sort") === "best";
  }
  function hashFor(extra = {}) {
    const s = { ...state, ...extra };
    const h = new URLSearchParams({ role: s.role, tier: s.tier, mode: s.mode });
    if (s.enemy) h.set("enemy", s.enemy);
    if (s.bestFirst) h.set("sort", "best");
    if (s.champ) { h.set("champ", s.champ); if (s.comp !== "gen") h.set("comp", s.comp); }
    return "#" + h.toString();
  }
  function writeHash() { history.replaceState(null, "", hashFor()); }
  // Link to a champion's profile in the current role and rank group.
  function profileHref(id) { return hashFor({ champ: id, comp: "gen" }); }

  // ------------------------------------------------------------ helpers
  let ddVersion = null;   // Data Dragon version for icons, fetched below
  const champ = (id) => D.champions[String(id)] || { name: "#" + id, api: "" };
  const pct = (x, digits = 1) => (100 * x).toFixed(digits) + "%";
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const norm = (s) => s.toLowerCase().normalize("NFD").replace(/[^a-z0-9]/g, "");

  function icon(id) {
    const c = champ(id);
    if (!ddVersion || !c.api) return '<img alt="">';
    return `<img alt="" loading="lazy" src="https://ddragon.leagueoflegends.com/cdn/${ddVersion}/img/champion/${encodeURIComponent(c.api)}.png" onerror="this.style.visibility='hidden'">`;
  }
  function wrClass(wr) { return wr >= 0.5 ? "good" : "bad"; }

  // Standard normal CDF (Abramowitz & Stegun 7.1.26 approximation of erf).
  function normCdf(x) {
    const t = 1 / (1 + 0.3275911 * Math.abs(x) / Math.SQRT2);
    const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t
      * Math.exp(-x * x / 2);
    return x >= 0 ? (1 + y) / 2 : (1 - y) / 2;
  }

  // 95% range for a winrate (Wilson interval), shown as a tooltip.
  function wilson(wins, games) {
    if (!games) return [0, 0];
    const z = 1.96, p = wins / games, d = 1 + z * z / games;
    const c = (p + z * z / (2 * games)) / d;
    const h = (z * Math.sqrt(p * (1 - p) / games + z * z / (4 * games * games))) / d;
    return [c - h, c + h];
  }
  function wrCell(wins, games) {
    const wr = wins / games, [lo, hi] = wilson(wins, games);
    return `<td class="num ${wrClass(wr)}" title="95% range: ${pct(lo)} – ${pct(hi)}">${pct(wr)}</td>`;
  }
  function smallBadge(games) {
    return games < T.small
      ? `<span class="badge small-sample" title="Fewer than ${T.small} games recorded">Small sample size</span>` : "";
  }
  function champCell(id, games, extra = "") {
    return `<td><div class="champ"><a class="champ-link" href="${profileHref(id)}" title="Open ${esc(champ(id).name)}'s profile">${icon(id)}<span class="name">${esc(champ(id).name)}</span></a>${extra}${smallBadge(games)}</div></td>`;
  }
  function champChip(id, inner) {
    return `<a class="chip" href="${profileHref(id)}" title="Open ${esc(champ(id).name)}'s profile">${icon(id)}${inner}</a>`;
  }

  function roleData() { return D.groups[state.tier].roles[state.role]; }
  function combosById() {
    const m = new Map();
    for (const c of roleData().combos) m.set(c[0], { id: c[0], games: c[1], wins: c[2], wr: c[2] / c[1], spread: c[3], safe: c[4], k: c[5],
                                                    phys: c[6] || 0, magic: c[7] || 0, tru: c[8] || 0 });
    return m;
  }

  // ------------------------------------------------------- first picking
  function renderFirst() {
    const combos = combosById();
    const matchups = roleData().matchups;
    const pool = [...combos.values()].filter((c) => c.wr > MIN_BLIND_WR && c.safe != null);
    const hidden = combos.size - pool.length;

    // Where the spread sits, not just how wide it is:
    //   floor = winrate - spread: roughly the winrate in one of its harder matchups (about
    //           the worst sixth), so a high winrate and a low spread both push it up.
    //   fav   = share of matchups expected to be above 50%, treating matchup winrates as
    //           spread out around the champion's winrate (normal curve, width = spread).
    for (const c of pool) {
      c.floor = c.wr - c.safe;
      c.fav = c.safe > 0 ? normCdf((c.wr - 0.5) / c.safe) : (c.wr > 0.5 ? 1 : 0);
    }
    // "Best pick": the BEST_PICKS champions with the highest bad-matchup winrate.
    const bestIds = new Set([...pool].sort((a, b) => b.floor - a.floor).slice(0, BEST_PICKS).map((c) => c.id));
    for (const c of pool) c.best = bestIds.has(c.id);

    // Default: lowest conservative spread first (ties: higher winrate).
    // "Best picks first": highest bad-matchup winrate first.
    if (state.bestFirst) pool.sort((a, b) => b.floor - a.floor);
    else pool.sort((a, b) => a.safe - b.safe || b.wr - a.wr);
    $("best-toggle").setAttribute("aria-pressed", state.bestFirst);
    $("best-toggle").textContent = state.bestFirst ? "★ Best picks first (on)" : "★ Show best picks first";

    const rows = pool.map((c, i) => {
      const worst = matchups.filter((m) => m[0] === c.id)
        .map((m) => ({ opp: m[1], games: m[2], wr: m[3] / m[2] }))
        .sort((a, b) => a.wr - b.wr).slice(0, 3);
      const chips = worst.length
        ? worst.map((w) => champChip(w.opp, `${esc(champ(w.opp).name)} <span class="${wrClass(w.wr)}">${pct(w.wr, 0)}</span>`)).join("")
        : `<span class="chip empty">No matchup with ${T.matchup}+ games yet</span>`;
      return `<tr>
        <td class="num rank">${i + 1}</td>
        ${champCell(c.id, c.games, c.best ? '<span class="badge best">Best pick</span>' : "")}
        ${wrCell(c.wins, c.games)}
        <td class="num" title="Point estimate ±${pct(c.spread)} across ${c.k} lane opponents">±${pct(c.safe)}</td>
        <td class="num floor" title="Winrate minus spread: roughly how it does in one of its harder matchups">${pct(c.floor)}</td>
        <td class="num" title="Share of matchups expected to be above 50%">${pct(c.fav, 0)}</td>
        <td class="num">${c.games.toLocaleString()}</td>
        <td><div class="chips">${chips}</div></td>
      </tr>`;
    });
    $("first-body").innerHTML = rows.join("") ||
      `<tr><td colspan="8" class="muted">No ${state.role} champion is above ${pct(MIN_BLIND_WR, 0)} with ${T.combo}+ games in this rank group yet.</td></tr>`;
    $("first-note").textContent = hidden
      ? `${hidden} other ${state.role} champion${hidden === 1 ? "" : "s"} with ${T.combo}+ games ${hidden === 1 ? "is" : "are"} hidden for being at or below ${pct(MIN_BLIND_WR, 0)} winrate.`
      : "";
  }

  // ----------------------------------------------------- counter picking
  const input = $("enemy-input"), list = $("enemy-list");
  let options = [], active = -1;

  function enemyChoices() {
    return [...combosById().values()].sort((a, b) => champ(a.id).name.localeCompare(champ(b.id).name));
  }

  function highlight(name, q) {
    if (!q) return esc(name);
    // Find where the typed letters start in the name, ignoring spaces and punctuation.
    const map = []; let flat = "";
    [...name].forEach((ch, i) => { const n = norm(ch); for (const x of n) { flat += x; map.push(i); } });
    const at = flat.indexOf(q);
    if (at < 0) return esc(name);
    const s = map[at], e = map[at + q.length - 1] + 1;
    return esc(name.slice(0, s)) + "<mark>" + esc(name.slice(s, e)) + "</mark>" + esc(name.slice(e));
  }

  function openList() {
    const q = norm(input.value);
    const all = enemyChoices();
    // Names starting with what was typed come first, then names containing it.
    const starts = all.filter((c) => norm(champ(c.id).name).startsWith(q));
    const contains = all.filter((c) => !starts.includes(c) && norm(champ(c.id).name).includes(q));
    options = starts.concat(contains);
    active = options.length ? 0 : -1;
    // How many counters each enemy has (matchups with enough games), shown in the list.
    const counters = new Map();
    for (const m of roleData().matchups) counters.set(m[1], (counters.get(m[1]) || 0) + 1);
    const meta = (c) => {
      const n = counters.get(c.id) || 0;
      return n ? `${n} counter${n === 1 ? "" : "s"}` : "not enough matchup data yet";
    };
    list.innerHTML = options.length
      ? options.map((c, i) => `<li role="option" data-i="${i}" aria-selected="${i === active}">${icon(c.id)}<span>${highlight(champ(c.id).name, q)}</span><span class="meta">${meta(c)}</span></li>`).join("")
      : `<li class="none">No ${state.role} champion with ${T.combo}+ games matches "${esc(input.value)}"</li>`;
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }
  function closeList() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
  }
  function moveActive(step) {
    if (!options.length) return;
    active = (active + step + options.length) % options.length;
    list.querySelectorAll("li[role=option]").forEach((li, i) => li.setAttribute("aria-selected", i === active));
    list.children[active].scrollIntoView({ block: "nearest" });
  }
  function choose(id) {
    state.enemy = id;
    input.value = champ(id).name;
    closeList();
    writeHash();
    renderCounter();
  }

  input.addEventListener("input", openList);
  input.addEventListener("focus", () => { input.select(); openList(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); list.hidden ? openList() : moveActive(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); moveActive(-1); }
    else if (e.key === "Enter") { e.preventDefault(); if (active >= 0 && options[active]) choose(options[active].id); }
    else if (e.key === "Escape") { closeList(); }
  });
  list.addEventListener("mousedown", (e) => {   // mousedown fires before the input loses focus
    const li = e.target.closest("li[data-i]");
    if (li) { e.preventDefault(); choose(options[Number(li.dataset.i)].id); }
  });
  input.addEventListener("blur", () => setTimeout(closeList, 100));

  function renderCounter() {
    const combos = combosById();
    const enemy = state.enemy != null ? combos.get(state.enemy) : null;
    const card = $("enemy-card"), results = $("counter-results"), note = $("counter-note");

    if (!enemy) {
      if (state.enemy != null) {   // chosen earlier, but not shown for this role/rank
        note.textContent = `${champ(state.enemy).name} doesn't have ${T.combo}+ ${state.role} games in this rank group yet.`;
      } else {
        note.textContent = "";
      }
      if (document.activeElement !== input) input.value = state.enemy != null ? champ(state.enemy).name : "";
      card.hidden = true; results.hidden = true;
      return;
    }

    input.value = champ(enemy.id).name;
    card.hidden = false;
    card.innerHTML = `<a href="${profileHref(enemy.id)}" title="Open ${esc(champ(enemy.id).name)}'s profile">${icon(enemy.id)}</a><div><div class="title"><a class="plain-link" href="${profileHref(enemy.id)}">${esc(champ(enemy.id).name)}</a> ${smallBadge(enemy.games)}</div>
      <div class="muted small">${state.role} · ${pct(enemy.wr)} winrate · ${enemy.games.toLocaleString()} games · ${esc(D.groups[state.tier].label)}</div></div>`;

    const rows = roleData().matchups.filter((m) => m[1] === enemy.id)
      .map((m) => ({ id: m[0], games: m[2], wins: m[3], wr: m[3] / m[2] }))
      .sort((a, b) => b.wr - a.wr);

    $("vs-header").textContent = "Winrate vs " + champ(enemy.id).name;
    $("counter-body").innerHTML = rows.map((r, i) => {
      const own = combos.get(r.id);
      return `<tr>
        <td class="num rank">${i + 1}</td>
        ${champCell(r.id, own.games)}
        ${wrCell(r.wins, r.games)}
        <td class="num">${r.games.toLocaleString()}</td>
        <td class="num muted">${pct(own.wr)}</td>
      </tr>`;
    }).join("");
    results.hidden = rows.length === 0;
    note.textContent = rows.length
      ? `Only matchups with ${T.matchup}+ games are listed. Hover a winrate for its 95% range.`
      : `No ${state.role} champion has ${T.matchup}+ games against ${champ(enemy.id).name} in this rank group yet.`;
  }

  // --------------------------------------------------- champion profile
  const B = () => window.BUILD_DATA || {};
  const itemName = (id) => (D.items && D.items[String(id)]) || "Item " + id;
  function itemIcon(id, size = "") {
    const name = esc(itemName(id));
    const src = ddVersion ? `https://ddragon.leagueoflegends.com/cdn/${ddVersion}/img/item/${id}.png` : "";
    return `<img class="item-icon ${size}" alt="${name}" title="${name}" loading="lazy" ${src ? `src="${src}"` : ""} onerror="this.style.visibility='hidden'">`;
  }
  const keycap = (k) => `<span class="keycap">${k}</span>`;
  const MIN_BUILD = (D.builds && D.builds.min_games) || T.combo;

  // Build files are big, so each rank group's file is only fetched when a profile needs it.
  const loading = {};
  function loadBuilds(group, done) {
    if (B()[group]) return done(true);
    if (!D.builds || !D.builds.available) return done(false);
    if (!loading[group]) {
      loading[group] = [];
      const s = document.createElement("script");
      s.src = `builds_${group}.js?v=${encodeURIComponent(D.generated)}`;
      s.onload = () => loading[group].splice(0).forEach((f) => f(true));
      s.onerror = () => { loading[group].splice(0).forEach((f) => f(false)); delete loading[group]; };
      document.head.appendChild(s);
    }
    loading[group].push(done);
  }

  function rolesFor(id) {
    return ROLES.filter(([r]) => D.groups[state.tier].roles[r].combos.some((c) => c[0] === id));
  }

  function renderProfile() {
    const id = state.champ, name = esc(champ(id).name);
    const c = combosById().get(id);
    const g = D.groups[state.tier];
    const roles = rolesFor(id);
    const roleLinks = roles.map(([r, label]) =>
      `<a class="role-pill${r === state.role ? " on" : ""}" href="${hashFor({ role: r, comp: "gen" })}">${label}</a>`).join("");

    if (!c) {
      $("profile").innerHTML = `<div class="profile-head"><div class="portrait">${icon(id)}</div><div class="head-main"><div class="title">${name}</div>
        <div class="muted small">${roles.length
          ? `Not enough ${state.role} games for ${name} in ${esc(g.label)}. Roles with ${T.combo}+ games:`
          : `No role has ${T.combo}+ games in ${esc(g.label)} yet.`}</div>
        <div class="role-pills">${roleLinks}</div></div></div>`;
      return;
    }

    const bans = (g.bans && g.bans[String(id)]) || 0;
    const matchups = roleData().matchups.filter((m) => m[0] === id)
      .map((m) => ({ opp: m[1], games: m[2], wr: m[3] / m[2] }));
    const best = [...matchups].sort((a, b) => b.wr - a.wr).slice(0, 5);
    const worst = [...matchups].sort((a, b) => a.wr - b.wr).slice(0, 5);
    const muChips = (list) => list.length
      ? list.map((m) => champChip(m.opp, `${esc(champ(m.opp).name)} <span class="${wrClass(m.wr)}">${pct(m.wr, 0)}</span> <span class="muted small">${m.games}</span>`)).join("")
      : `<span class="chip empty">No matchup with ${T.matchup}+ games yet</span>`;
    const [lo, hi] = wilson(c.wins, c.games);
    const dmg = c.phys + c.magic + c.tru;

    $("profile").innerHTML = `
      <div class="profile-head">
        <div class="portrait">${icon(id)}</div>
        <div class="head-main">
          <div class="title">${name} ${smallBadge(c.games)}</div>
          <div class="role-pills">${roleLinks}</div>
          <div class="stat-row">
            <div class="stat"><span class="stat-val ${wrClass(c.wr)}" title="95% range: ${pct(lo)} – ${pct(hi)}">${pct(c.wr)}</span><span class="stat-label">Winrate</span></div>
            <div class="stat"><span class="stat-val">${pct(c.games / g.games)}</span><span class="stat-label">Pick rate</span></div>
            <div class="stat"><span class="stat-val">${pct(bans / g.games)}</span><span class="stat-label">Ban rate</span></div>
            <div class="stat"><span class="stat-val">${c.games.toLocaleString()}</span><span class="stat-label">Games</span></div>
            <div class="stat"><span class="stat-val">${c.safe != null ? "±" + pct(c.safe) : "–"}</span><span class="stat-label">Matchup spread</span></div>
          </div>
        </div>
      </div>

      <h2 class="swoosh">Damage Profile</h2>
      ${dmg ? `<div class="dmg-bar" title="Share of damage dealt to champions">
        <span class="dmg phys" style="width:${100 * c.phys / dmg}%"></span>
        <span class="dmg magic" style="width:${100 * c.magic / dmg}%"></span>
        <span class="dmg true" style="width:${100 * c.tru / dmg}%"></span>
      </div>
      <div class="dmg-legend"><span><i class="sw phys"></i>Physical ${pct(c.phys / dmg, 0)}</span>
        <span><i class="sw magic"></i>Magic ${pct(c.magic / dmg, 0)}</span>
        <span><i class="sw true"></i>True ${pct(c.tru / dmg, 0)}</span></div>`
        : `<p class="note">No damage data yet. Run <code>python Data_Gather.py backfill-damage</code>.</p>`}

      <h2 class="swoosh">Recommended Build</h2>
      <div id="build-area"><p class="note">Loading build data…</p></div>

      <h2 class="swoosh">Lane Matchups</h2>
      <div class="mu-grid">
        <div><div class="field-label">Best against</div><div class="chips">${muChips(best)}</div></div>
        <div><div class="field-label">Struggles against</div><div class="chips">${muChips(worst)}</div></div>
      </div>
      <p class="note">Matchups with ${T.matchup}+ games in ${esc(g.label)}. The small number is games played.</p>`;

    const tier = state.tier, role = state.role;
    loadBuilds(tier, (ok) => {
      if (state.champ !== id || state.tier !== tier || state.role !== role || !$("build-area")) return;
      $("build-area").innerHTML = ok ? buildHtml(B()[tier]?.[role]?.[String(id)])
        : `<p class="note">Build data isn't available yet. Collect timelines, then run <code>python export_site.py</code>.</p>`;
    });
  }

  function buildHtml(entry) {
    if (!entry) return `<p class="note">No games with build data for this champion in this role and rank yet.</p>`;
    const cuts = D.builds;
    const tips = {
      gen: "All games",
      ad: `Enemy team expected to deal ${pct(1 - cuts.cut_ad, 0)}+ physical damage (the most physical 25% of teams)`,
      ap: `Enemy team expected to deal ${pct(cuts.cut_ap, 0)}+ magic damage (the most magical 25% of teams)`,
      mixed: "Enemy teams in between",
    };
    const tabs = COMPS.map(([k, label]) => {
      const n = entry[k] ? entry[k].n : 0;
      return `<a class="glossy small${k === state.comp ? " on" : ""}" href="${hashFor({ comp: k })}" title="${tips[k]}">${label} <span class="count">${n.toLocaleString()}</span></a>`;
    }).join("");
    const s = entry[state.comp] || { n: 0 };
    let body;
    if (s.n < MIN_BUILD) {
      body = `<p class="note">Not enough build data yet: ${s.n} game${s.n === 1 ? "" : "s"} with timelines (needs ${MIN_BUILD}).</p>`;
    } else {
      const wr = (w, n) => `<span class="${wrClass(w / n)}">${pct(w / n)}</span>`;
      const reached3 = s.slots[2] ? s.slots[2].r : 0;
      const path = s.path[0];
      const pathHtml = path ? `
        <div class="path">
          ${path[0].map((it, i) => `${i ? '<span class="arrow">›</span>' : ""}
            <div class="path-step">${itemIcon(it, "big")}<div class="step-name">${esc(itemName(it))}</div>
              <div class="muted small">${s.mins[i] != null ? "~" + s.mins[i] + " min" : ""}</div></div>`).join("")}
        </div>
        <div class="muted small">${pct(path[1] / reached3)} of games with 3+ items · ${wr(path[2], path[1])} winrate · ${path[1].toLocaleString()} games</div>
        ${s.path.slice(1).map((p) => `<div class="alt-path">${p[0].map((it) => itemIcon(it)).join('<span class="arrow sm">›</span>')}
          <span class="muted small">${pct(p[1] / reached3)} · ${wr(p[2], p[1])}</span></div>`).join("")}`
        : `<p class="note">Too few games reach 3 completed items.</p>`;

      const slotCols = s.slots.map((sl, i) => `
        <div class="slot-col"><div class="field-label">${["1st", "2nd", "3rd", "4th", "5th"][i]} item</div>
          ${sl.items.length ? sl.items.map((x) => `<div class="slot-item">${itemIcon(x[0])}<div>
            <div class="small">${esc(itemName(x[0]))}</div>
            <div class="muted small" title="${x[1]} games">${pct(x[1] / sl.r, 0)} · ${wr(x[2], x[1])}</div></div></div>`).join("")
            : `<div class="muted small">–</div>`}
        </div>`).join("");

      const sit = state.comp === "gen" ? "" : `
        <div class="build-block wide"><div class="field-label">Situational against this comp</div>
          ${s.sit && s.sit.length ? s.sit.map((x) => `<div class="slot-item">${itemIcon(x[0])}<div>
            <div class="small">${esc(itemName(x[0]))}</div>
            <div class="muted small">built in <strong>${pct(x[1], 0)}</strong> of these games vs ${pct(x[2], 0)} overall · ${wr(x[4], x[3])}</div></div></div>`).join("")
          : `<div class="muted small">Nothing stands out: the general build holds up against this comp.</div>`}
        </div>`;

      const sk = s.skill[0];
      body = `
        ${s.n < T.small ? `<p>${smallBadge(s.n)} <span class="muted small">${s.n} games with build data</span></p>` : ""}
        <div class="build-grid">
          <div class="build-block"><div class="field-label">Starting items</div>
            ${s.start.map((st) => `<div class="start-row">${st[0].map((it) => itemIcon(it)).join("")}
              <span class="muted small">${pct(st[1] / s.n, 0)} · ${wr(st[2], st[1])}</span></div>`).join("")}
          </div>
          <div class="build-block"><div class="field-label">Boots</div>
            ${s.boots.map((b) => `<div class="slot-item">${itemIcon(b[0])}<div><div class="small">${esc(itemName(b[0]))}</div>
              <div class="muted small">${pct(b[1] / s.n, 0)} · ${wr(b[2], b[1])} · ~${b[3]} min</div></div></div>`).join("")}
          </div>
          <div class="build-block"><div class="field-label">Skill order</div>
            ${sk ? `<div class="skills">${[...sk[0]].map(keycap).join('<span class="arrow sm">›</span>')}</div>
              <div class="muted small">Max order · ${pct(sk[1] / s.n, 0)} of games · ${wr(sk[2], sk[1])}</div>
              ${s.first3[0] ? `<div class="skills small-keys">${[...s.first3[0][0]].map(keycap).join("")}</div>
              <div class="muted small">First three levels</div>` : ""}` : `<div class="muted small">–</div>`}
          </div>
          <div class="build-block wide"><div class="field-label">Core build path</div>${pathHtml}</div>
          ${sit}
          <div class="build-block wide"><div class="field-label">Item choices by order</div>
            <div class="slot-grid">${slotCols}</div>
            <div class="muted small">Percent of games that finished that many items · winrate. Late items are biased toward long games.</div>
          </div>
        </div>`;
    }
    return `<div class="build-tabs">${tabs}</div>${body}`;
  }

  // ------------------------------------------------------------ controls
  function renderControls() {
    $("role-buttons").innerHTML = ROLES.map(([r, label]) =>
      `<button data-role="${r}" role="tab" aria-selected="${r === state.role}">${label}</button>`).join("");
    document.querySelectorAll("#mode-buttons button").forEach((b) =>
      b.setAttribute("aria-selected", !state.champ && b.dataset.mode === state.mode));
    $("tier-select").value = state.tier;
    const roleName = ROLES.find(([r]) => r === state.role)[1] + (state.role === "jungle" || state.role === "support" ? "" : " Lane");
    $("role-title").textContent = state.champ ? `${champ(state.champ).name} · ${roleName}` : roleName;
    $("min-wr").textContent = pct(MIN_BLIND_WR, 0);
    const g = D.groups[state.tier];
    $("data-info").textContent =
      `${g.games.toLocaleString()} ranked solo games · ${g.label} · patch ${D.patches.join(", ")} · updated ${D.generated}`;
  }

  function render() {
    renderControls();
    $("view-profile").hidden = !state.champ;
    $("view-first").hidden = !!state.champ || state.mode !== "first";
    $("view-counter").hidden = !!state.champ || state.mode !== "counter";
    if (state.champ) renderProfile();
    else if (state.mode === "first") renderFirst();
    else renderCounter();
  }

  $("tier-select").innerHTML = Object.entries(D.groups)
    .map(([k, g]) => `<option value="${k}">${esc(g.label)}</option>`).join("");
  $("tier-select").addEventListener("change", (e) => { state.tier = e.target.value; writeHash(); render(); });
  $("role-buttons").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-role]");
    if (b) { state.role = b.dataset.role; writeHash(); render(); }
  });
  $("mode-buttons").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]");
    if (!b) return;
    state.mode = b.dataset.mode; state.champ = null; writeHash(); render();
    if (state.mode === "counter" && state.enemy == null) input.focus();
  });
  $("best-toggle").addEventListener("click", () => {
    state.bestFirst = !state.bestFirst; writeHash(); renderFirst();
  });
  $("back-button").addEventListener("click", () => {
    state.champ = null; history.pushState(null, "", hashFor()); render();
  });
  window.addEventListener("hashchange", () => {
    const before = state.champ;
    readHash(); render();
    if (state.champ !== before) window.scrollTo({ top: 0 });   // new champion: start at the top
  });

  $("rules").textContent =
    `Only champion + role combos with ${T.combo}+ recorded games are shown; under ${T.small} games is marked ` +
    `"small sample size". Winrates count the whole game: the match data says which team won, not who won the lane.`;

  readHash();
  render();

  // Champion icons: ask Data Dragon for the current version, then re-render with images.
  fetch("https://ddragon.leagueoflegends.com/api/versions.json")
    .then((r) => r.json())
    .then((v) => { ddVersion = v[0]; render(); })
    .catch(() => { /* no icons offline; everything else still works */ });
})();
