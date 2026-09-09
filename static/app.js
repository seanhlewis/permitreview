const REVIEW_MODES = {
  fast: {
    label: "Guided review",
    action: "Start guided review",
    description: "Check the model suggestion, accept it when right, or correct it.",
  },
  blind: {
    label: "Blind review",
    action: "Start blind review",
    description: "Assign categories without seeing model suggestions.",
  },
  readonly: {
    label: "Historical review · read-only",
    action: "View",
    description: "View saved historical permits without editing them.",
  },
  narrow: {
    label: "Category review",
    action: "Start",
    description: "Pick the specific type of work. The model's guess is hidden.",
  },
};

const state = {
  samples: [],
  myTasks: [],
  historicalTasks: [],
  reviewerKnown: false,
  assignedTotal: 0,
  taxonomy: [],
  history: [],
  byFlag: new Map(),
  taxonomyByCategory: new Map(),
  taxonomyCategories: [],
  currentSample: null,
  currentMode: "fast",
  currentItem: null,
  currentIndex: 0,
  currentTotal: 0,
  selectedFlags: [],
  blindActiveCategory: "",
  blindTaxonomyQuery: "",
  showFullTree: false,
  blindAutosaveTimer: null,
  reviewAutosaveDirty: false,
  reviewSaveInFlight: false,
  reviewContextToken: 0,
  startedAt: Date.now(),
};

const app = document.getElementById("app");
const reviewerInput = document.getElementById("reviewerName");
const refreshBtn = document.getElementById("refreshBtn");

const savedReviewer = localStorage.getItem("candyReviewerName") || "";
reviewerInput.value = savedReviewer;
const BLIND_AUTOSAVE_DELAY_MS = 1500;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[ch]));
}

function formatHistoryDate(value) {
  if (!value) return "No saved activity";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function historyStatusLabel(status) {
  return ({
    reviewed: "Completed",
    skipped: "Skipped",
    unclear: "Unclear",
    denied: "Denied",
    in_progress: "In progress",
  })[status] || (status ? String(status) : "Not saved");
}

function reviewer() {
  return reviewerInput.value.trim();
}

function reviewerReady() {
  return reviewer().length > 0;
}

function modeInfo(mode) {
  return REVIEW_MODES[mode] || REVIEW_MODES.fast;
}

const V3_TITLES = {
  taxval_v3_electrical: "Electrical & electrification",
  taxval_v3_mechanical: "Mechanical & HVAC",
  taxval_v3_plumbing: "Plumbing & gas",
  taxval_v3_exterior: "Exterior",
  taxval_v3_building: "Building & structural",
  taxval_v3_sitework: "Sitework & pools",
  taxval_v3_lifesafety: "Life safety",
  taxval_v3_anchor: "Random mix (all categories)",
};
const TARGET_TITLES = {
  alteration_remodel: "Alteration / remodel",
  battery_storage: "Battery storage",
  deck_patio_porch: "Deck / patio / porch",
  demolition: "Demolition",
  driveway_paving: "Driveway / paving",
  electrical_panel_upgrade: "Electrical panel upgrade",
  ev_charger: "EV charger",
  fence_wall: "Fence / wall",
  fire_protection: "Fire protection",
  garage_accessory: "Garage / accessory",
  gas: "Gas",
  grading_sitework: "Grading / sitework",
  mechanical_hvac: "Mechanical / HVAC",
  new_construction: "New construction",
  plumbing: "Plumbing",
  roofing: "Roofing",
  sewer_utility: "Sewer / utility",
  sign: "Sign",
  structural: "Structural",
  water_heater: "Water heater",
  window_door: "Window / door",
};
function sampleLabel(sample) {
  const key = (typeof sample === "string") ? sample : (sample.sample || "");
  if (V3_TITLES[key]) return V3_TITLES[key];
  if (key.startsWith("taxval_v4_targeted_")) {
    const target = key.replace("taxval_v4_targeted_", "");
    return `Targeted review · ${TARGET_TITLES[target] || humanizeCategoryName(target)}`;
  }
  const n = (typeof sample === "object" && sample.expected_n) || 0;
  return n ? `Random sample ${n.toLocaleString()}` : key;
}

function humanizeCategoryName(value) {
  return String(value ?? "")
    .trim()
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function sortTaxonomyRows(a, b) {
  const aName = String(a.subcategory || a.display_label || a.flag || "").toLowerCase();
  const bName = String(b.subcategory || b.display_label || b.flag || "").toLowerCase();
  const cmp = aName.localeCompare(bName);
  if (cmp) return cmp;
  return String(a.flag || "").localeCompare(String(b.flag || ""));
}

function buildTaxonomyIndex(rows) {
  const byCategory = new Map();
  for (const row of rows || []) {
    const category = String(row.category || "").trim() || "uncategorized";
    if (!byCategory.has(category)) byCategory.set(category, []);
    byCategory.get(category).push(row);
  }
  const categories = Array.from(byCategory.keys())
    .sort((a, b) => humanizeCategoryName(a).localeCompare(humanizeCategoryName(b)))
    .map((category) => ({
      category,
      label: humanizeCategoryName(category) || category,
      count: byCategory.get(category).length,
      items: byCategory.get(category).slice().sort(sortTaxonomyRows),
    }));
  return { byCategory, categories };
}

function taxonomyMatchesQuery(row, query) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return true;
  const haystack = [
    row.flag,
    row.category,
    row.subcategory,
    row.display_label,
    row.description,
    ...(row.keywords || []),
  ].join(" ").toLowerCase();
  return haystack.includes(q);
}

function clearBlindAutosaveTimer() {
  if (state.blindAutosaveTimer) {
    clearTimeout(state.blindAutosaveTimer);
    state.blindAutosaveTimer = null;
  }
}

function invalidateReviewContext() {
  state.reviewContextToken += 1;
  clearBlindAutosaveTimer();
  state.reviewAutosaveDirty = false;
}

function scheduleBlindAutosave() {
  if (!state.currentItem || !reviewerReady()) return;
  state.reviewAutosaveDirty = true;
  clearBlindAutosaveTimer();
  const reviewId = state.currentItem.review_id;
  const reviewToken = state.reviewContextToken;
  state.blindAutosaveTimer = window.setTimeout(() => {
    state.blindAutosaveTimer = null;
    if (!state.currentItem || state.currentItem.review_id !== reviewId || state.reviewContextToken !== reviewToken) {
      return;
    }
    const existing = state.currentItem.existing_review || {};
    saveReview(
      state.selectedFlags,
      existing.status || "reviewed",
      existing.chosen_source || "autosave",
      false,
    );
  }, BLIND_AUTOSAVE_DELAY_MS);
}

async function flushReviewAutosave() {
  if (!state.reviewAutosaveDirty || !state.currentItem || !reviewerReady()) return;
  clearBlindAutosaveTimer();
  const existing = state.currentItem.existing_review || {};
  await saveReview(
    state.selectedFlags,
    existing.status || "reviewed",
    existing.chosen_source || "autosave",
    false,
  );
}

async function getJSON(url, options) {
  const res = await fetch(url, options);
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    const err = new Error(data.message || data.error || "Request failed");
    err.data = data;
    throw err;
  }
  return data;
}

