/*
 * Draft Helper front end. All data comes from data.js (window.SITE_DATA),
 * made by export_site.py. Nothing is sent anywhere. The only outside requests
 * are champion icons from Riot's Data Dragon CDN.
 *
 * Data layout per tier group and role:
 *   combos:   [championId, games, wins, spread, conservativeSpread, matchupsUsed]
 *   matchups: [championId, opponentId, games, wins]   (both combos >= 100 games, matchup >= 50)
 */
(function () {
  "use strict";

  const D = window.SITE_DATA;
  const ROLES = [["top", "Top"], ["jungle", "Jungle"], ["mid", "Mid"], ["bot", "Bot"], ["support", "Support"]];
  const T = D ? D.thresholds : {};
  const $ = (id) => document.getElementById(id);

  if (!D) {
    $("data-info").textContent = "No data found. Run `python export_site.py` to create docs/data.js.";
    return;
  }

  // ------------------------------------------------------------- state
  // Kept in the URL (#role=mid&tier=all&mode=counter&enemy=238) so a view can be bookmarked or shared.
  const state = { role: "mid", tier: "all", mode: "first", enemy: null };
  function readHash() {
    const h = new URLSearchParams(location.hash.slice(1));
    if (ROLES.some(([r]) => r === h.get("role"))) state.role = h.get("role");
    if (D.groups[h.get("tier")]) state.tier = h.get("tier");
    if (["first", "counter"].includes(h.get("mode"))) state.mode = h.get("mode");
    state.enemy = h.get("enemy") ? Number(h.get("enemy")) : null;
  }
  function writeHash() {
    const h = new URLSearchParams({ role: state.role, tier: state.tier, mode: state.mode });
    if (state.mode === "counter" && state.enemy) h.set("enemy", state.enemy);
    history.replaceState(null, "", "#" + h.toString());
  }

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
    return `<td><div class="champ">${icon(id)}<span class="name">${esc(champ(id).name)}</span>${extra}${smallBadge(games)}</div></td>`;
  }

  function roleData() { return D.groups[state.tier].roles[state.role]; }
  function combosById() {
    const m = new Map();
    for (const c of roleData().combos) m.set(c[0], { id: c[0], games: c[1], wins: c[2], wr: c[2] / c[1], spread: c[3], safe: c[4], k: c[5] });
    return m;
  }

  // ------------------------------------------------------- first picking
  function renderFirst() {
    const combos = combosById();
    const matchups = roleData().matchups;
    const pool = [...combos.values()].filter((c) => c.wr > 0.48 && c.safe != null); //Changed 0.5 -> 0.48 to allow for potentially safer blind picks that don't themselves have a high winrate.
    const hidden = combos.size - pool.length;

    // Lowest conservative spread first; ties go to the higher winrate.
    pool.sort((a, b) => a.safe - b.safe || b.wr - a.wr);

    // "Best pick": nobody else is at least as good on both and strictly better on one (Pareto front).
    for (const c of pool) {
      c.best = !pool.some((o) => o !== c && o.wr >= c.wr && o.safe <= c.safe && (o.wr > c.wr || o.safe < c.safe));
    }

    const rows = pool.map((c, i) => {
      const worst = matchups.filter((m) => m[0] === c.id)
        .map((m) => ({ opp: m[1], games: m[2], wr: m[3] / m[2] }))
        .sort((a, b) => a.wr - b.wr).slice(0, 3);
      const chips = worst.length
        ? worst.map((w) => `<span class="chip" title="${w.games} games">${icon(w.opp)}${esc(champ(w.opp).name)} <span class="${wrClass(w.wr)}">${pct(w.wr, 0)}</span></span>`).join("")
        : `<span class="chip empty">No matchup with ${T.matchup}+ games yet</span>`;
      return `<tr>
        <td class="num rank">${i + 1}</td>
        ${champCell(c.id, c.games, c.best ? '<span class="badge best">Best pick</span>' : "")}
        ${wrCell(c.wins, c.games)}
        <td class="num" title="Point estimate ±${pct(c.spread)} across ${c.k} lane opponents">±${pct(c.safe)}</td>
        <td class="num">${c.games.toLocaleString()}</td>
        <td><div class="chips">${chips}</div></td>
      </tr>`;
    });
    $("first-body").innerHTML = rows.join("") ||
      `<tr><td colspan="6" class="muted">No ${state.role} champion is above 50% with ${T.combo}+ games in this rank group yet.</td></tr>`;
    $("first-note").textContent = hidden
      ? `${hidden} other ${state.role} champion${hidden === 1 ? "" : "s"} with ${T.combo}+ games ${hidden === 1 ? "is" : "are"} hidden for being at or below 50% winrate.`
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
    card.innerHTML = `${icon(enemy.id)}<div><div class="title">${esc(champ(enemy.id).name)} ${smallBadge(enemy.games)}</div>
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

  // ------------------------------------------------------------ controls
  function renderControls() {
    $("role-buttons").innerHTML = ROLES.map(([r, label]) =>
      `<button data-role="${r}" role="tab" aria-selected="${r === state.role}">${label}</button>`).join("");
    document.querySelectorAll("#mode-buttons button").forEach((b) =>
      b.setAttribute("aria-selected", b.dataset.mode === state.mode));
    $("tier-select").value = state.tier;
    const g = D.groups[state.tier];
    $("data-info").textContent =
      `${g.games.toLocaleString()} ranked solo games · ${g.label} · patch ${D.patches.join(", ")} · updated ${D.generated}`;
  }

  function render() {
    renderControls();
    $("view-first").hidden = state.mode !== "first";
    $("view-counter").hidden = state.mode !== "counter";
    if (state.mode === "first") renderFirst(); else renderCounter();
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
    state.mode = b.dataset.mode; writeHash(); render();
    if (state.mode === "counter" && state.enemy == null) input.focus();
  });
  window.addEventListener("hashchange", () => { readHash(); render(); });

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
