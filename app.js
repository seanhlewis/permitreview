const STORAGE_KEY = "permitreview.static.v1";
const state = { manifest: null, permits: [], byCategory: new Map(), store: loadStore(), category: null, index: 0 };

function loadStore() {
  try { return Object.assign({ reviewerName: "", reviews: {}, drafts: {} }, JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}")); }
  catch { return { reviewerName: "", reviews: {}, drafts: {} }; }
}
function persist() { localStorage.setItem(STORAGE_KEY, JSON.stringify(state.store)); }
function reviewerName() { return document.querySelector("#reviewerName").value.trim(); }
function reviewerKey(name = reviewerName()) { return name.toLowerCase().replace(/\s+/g, " "); }
function initial(name = reviewerName()) { return (name.trim()[0] || "?").toUpperCase(); }
function taskKey(category, permit) { return `${category}::${permit.id}`; }
function records(category, permit) { return state.store.reviews[taskKey(category, permit)] || []; }
function uniqueInitials(category, permit) { return [...new Map(records(category, permit).map(r => [r.reviewerKey, r.initial])).values()]; }
function currentRecord(category, permit) { return records(category, permit).find(r => r.reviewerKey === reviewerKey()) || null; }
function isAnsweredByCurrent(category, permit) { return !!currentRecord(category, permit); }
function canEdit(category, permit) { const own = currentRecord(category, permit); return !!own || uniqueInitials(category, permit).length < (state.manifest?.reviewerSlotsPerPermit || 2); }
function esc(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;", "'":"&#39;"}[c])); }

async function boot() {
  try {
    const [manifestResponse, permitsResponse] = await Promise.all([fetch("data/manifest.json"), fetch("data/permits.json")]);
    if (!manifestResponse.ok || !permitsResponse.ok) throw new Error("Data files could not be loaded");
    state.manifest = await manifestResponse.json();
    state.permits = (await permitsResponse.json()).permits;
    for (const category of state.manifest.categories) state.byCategory.set(category.code, state.permits.filter(p => p.category === category.code));
    document.querySelector("#reviewerName").value = state.store.reviewerName || "";
    document.querySelector("#loadStatus").textContent = `${state.manifest.categoryCount} categories loaded · ${state.manifest.categorySlotCount} category permits`;
    renderDashboard();
  } catch (error) { document.querySelector("#loadStatus").textContent = `Unable to load static data: ${error.message}`; }
}

function renderDashboard() {
  const all = [...state.byCategory.values()].flat();
  const answered = all.filter(p => isAnsweredByCurrent(p.category, p)).length;
  const completedCategories = [...state.byCategory.entries()].filter(([c, rows]) => rows.every(p => isAnsweredByCurrent(c, p))).length;
  document.querySelector("#summary").innerHTML = [
    [answered, "Your saved answers"],
    [all.length, "Category permit slots"],
    [completedCategories, "Completed queues"],
  ].map(([value, label]) => `<div class="summary-card"><span class="summary-value">${value}</span><span class="summary-label">${label}</span></div>`).join("");
  document.querySelector("#categoryGrid").innerHTML = state.manifest.categories.map(category => {
    const rows = state.byCategory.get(category.code) || [];
    const mine = rows.filter(p => isAnsweredByCurrent(category.code, p)).length;
    const initials = [...new Set(rows.flatMap(p => uniqueInitials(category.code, p)))].slice(0, 8);
    const complete = mine === rows.length;
    return `<article class="category-card"><div><h3>${esc(category.label)}</h3><div class="category-subline"><span>${mine}/${rows.length} answered</span><span class="category-status">${complete ? "Complete" : mine ? "In progress" : "Not started"}</span></div></div><div class="claim-preview">${initials.length ? `Saved reviewer initials in this browser: ${initials.map(esc).join(" · ")}` : "No saved reviewer answers in this browser"}</div><button class="button primary start-category" data-category="${esc(category.code)}">Review queue</button></article>`;
  }).join("");
  document.querySelectorAll(".start-category").forEach(button => button.addEventListener("click", () => openCategory(button.dataset.category)));
}

function openCategory(category) {
  if (!reviewerName()) { document.querySelector("#reviewerName").focus(); document.querySelector("#loadStatus").textContent = "Enter your name before opening a review queue."; return; }
  state.store.reviewerName = reviewerName(); persist(); state.category = category;
  const rows = state.byCategory.get(category) || [];
  const first = rows.findIndex(p => canEdit(category, p) && !isAnsweredByCurrent(category, p));
  state.index = first < 0 ? 0 : first;
  document.querySelector("#dashboardView").hidden = true; document.querySelector("#reviewView").hidden = false; renderReview();
}
function renderReview() {
  const category = state.category, rows = state.byCategory.get(category) || [], permit = rows[state.index];
  const categoryLabel = state.manifest.categories.find(c => c.code === category)?.label || category;
  document.querySelector("#reviewCategoryEyebrow").textContent = "Targeted review queue";
  document.querySelector("#reviewCategoryTitle").textContent = categoryLabel;
  document.querySelector("#reviewerBadge").textContent = `${initial()} · ${reviewerName()}`;
  document.querySelector("#queueCount").textContent = `${rows.length} permits`;
  document.querySelector("#queueList").innerHTML = rows.map((p, index) => {
    const initials = uniqueInitials(category, p).join(" · ");
    const own = isAnsweredByCurrent(category, p);
    return `<button class="queue-item ${index === state.index ? "selected" : ""}" data-index="${index}"><span class="queue-number">${index + 1}</span><span class="queue-summary">${esc(p.description || "No description")}</span><span class="queue-reviewers">${own ? "Your answer saved" : initials ? `Saved: ${esc(initials)}` : canEdit(category, p) ? "Available" : "Two reviewers saved"}</span></button>`;
  }).join("");
  document.querySelectorAll(".queue-item").forEach(button => button.addEventListener("click", () => { state.index = Number(button.dataset.index); renderReview(); }));
  document.querySelector("#permitNumber").textContent = permit.permitNumber ? `Permit ${permit.permitNumber}` : "Permit record";
  document.querySelector("#permitProgress").textContent = `${state.index + 1} of ${rows.length}`;
  document.querySelector("#permitDescription").textContent = permit.description || "No description supplied.";
  document.querySelector("#permitMeta").textContent = [permit.city, permit.state, permit.jurisdiction, permit.permitType, permit.workType].filter(Boolean).join(" · ") || "Source metadata unavailable";
  document.querySelector("#claimers").textContent = uniqueInitials(category, permit).join(" · ") || "None yet";
  const own = currentRecord(category, permit), locked = !canEdit(category, permit);
  document.querySelectorAll("input[name=answer]").forEach(input => input.checked = own?.answer === input.value);
  const draftKey = `${taskKey(category, permit)}::${reviewerKey()}`;
  document.querySelector("#comment").value = own?.comment ?? state.store.drafts[draftKey] ?? "";
  document.querySelector("#lockMessage").hidden = !locked;
  document.querySelector("#lockMessage").textContent = "This permit already has two saved reviewer slots in this browser. It is read-only for this reviewer.";
  document.querySelector("#answerFieldset").disabled = locked;
  document.querySelector("#comment").disabled = locked;
  document.querySelector("#saveNext").disabled = locked;
  document.querySelector("#previousPermit").disabled = state.index === 0;
}

function saveAnswer(goNext = true) {
  const category = state.category, permit = (state.byCategory.get(category) || [])[state.index];
  if (!permit || !canEdit(category, permit)) return;
  const answer = document.querySelector("input[name=answer]:checked")?.value;
  if (!answer) { document.querySelector("#autosaveStatus").textContent = "Choose Yes, No, or Unclear before saving."; return; }
  const key = taskKey(category, permit), row = { reviewerKey: reviewerKey(), reviewerName: reviewerName(), initial: initial(), answer, comment: document.querySelector("#comment").value, savedAt: new Date().toISOString() };
  const existing = records(category, permit), ownIndex = existing.findIndex(r => r.reviewerKey === row.reviewerKey);
  if (ownIndex >= 0) existing[ownIndex] = row; else existing.push(row);
  state.store.reviews[key] = existing; delete state.store.drafts[`${key}::${reviewerKey()}`]; state.store.reviewerName = reviewerName(); persist();
  if (goNext) { const next = (state.byCategory.get(category) || []).findIndex((p, index) => index > state.index && canEdit(category, p) && !isAnsweredByCurrent(category, p)); if (next >= 0) state.index = next; else { const any = (state.byCategory.get(category) || []).findIndex(p => canEdit(category, p) && !isAnsweredByCurrent(category, p)); if (any >= 0) state.index = any; } }
  renderReview();
}
function move(delta) { const rows = state.byCategory.get(state.category) || []; state.index = Math.max(0, Math.min(rows.length - 1, state.index + delta)); renderReview(); }

document.querySelector("#reviewerName").addEventListener("input", event => { state.store.reviewerName = event.target.value; persist(); if (!document.querySelector("#reviewView").hidden) renderReview(); });
document.querySelector("#comment").addEventListener("input", event => { if (!state.category) return; const permit = (state.byCategory.get(state.category) || [])[state.index]; if (!permit) return; state.store.drafts[`${taskKey(state.category, permit)}::${reviewerKey()}`] = event.target.value; persist(); document.querySelector("#autosaveStatus").textContent = "Comment autosaved locally"; });
document.querySelector("#saveNext").addEventListener("click", () => saveAnswer(true));
document.querySelector("#previousPermit").addEventListener("click", () => move(-1));
document.querySelector("#backToQueues").addEventListener("click", () => { document.querySelector("#reviewView").hidden = true; document.querySelector("#dashboardView").hidden = false; renderDashboard(); });
document.querySelector("#helpButton").addEventListener("click", () => document.querySelector("#helpDialog").showModal());
document.querySelector("#closeHelp").addEventListener("click", () => document.querySelector("#helpDialog").close());
document.querySelector("#resetButton").addEventListener("click", () => { if (confirm("Delete saved answers and comments from this browser?")) { localStorage.removeItem(STORAGE_KEY); state.store = loadStore(); document.querySelector("#reviewerName").value = ""; if (!document.querySelector("#reviewView").hidden) { document.querySelector("#reviewView").hidden = true; document.querySelector("#dashboardView").hidden = false; } renderDashboard(); } });
document.addEventListener("keydown", event => { if (document.querySelector("#reviewView").hidden || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return; if (event.key === "ArrowLeft") move(-1); if (event.key === "ArrowRight") move(1); });
boot();