function labelName(flag) {
  const row = state.byFlag.get(flag);
  if (!row) return flag;
  return row.display_label || `${row.category} -> ${row.subcategory}`;
}

// just the leaf name (no "Family ->" prefix) — used in the category picker where the family is already shown
function leafName(flag) {
  const row = state.byFlag.get(flag);
  if (!row) return flag;
  return row.subcategory || row.display_label || flag;
}

function flagChips(flags, className = "") {
  if (!flags || !flags.length) return `<span class="empty-label">No taxonomy label</span>`;
  return flags.map((flag) => `<span class="chip ${className}" title="${esc(flag)}">${esc(labelName(flag))}</span>`).join("");
}

async function load() {
  invalidateReviewContext();
  const [status, taxonomy, history] = await Promise.all([
    getJSON("/api/status"),
    getJSON("/api/taxonomy"),
    getJSON("/api/history"),
  ]);
  state.samples = status.samples;
  state.taxonomy = taxonomy.taxonomy || [];
  state.history = history.history || [];
  state.byFlag = new Map(state.taxonomy.map((row) => [row.flag, row]));
  const taxonomyIndex = buildTaxonomyIndex(state.taxonomy);
  state.taxonomyByCategory = taxonomyIndex.byCategory;
  state.taxonomyCategories = taxonomyIndex.categories;
  renderDashboard();
  loadMyTasks();
}

let myTasksTimer = null;
async function loadMyTasks() {
  const name = reviewer();
  if (!name) { state.myTasks = []; renderDashboard(); return; }
  try {
    const r = await getJSON("/api/mytasks?reviewer=" + encodeURIComponent(name));
    state.myTasks = (r && r.tasks) || [];
    state.historicalTasks = (r && r.historical) || [];
    state.reviewerKnown = Boolean(r && r.reviewer_known);
    state.assignedTotal = Number((r && r.assigned_total) || 0);
  } catch (e) { state.myTasks = []; state.historicalTasks = []; state.reviewerKnown = false; state.assignedTotal = 0; }
  renderDashboard();
}
function scheduleMyTasks() {
  if (myTasksTimer) clearTimeout(myTasksTimer);
  myTasksTimer = setTimeout(loadMyTasks, 1000);
}

function progressFor(sample, mode) {
  const name = reviewer();
  return state.history.find((row) => row.sample === sample.sample && row.mode === mode && row.reviewer === name);
}

function renderDashboard() {
  const nameReady = reviewerReady();
  const nameGate = nameReady ? "" : `
    <section class="name-gate">
      <strong>Type your reviewer name to begin.</strong>
    </section>
  `;

  const STATE_LABEL = { not_started: "Not started", in_progress: "In progress", done: "Done" };
  const tasks = nameReady ? (state.myTasks || []) : [];
  const rows = tasks.map((t) => {
    const pct = t.assigned ? Math.round((t.done / t.assigned) * 100) : 0;
    const stateLabel = STATE_LABEL[t.state] || t.state;
    const btnLabel = t.state === "not_started" ? "Start" : (t.state === "done" ? "Review again" : "Resume");
    const disabled = !t.ready;
    const meta = t.ready
      ? `${t.done}/${t.assigned} class-target reviews${t.remaining ? ` &middot; ${t.remaining} left` : ""}`
      : "No permits loaded yet";
    return `
      <section class="sample-row">
        <div class="sample-main">
          <div class="sample-title">${esc(t.title)} <span class="task-badge ${esc(t.state)}">${esc(stateLabel)}</span></div>
          <div class="sample-meta">${meta}</div>
          <div class="bar"><div style="width:${pct}%"></div></div>
        </div>
        <div class="sample-actions">
          <button class="primary-action" data-review="${esc(t.sample)}" data-mode="${esc(t.mode)}" ${disabled ? "disabled" : ""} title="${disabled ? "Waiting for permits to be loaded" : ""}">${esc(btnLabel)}</button>
        </div>
      </section>
    `;
  }).join("");

  const historicalRows = (state.historicalTasks || []).map((t) => {
    const total = Number(t.total || t.assigned || 0);
    const sourceTotal = Number(t.source_total || 0);
    const saved = Number(t.saved_count || 0);
    const completed = Number(t.completed_count || 0);
    const pct = total ? Math.min(100, Math.round((completed / total) * 100)) : 0;
    const stateLabel = ({
      done: "Complete",
      in_progress: "In progress",
      not_started: "Not started",
    })[t.state] || "Not started";
    const progress = saved
      ? `${total.toLocaleString()} permits in this reviewer's historical record · ${completed.toLocaleString()} completed answers`
      : "No saved historical records for this reviewer";
    const sourceMeta = sourceTotal && sourceTotal !== total
      ? `Full historical sample: ${sourceTotal.toLocaleString()} permits`
      : "Historical sample size";
    return `
      <section class="sample-row historical-task" data-locked="true">
        <div class="sample-main">
          <div class="sample-title">${esc(t.title)} <span class="task-badge locked">Historical · locked</span></div>
          <div class="sample-meta">${esc(progress)}</div>
          <div class="sample-meta historical-source-meta">${esc(sourceMeta)}</div>
          <div class="historical-progress" aria-label="${esc(completed)} of ${esc(total)} historical permits completed">
            <div class="historical-progress-track"><div style="width:${pct}%"></div></div>
            <span>${esc(stateLabel)} · Last activity: ${esc(formatHistoryDate(t.last_updated))}</span>
          </div>
        </div>
        <div class="sample-actions"><button class="historical-view" data-review="${esc(t.sample)}" data-mode="readonly" ${total > 0 ? "" : "disabled"}>View</button></div>
      </section>
    `;
  }).join("");

  const intro = "";

  app.innerHTML = `
    ${nameGate}
    ${intro}
    ${nameReady ? `<section class="queue-section"><div class="queue-section-head"><h2>Active targeted queues</h2><p>Class-organized permits for the new human validation round.</p></div><section class="sample-list">${rows}</section></section>` : ""}
    ${nameReady ? `<section class="queue-section historical-section"><div class="queue-section-head"><h2>Historical review tasks</h2><p>Completed V3 task cards are locked. Saved review records remain available in Review History.</p></div><section class="sample-list">${historicalRows}</section></section>` : ""}
  `;
  wireDashboard();
}

function renderHistory() {
  if (!state.history.length) {
    return `
      <section class="history-panel">
        <div class="section-head">
          <h2>Review History</h2>
        </div>
        <div class="empty-label">No reviews have been saved yet.</div>
      </section>
    `;
  }

  const rows = state.history.map((item) => {
    const total = item.total || 0;
    const saved = item.saved_count || 0;
    const pct = total ? Math.min(100, Math.round((saved / total) * 100)) : 0;
    return `
      <tr>
        <td>
          <strong>${esc(item.reviewer)}</strong>
          <span>${esc(item.last_updated || "")}</span>
        </td>
        <td>
          <strong>${esc(item.sample_label)} - ${esc(item.job)}</strong>
        </td>
        <td>
          <div class="history-progress">
            <span>${esc(saved)}/${esc(total)} saved</span>
            <div class="bar"><div style="width:${pct}%"></div></div>
          </div>
        </td>
        <td>${esc(item.reviewed_count)} reviewed<br>${esc(item.skipped_count)} skipped<br>${esc(item.unclear_count)} unclear<br>${esc(item.denied_count || 0)} denied</td>
      </tr>
    `;
  }).join("");

  return `
    <section class="history-panel">
      <div class="section-head">
        <h2>Review History</h2>
      </div>
      <table>
        <thead><tr><th>Reviewer</th><th>Job</th><th>Progress</th><th>Saved labels</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>
  `;
}

function wireDashboard() {
  document.querySelectorAll("[data-review]").forEach((btn) => {
    btn.addEventListener("click", () => startReview(btn.dataset.review, btn.dataset.mode));
  });
}

async function startReview(sample, mode) {
  if (!reviewerReady()) {
    reviewerInput.focus();
    renderDashboard();
    return;
  }
  state.currentSample = sample;
  state.currentMode = mode;
  await loadNext();
}

async function loadNext() {
  invalidateReviewContext();
  state.selectedFlags = [];
  state.blindActiveCategory = "";
  state.blindTaxonomyQuery = "";
  state.showFullTree = false;
  state.startedAt = Date.now();
  const url = `/api/next/${encodeURIComponent(state.currentSample)}?mode=${encodeURIComponent(state.currentMode)}&reviewer=${encodeURIComponent(reviewer())}`;
  try {
    const data = await getJSON(url);
    if (data.complete) {
      const info = modeInfo(state.currentMode);
      app.innerHTML = `
        <section class="complete">
          <h2>Queue Complete</h2>
          <p>${esc(sampleLabel(state.currentSample))} is complete for ${esc(reviewer())}.</p>
          <button id="backHome">Back</button>
          <button id="scoreNow">Generate report</button>
        </section>`;
      document.getElementById("backHome").onclick = load;
      document.getElementById("scoreNow").onclick = () => scoreSample(state.currentSample, state.currentMode, reviewer());
      return;
    }
    state.currentItem = data.item;
    state.currentIndex = Number(data.item.index || 0);
    state.currentTotal = Number(data.item.total || 0);
    renderReview();
  } catch (err) {
    renderError(err);
  }
}

async function loadItem(index) {
  invalidateReviewContext();
  state.selectedFlags = [];
  state.blindActiveCategory = "";
  state.blindTaxonomyQuery = "";
  state.showFullTree = false;
  state.startedAt = Date.now();
  const url = `/api/item/${encodeURIComponent(state.currentSample)}?mode=${encodeURIComponent(state.currentMode)}&reviewer=${encodeURIComponent(reviewer())}&index=${encodeURIComponent(index)}`;
  try {
    const data = await getJSON(url);
    if (data.complete) {
      await loadNext();
      return;
    }
    state.currentItem = data.item;
    state.currentIndex = Number(data.item.index || 0);
    state.currentTotal = Number(data.item.total || 0);
    try {
      const prior = JSON.parse((data.item.existing_review || {}).chosen_labels_json || "[]");
      if (Array.isArray(prior) && prior.length) state.selectedFlags = prior.slice();
    } catch (e) {}
    renderReview();
  } catch (err) {
    renderError(err);
  }
}

async function moveItem(delta) {
  const nextIndex = state.currentIndex + delta;
  if (nextIndex < 0 || (state.currentTotal && nextIndex >= state.currentTotal)) return;
  await flushReviewAutosave();
  await loadItem(nextIndex);
}

function renderReview() {
  const item = state.currentItem;
  const options = item.options || [];
  const info = modeInfo(state.currentMode);
  const position = item.position || state.currentIndex + 1;
  const total = item.total || state.currentTotal || "?";
  const suggested = options[0];
  const alternate = options[1];
  const leafOptions = item.leaf_options || [];
  const isNarrow = state.currentMode === "narrow";
  const isReadonly = state.currentMode === "readonly";
  const hasFamily = isNarrow && leafOptions.length > 0 && !state.showFullTree;
  const showTree = state.currentMode === "blind" || (isNarrow && !hasFamily);
  const historicalReview = item.existing_review || {};
  let historicalFlags = [];
  try { historicalFlags = JSON.parse(historicalReview.chosen_labels_json || "[]") || []; } catch (e) {}
  const readonlyHtml = `
    <section class="readonly-panel" aria-readonly="true">
      <strong>Historical permit · read-only</strong>
      <p>Editing is disabled. Use Back and Forward, or the keyboard arrow keys, to browse this historical queue.</p>
      <div class="readonly-field"><b>Saved answer:</b> ${historicalFlags.length ? flagChips(historicalFlags) : (historicalReview.chosen_source === "none" ? "No applicable category" : "No saved category")}</div>
      <div class="readonly-field"><b>Completion status:</b> ${esc(historyStatusLabel(historicalReview.status))}</div>
      <div class="readonly-field"><b>Review mode:</b> ${esc(historicalReview.mode || "Historical record")}</div>
      <div class="readonly-field"><b>Reason:</b> ${historicalReview.reason_tag ? esc(historicalReview.reason_tag) : "No reason recorded"}</div>
      <div class="readonly-field"><b>Saved note:</b> ${historicalReview.note ? esc(historicalReview.note) : "No saved note"}</div>
      <div class="readonly-field"><b>Last saved:</b> ${esc(formatHistoryDate(historicalReview.updated_at))}</div>
    </section>`;
  const narrowHtml = `
    <section class="narrow-panel">
      <div class="narrow-head">What specific type of work is this permit?${item.family ? ` <span class="muted">— ${esc(item.family)}</span>` : ""}</div>
      <div class="narrow-grid">
        ${leafOptions.map((o, i) => `<button class="narrow-choice" data-leaf="${esc(o.flag)}"><b>${i + 1}</b><span>${esc(leafName(o.flag))}</span></button>`).join("")}
      </div>
      <div class="narrow-actions">
        <button class="narrow-none" data-none="1"><b>8</b>None of these apply</button>
        <button class="narrow-skip" data-guided-skip="1"><b>9</b>Skip for now</button>
      </div>
      <input id="noteField" type="text" placeholder="Optional note" autocomplete="off">
    </section>`;
  const actionHtml = isReadonly ? readonlyHtml : state.currentMode === "fast" ? `
    <section class="guided-decision-grid">
      <button class="guided-decision primary" data-guided-source="${esc(suggested ? suggested.id : "")}" ${suggested ? "" : "disabled"}>
        <b>1</b>
        <span>Accept suggested labels</span>
        <strong>${suggested ? flagChips(suggested.flags) : `<span class="empty-label">No suggestion available</span>`}</strong>
      </button>
      <button class="guided-decision" data-guided-source="${esc(alternate ? alternate.id : "")}" ${alternate ? "" : "disabled"}>
        <b>2</b>
        <span>Use alternate labels</span>
        <strong>${alternate ? flagChips(alternate.flags) : `<span class="empty-label">No alternate labels</span>`}</strong>
      </button>
      <button class="guided-decision skip" data-guided-skip="1">
        <b>3</b>
        <span>Skip</span>
        <strong><span class="empty-label">Come back to this permit later</span></strong>
      </button>
    </section>
    <section class="guided-note-panel">
      <input id="noteField" type="text" placeholder="Note" autocomplete="off">
      <button class="deny-action" data-guided-deny="1">DENY</button>
    </section>
  ` : hasFamily ? narrowHtml : `
    <section class="proposal-band muted">
      <div>Choose the category below. The model's guess is hidden on purpose.</div>
      <div>Selections autosave after a short pause and stay on this permit.</div>
    </section>
  `;
  const manualHtml = showTree ? `
    <section class="manual-panel blind-panel">
      <div class="manual-head">
        <div>
          <h3>Choose categories</h3>
          <p class="manual-subtitle">
            ${state.blindActiveCategory ? `Pick one or more subcategories from ${esc(humanizeCategoryName(state.blindActiveCategory))}. Search to narrow them down.` : "Start with one of the top-level classes."}
            Autosaves after a short pause and stays on this permit.
          </p>
        </div>
        <div class="manual-actions">
          ${state.blindActiveCategory ? `<button id="backTaxonomy">Back to classes</button>` : ""}
          <button id="noneApplicable" class="none-applicable">No applicable category (admin / no real work)</button>
          <button id="saveManual">Save now</button>
        </div>
      </div>
      <div id="selectedFlags" class="selected-flags"></div>
      ${state.blindActiveCategory ? `
        <div class="taxonomy-search-row">
          <input id="taxonomySearch" class="search" type="search" placeholder="Search subcategories, keywords, or description" autocomplete="off" value="${esc(state.blindTaxonomyQuery)}">
          <div class="taxonomy-search-meta">Search stays inside this class.</div>
        </div>
      ` : ""}
      <div id="taxonomyResults" class="taxonomy-browser"></div>
      <div class="review-fields">
        <select id="reasonTag">
          <option value="">Reason</option>
          <option value="wrong_trade">Wrong trade</option>
          <option value="too_generic">Too generic</option>
          <option value="too_specific">Too specific</option>
          <option value="missed_secondary">Missed secondary work</option>
          <option value="source_ambiguous">Source ambiguous</option>
          <option value="other">Other</option>
        </select>
        <input id="noteField" type="text" placeholder="Note">
      </div>
    </section>
  ` : "";

  app.innerHTML = `
    <section class="review-shell${isReadonly ? " readonly-review" : ""}">
      <div class="review-header">
        <button id="homeBtn">Home</button>
        <div>
          <div class="eyebrow">${esc(info.label)}</div>
          <h2>${esc(reviewer())} — ${esc(sampleLabel(state.currentSample))}</h2>
        </div>
        <div class="review-nav">
          <button id="prevItemBtn" ${state.currentIndex <= 0 ? "disabled" : ""}>Back</button>
          <span>${esc(position)} / ${esc(total)}</span>
          <button id="nextItemBtn" ${state.currentTotal && state.currentIndex >= state.currentTotal - 1 ? "disabled" : ""}>Forward</button>
        </div>
      </div>

      <section class="permit-panel">
        <p>${esc(item.description)}</p>
      </section>

      ${actionHtml}

      ${manualHtml}
    </section>
  `;
  wireReview();
  renderSelected();
  renderTaxonomy("");
  markExistingAnswer(item);
}

// when a permit was already answered (navigating back), show the prior pick as selected
function markExistingAnswer(item) {
  const ex = item.existing_review;
  const noteField = document.getElementById("noteField");
  if (noteField) noteField.value = (ex && ex.note) || "";
  const reasonTag = document.getElementById("reasonTag");
  if (reasonTag) reasonTag.value = (ex && ex.reason_tag) || "";
  if (!ex || !ex.status) return;
  let prior = [];
  try { prior = JSON.parse(ex.chosen_labels_json || "[]") || []; } catch (e) {}
  prior.forEach((fl) => {
    const b = document.querySelector('.narrow-choice[data-leaf="' + fl + '"]');
    if (b) b.classList.add("selected");
  });
  if ((ex.chosen_source === "none") || (ex.status === "reviewed" && prior.length === 0 && ex.chosen_source !== "narrow_pick")) {
    const nb = document.querySelector(".narrow-none");
    if (nb) nb.classList.add("selected");
  }
}

function wireReview() {
  document.getElementById("homeBtn").onclick = async () => {
    await flushReviewAutosave();
    await load();
  };
  document.getElementById("prevItemBtn").onclick = () => moveItem(-1);
  document.getElementById("nextItemBtn").onclick = () => moveItem(1);
  const skipBtn = document.getElementById("skipBtn");
  if (skipBtn) skipBtn.onclick = () => saveReview([], "skipped", "skip");
  const backTaxonomy = document.getElementById("backTaxonomy");
  if (backTaxonomy) backTaxonomy.onclick = () => {
    state.blindActiveCategory = "";
    state.blindTaxonomyQuery = "";
    renderReview();
  };
  const saveManual = document.getElementById("saveManual");
  if (saveManual) saveManual.onclick = () => {
    clearBlindAutosaveTimer();
    saveReview(state.selectedFlags, "reviewed", "manual", false);
  };
  document.querySelectorAll("[data-guided-source]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const opt = (state.currentItem.options || []).find((item) => item.id === btn.dataset.guidedSource);
      if (opt) saveReview(opt.flags, "reviewed", opt.id);
    });
  });
  document.querySelectorAll("[data-guided-skip]").forEach((btn) => {
    btn.addEventListener("click", () => saveReview([], "skipped", "skip"));
  });
  document.querySelectorAll("[data-guided-deny]").forEach((btn) => {
    btn.addEventListener("click", () => saveReview([], "denied", "deny"));
  });
  // narrowed blind picker: one click on a family leaf = assign + save (flash green first)
  document.querySelectorAll("[data-leaf]").forEach((btn) => {
    btn.addEventListener("click", () => { btn.classList.add("selected"); saveReview([btn.dataset.leaf], "reviewed", "narrow_pick"); });
  });
  const noneBtn = document.getElementById("noneApplicable");
  if (noneBtn) noneBtn.onclick = () => { clearBlindAutosaveTimer(); saveReview([], "reviewed", "none"); };
  // "None of these apply" in a category task: save as none + advance (no tree)
  document.querySelectorAll("[data-none]").forEach((btn) => {
    btn.addEventListener("click", () => { btn.classList.add("selected"); saveReview([], "reviewed", "none"); });
  });
  document.querySelectorAll("[data-fulltree]").forEach((btn) => {
    btn.addEventListener("click", () => { state.showFullTree = true; renderReview(); });
  });
  const reasonTag = document.getElementById("reasonTag");
  if (reasonTag) reasonTag.addEventListener("change", scheduleBlindAutosave);
  const noteField = document.getElementById("noteField");
  if (noteField) noteField.addEventListener("input", scheduleBlindAutosave);
  if (document.getElementById("taxonomyResults")) {
    const search = document.getElementById("taxonomySearch");
    if (search) {
      search.oninput = () => {
        state.blindTaxonomyQuery = search.value;
        renderTaxonomy();
      };
    }
  }
  window.onkeydown = (event) => {
    if (window.__helpOpen) return;
    if (!state.currentItem) return;
    const editingText = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target?.tagName || "");
    if (!editingText && event.key === "ArrowLeft") {
      event.preventDefault();
      moveItem(-1);
      return;
    }
    if (!editingText && event.key === "ArrowRight") {
      event.preventDefault();
      moveItem(1);
      return;
    }
    if (state.currentMode === "readonly") return;
    if (state.currentMode === "narrow" && !editingText && !state.showFullTree) {
      const opts = state.currentItem.leaf_options || [];
      const n = parseInt(event.key, 10);
      if (n >= 1 && n <= opts.length) {
        const b = document.querySelectorAll(".narrow-choice")[n - 1];
        if (b) b.classList.add("selected");
        saveReview([opts[n - 1].flag], "reviewed", "narrow_pick");
        return;
      }
      if (event.key === "8") { saveReview([], "reviewed", "none"); return; }
      if (event.key === "9" || event.key.toLowerCase() === "s") { saveReview([], "skipped", "skip"); return; }
      return;
    }
    if (state.currentMode === "fast" && !editingText) {
      if (event.key === "1") {
        const opt = state.currentItem.options[0];
        if (opt) saveReview(opt.flags, "reviewed", opt.id);
      } else if (event.key === "2") {
        const opt = state.currentItem.options[1];
        if (opt) saveReview(opt.flags, "reviewed", opt.id);
      } else if (event.key === "3") {
        saveReview([], "skipped", "skip");
      } else if (event.key === "4") {
        saveReview([], "denied", "deny");
      }
      return;
    }
    if (!editingText && event.key === "/" && search && document.activeElement !== search) {
      event.preventDefault();
      search.focus();
    } else if (!editingText && event.key.toLowerCase() === "u") {
      saveReview([], "unclear", "unclear", state.currentMode !== "blind");
    } else if (!editingText && event.key === "Enter") {
      saveReview(state.selectedFlags, "reviewed", "manual", state.currentMode !== "blind");
    }
  };
}

function renderSelected() {
  const el = document.getElementById("selectedFlags");
  if (!el) return;
  el.innerHTML = state.selectedFlags.length
    ? state.selectedFlags.map((flag) => `<button class="chip removable" data-remove="${esc(flag)}">${esc(labelName(flag))}</button>`).join("")
    : `<span class="empty-label">No categories selected</span>`;
  el.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.onclick = () => {
      state.selectedFlags = state.selectedFlags.filter((flag) => flag !== btn.dataset.remove);
      renderSelected();
      renderTaxonomy();
      scheduleBlindAutosave();
    };
  });
}

function toggleBlindTaxonomyFlag(flag) {
  if (!flag) return;
  if (state.selectedFlags.includes(flag)) {
    state.selectedFlags = state.selectedFlags.filter((item) => item !== flag);
  } else {
    state.selectedFlags = state.selectedFlags.concat(flag);
  }
  renderSelected();
  renderTaxonomy();
  scheduleBlindAutosave();
}

function renderTaxonomy() {
  const box = document.getElementById("taxonomyResults");
  if (!box) return;
  if (!state.taxonomyCategories.length) {
    box.innerHTML = `<div class="taxonomy-empty">No taxonomy loaded.</div>`;
    return;
  }

  if (!state.blindActiveCategory) {
    box.innerHTML = `
      <div class="taxonomy-stage">
        <div class="taxonomy-stage-head">
          <div>
            <strong>Top-level classes</strong>
            <span>Pick one class first</span>
          </div>
          <span class="taxonomy-count">${esc(state.taxonomyCategories.length)} groups</span>
        </div>
        <div class="taxonomy-category-grid">
          ${state.taxonomyCategories.map((group) => `
            <button class="taxonomy-category-card" data-category="${esc(group.category)}">
              <strong>${esc(group.label)}</strong>
              <span>${esc(group.count)} subcategories</span>
            </button>
          `).join("")}
        </div>
      </div>
    `;
    box.querySelectorAll("[data-category]").forEach((btn) => {
      btn.onclick = () => {
        state.blindActiveCategory = btn.dataset.category || "";
        state.blindTaxonomyQuery = "";
        renderReview();
      };
    });
    return;
  }

  const group = state.taxonomyCategories.find((item) => item.category === state.blindActiveCategory);
  if (!group) {
    state.blindActiveCategory = "";
    renderTaxonomy();
    return;
  }

  const query = state.blindTaxonomyQuery;
  const filtered = group.items.filter((row) => taxonomyMatchesQuery(row, query));
  const leafHtml = filtered.length ? `
      <div class="taxonomy-leaf-grid">
        ${filtered.map((row) => {
          const selected = state.selectedFlags.includes(row.flag);
          return `
            <button class="taxonomy-leaf-card${selected ? " selected" : ""}" data-flag="${esc(row.flag)}" aria-pressed="${selected ? "true" : "false"}">
              <strong>${esc(row.subcategory || row.display_label || row.flag)}</strong>
              <span>${esc(row.description || row.flag)}</span>
            </button>
          `;
        }).join("")}
      </div>
  ` : `<div class="taxonomy-empty">No matches. Try a different search term or clear the search box.</div>`;
  box.innerHTML = `
    <div class="taxonomy-stage">
      <div class="taxonomy-stage-head">
        <div>
          <strong>${esc(group.label)}</strong>
          <span>${esc(filtered.length)} of ${esc(group.items.length)} subcategories shown, alphabetical</span>
        </div>
      </div>
      ${leafHtml}
    </div>
  `;
  box.querySelectorAll("[data-flag]").forEach((btn) => {
    btn.onclick = () => toggleBlindTaxonomyFlag(btn.dataset.flag);
  });
}

async function saveReview(labels, status, chosenSource, advanceAfterSave = true) {
  if (!reviewerReady()) {
    reviewerInput.focus();
    return;
  }
  if (state.reviewSaveInFlight) return;
  state.reviewSaveInFlight = true;
  clearBlindAutosaveTimer();
  const reviewToken = state.reviewContextToken;
  try {
    const payload = {
      sample: state.currentSample,
      review_id: state.currentItem.review_id,
      reviewer: reviewer(),
      mode: state.currentMode,
      status,
      labels,
      chosen_source: chosenSource,
      reason_tag: document.getElementById("reasonTag")?.value || "",
      note: document.getElementById("noteField")?.value || "",
      review_time_ms: Date.now() - state.startedAt,
    };
    await getJSON("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (reviewToken !== state.reviewContextToken) return;
    state.reviewAutosaveDirty = false;
    state.currentItem.existing_review = {
      ...(state.currentItem.existing_review || {}),
      status,
      chosen_labels_json: JSON.stringify(labels),
      chosen_source: chosenSource,
      reason_tag: payload.reason_tag,
      note: payload.note,
    };
    if (state.currentMode === "blind" || !advanceAfterSave) return;
    const nextIndex = state.currentIndex + 1;
    if (state.currentTotal && nextIndex < state.currentTotal) {
      await loadItem(nextIndex);
    } else {
      await loadNext();
    }
  } catch (err) {
    renderError(err);
  } finally {
    state.reviewSaveInFlight = false;
  }
}

async function scoreSample(sample, mode = "", reportReviewer = "") {
  const params = new URLSearchParams();
  if (mode) params.set("mode", mode);
  if (reportReviewer) params.set("reviewer", reportReviewer);
  const query = params.toString() ? `?${params.toString()}` : "";
  app.innerHTML = `<section class="notice">Generating accuracy report...</section>`;
  try {
    const data = await getJSON(`/api/score/${encodeURIComponent(sample)}${query}`);
    const rows = data.summary.methods.map((item) => `
      <tr>
        <td>${esc(item.method)}</td>
        <td>${esc(item.reviewed_n)}</td>
        <td>${esc(item.strict_exact_errors)}</td>
        <td>${item.strict_exact_accuracy == null ? "" : esc((item.strict_exact_accuracy * 100).toFixed(2) + "%")}</td>
        <td>${item.one_sided_95_upper_error == null ? "" : esc((item.one_sided_95_upper_error * 100).toFixed(3) + "%")}</td>
        <td>${item.supports_error_lt_1pct ? "yes" : "no"}</td>
      </tr>
    `).join("");
    app.innerHTML = `
      <section class="score-panel">
        <div class="review-header">
          <button id="backBtn">Back</button>
          <div>
            <div class="eyebrow">Accuracy report</div>
            <h2>${esc(sample)}</h2>
          </div>
        </div>
        <p>Compares saved human labels with the hidden model answers for ${esc(reportReviewer || "all reviewers")} in ${esc(mode ? modeInfo(mode).label : "all review modes")}.</p>
        <table>
          <thead><tr><th>Method</th><th>Reviewed</th><th>Errors</th><th>Accuracy</th><th>95% upper error</th><th>&lt;1%</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </section>
    `;
    document.getElementById("backBtn").onclick = load;
  } catch (err) {
    renderError(err);
  }
}

function renderError(err) {
  invalidateReviewContext();
  const message = err?.data?.message || err.message || "Something failed";
  app.innerHTML = `
    <section class="error-panel">
      <h2>${esc(message)}</h2>
      <button id="backHome">Back</button>
    </section>
  `;
  document.getElementById("backHome").onclick = load;
}

reviewerInput.addEventListener("input", () => {
  localStorage.setItem("candyReviewerName", reviewer());
  renderDashboard();
  scheduleMyTasks();
});

// theme (dark / light) with saved preference
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  const b = document.getElementById("themeToggle");
  if (b) b.textContent = t === "dark" ? "Light" : "Dark";
}
applyTheme(localStorage.getItem("permitTheme") || "light");
const themeToggle = document.getElementById("themeToggle");
if (themeToggle) themeToggle.onclick = () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  localStorage.setItem("permitTheme", next);
  applyTheme(next);
};

// reviewer-guide (?) modal
(function () {
  const overlay = document.getElementById("helpOverlay");
  const legacyOverlay = document.getElementById("helpOverlayV1");
  const openBtn = document.getElementById("helpBtn");
  const closeBtn = document.getElementById("helpClose");
  const legacyBtn = document.getElementById("legacyHelpBtn");
  const legacyCloseBtn = document.getElementById("helpCloseV1");
  if (!overlay || !openBtn) return;
  const open = () => { overlay.hidden = false; window.__helpOpen = true; overlay.scrollTop = 0;
    const body = overlay.querySelector(".help-body"); if (body) body.scrollTop = 0; closeBtn && closeBtn.focus(); };
  const close = () => { overlay.hidden = true; if (legacyOverlay) legacyOverlay.hidden = true; window.__helpOpen = false; };
  const openLegacy = () => {
    overlay.hidden = true;
    if (!legacyOverlay) return;
    legacyOverlay.hidden = false;
    legacyOverlay.scrollTop = 0;
    const body = legacyOverlay.querySelector(".help-body"); if (body) body.scrollTop = 0;
    legacyCloseBtn && legacyCloseBtn.focus();
  };
  const closeLegacy = () => { if (legacyOverlay) legacyOverlay.hidden = true; open(); };
  openBtn.onclick = open;
  if (closeBtn) closeBtn.onclick = close;
  if (legacyBtn) legacyBtn.onclick = openLegacy;
  if (legacyCloseBtn) legacyCloseBtn.onclick = closeLegacy;
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  if (legacyOverlay) legacyOverlay.addEventListener("click", (e) => { if (e.target === legacyOverlay) closeLegacy(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && window.__helpOpen) { e.preventDefault(); if (legacyOverlay && !legacyOverlay.hidden) closeLegacy(); else close(); }
  });
})();

refreshBtn.onclick = load;
load().catch(renderError);
