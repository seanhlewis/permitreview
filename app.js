const state = { manifest: null, permits: [], byCategory: new Map(), live: null, category: null, index: 0, openedAt: null, draftTimer: null };
const reviewerInput = document.querySelector("#reviewerName");

function reviewerName() { return reviewerInput.value.trim(); }
function initial() { return (reviewerName()[0] || "?").toUpperCase(); }
function taskKey(category, permit) { return `${category}::${permit.id}`; }
function currentPermit() { return (state.byCategory.get(state.category) || [])[state.index]; }
function currentKey() { const permit = currentPermit(); return permit ? taskKey(state.category, permit) : ""; }
function savedReview(key) { return state.live?.mine?.[key] || null; }
function savedDraft(key) { return state.live?.drafts?.[key] || null; }
function claims(key) { return state.live?.claims?.[key] || []; }
function categoryMeta(code) { return state.live?.categories?.find(c => c.code === code) || state.manifest.categories.find(c => c.code === code); }
function esc(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;", "'":"&#39;"}[c])); }
function apiError(payload, fallback) { return payload?.error === "category_locked" ? `This category is already assigned to ${payload.lockedTo.join(" · ")}.` : payload?.error === "permit_locked" ? `This permit already has two saved reviewers: ${payload.lockedTo.join(" · ")}.` : payload?.error || fallback; }

async function loadLive() {
  const name = reviewerName();
  const response = await fetch(`/api/state?reviewer=${encodeURIComponent(name)}`, {cache:"no-store"});
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(apiError(payload, "The live review state could not be loaded."));
  state.live = payload;
  renderDashboard();
  if (!document.querySelector("#reviewView").hidden) renderReview();
}
async function boot() {
  try {
    const [manifestResponse, permitsResponse] = await Promise.all([fetch("data/manifest.json"), fetch("data/permits.json")]);
    if (!manifestResponse.ok || !permitsResponse.ok) throw new Error("Static permit files could not be loaded");
    state.manifest = await manifestResponse.json(); state.permits = (await permitsResponse.json()).permits;
    for (const category of state.manifest.categories) state.byCategory.set(category.code, state.permits.filter(p => p.category === category.code));
    document.querySelector("#loadStatus").textContent = `${state.manifest.categoryCount} categories loaded · ${state.manifest.categorySlotCount} category permits`;
    await loadLive();
  } catch (error) { document.querySelector("#loadStatus").textContent = `Unable to load live review state: ${error.message}`; }
}

function renderDashboard() {
  const categories = state.manifest.categories.map(c => categoryMeta(c.code));
  const mine = Object.keys(state.live?.mine || {}).length;
  const completeCategories = categories.filter(c => c.myAnswered === c.permitCount).length;
  document.querySelector("#summary").innerHTML = [[mine,"Your saved answers"],[state.manifest.categorySlotCount,"Category permit slots"],[completeCategories,"Completed queues"]].map(([value,label]) => `<div class="summary-card"><span class="summary-value">${value}</span><span class="summary-label">${label}</span></div>`).join("");
  document.querySelector("#categoryGrid").innerHTML = categories.map(category => {
    const locked = !!category.lockedForCurrent;
    const assigned = (category.reviewerInitials || []).join(" · ");
    const complete = category.myAnswered === category.permitCount;
    const status = locked ? "Assigned" : complete ? "Complete" : category.myAnswered ? "In progress" : assigned ? "Open" : "Not started";
    return `<article class="category-card"><div><h3>${esc(category.label)}</h3><div class="category-subline"><span>${category.myAnswered || 0}/${category.permitCount} answered</span><span class="category-status">${status}</span></div></div><div class="claim-preview">${assigned ? `Saved reviewer initials: ${esc(assigned)}` : "No saved reviewer answers"}</div><button class="button primary start-category" data-category="${esc(category.code)}" ${locked ? "disabled" : ""}>${locked ? `Locked to ${esc(assigned)}` : "Review queue"}</button></article>`;
  }).join("");
  document.querySelectorAll(".start-category").forEach(button => button.addEventListener("click", () => openCategory(button.dataset.category)));
}

function openCategory(category) {
  if (!reviewerName()) { reviewerInput.focus(); document.querySelector("#loadStatus").textContent = "Enter your name before opening a review queue."; return; }
  const meta = categoryMeta(category);
  if (meta?.lockedForCurrent) { document.querySelector("#loadStatus").textContent = `This category is already assigned to ${(meta.lockedTo || []).join(" · ")}.`; return; }
  state.category = category; const rows = state.byCategory.get(category) || [];
  const first = rows.findIndex(p => !savedReview(taskKey(category, p)) && claims(taskKey(category, p)).length < 2);
  state.index = first < 0 ? 0 : first; state.openedAt = Date.now();
  document.querySelector("#dashboardView").hidden = true; document.querySelector("#reviewView").hidden = false; renderReview();
}
function elapsedMs() { return state.openedAt ? Math.max(0, Date.now() - state.openedAt) : 0; }
function renderReview() {
  const category = state.category, rows = state.byCategory.get(category) || [], permit = currentPermit();
  if (!permit) return;
  const label = categoryMeta(category)?.label || category;
  document.querySelector("#reviewCategoryEyebrow").textContent = "Live targeted review queue";
  document.querySelector("#reviewCategoryTitle").textContent = label;
  document.querySelector("#reviewerBadge").textContent = `${initial()} · ${reviewerName()}`;
  document.querySelector("#queueCount").textContent = `${rows.length} permits`;
  document.querySelector("#queueList").innerHTML = rows.map((p, index) => { const key = taskKey(category,p), own = !!savedReview(key), initials = claims(key).join(" · "), available = !categoryMeta(category)?.lockedForCurrent && (own || claims(key).length < 2); return `<button class="queue-item ${index === state.index ? "selected" : ""}" data-index="${index}"><span class="queue-number">${index + 1}</span><span class="queue-summary">${esc(p.description || "No description")}</span><span class="queue-reviewers">${own ? "Your answer saved" : initials ? `Saved: ${esc(initials)}` : available ? "Available" : "Two reviewers saved"}</span></button>`; }).join("");
  document.querySelectorAll(".queue-item").forEach(button => button.addEventListener("click", () => { state.index = Number(button.dataset.index); state.openedAt = Date.now(); renderReview(); }));
  document.querySelector("#permitNumber").textContent = permit.permitNumber ? `Permit ${permit.permitNumber}` : "Permit record";
  document.querySelector("#permitProgress").textContent = `${state.index + 1} of ${rows.length}`;
  document.querySelector("#permitDescription").textContent = permit.description || "No description supplied.";
  document.querySelector("#permitMeta").textContent = [permit.city, permit.state, permit.jurisdiction, permit.permitType, permit.workType].filter(Boolean).join(" · ") || "Source metadata unavailable";
  document.querySelector("#claimers").textContent = claims(taskKey(category, permit)).join(" · ") || "None yet";
  const own = savedReview(taskKey(category, permit)), draft = savedDraft(taskKey(category, permit));
  document.querySelectorAll("input[name=answer]").forEach(input => input.checked = own?.answer === input.value);
  document.querySelector("#comment").value = own?.comment ?? draft?.comment ?? "";
  const categoryLockedForCurrent = !!categoryMeta(category)?.lockedForCurrent;
  const permitLocked = !own && claims(taskKey(category, permit)).length >= 2;
  const locked = categoryLockedForCurrent || permitLocked;
  document.querySelector("#lockMessage").hidden = !locked;
  document.querySelector("#lockMessage").textContent = categoryLockedForCurrent ? `This category is assigned to ${(categoryMeta(category).lockedTo || []).join(" · ")}.` : `This permit already has two saved reviewers: ${claims(taskKey(category, permit)).join(" · ")}.`;
  document.querySelector("#answerFieldset").disabled = locked; document.querySelector("#comment").disabled = locked; document.querySelector("#saveNext").disabled = locked;
  document.querySelector("#previousPermit").disabled = state.index === 0;
}

async function saveDraft() {
  if (!state.category || !reviewerName() || !currentPermit()) return;
  const payload = {category:state.category, permitId:currentPermit().id, reviewerName:reviewerName(), comment:document.querySelector("#comment").value, startedAt:new Date(state.openedAt || Date.now()).toISOString(), elapsedMs:elapsedMs()};
  const response = await fetch("/api/drafts", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
  const result = await response.json();
  document.querySelector("#autosaveStatus").textContent = response.ok ? "Comment autosaved to live server" : apiError(result, "Autosave failed");
  if (response.ok) {
    const key = currentKey();
    state.live.drafts[key] = {
      comment: payload.comment,
      startedAt: payload.startedAt,
      savedAt: result.savedAt,
      elapsedMs: result.elapsedMs
    };
  }
}
async function saveAnswer() {
  const permit = currentPermit(); if (!permit) return;
  const answer = document.querySelector("input[name=answer]:checked")?.value;
  if (!answer) { document.querySelector("#autosaveStatus").textContent = "Choose Yes, No, or Unclear before saving."; return; }
  const payload = {category:state.category, permitId:permit.id, reviewerName:reviewerName(), answer, comment:document.querySelector("#comment").value, startedAt:new Date(state.openedAt || Date.now()).toISOString(), elapsedMs:elapsedMs()};
  const response = await fetch("/api/reviews", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok || !result.ok) { document.querySelector("#autosaveStatus").textContent = apiError(result, "The answer could not be saved."); await loadLive(); return; }
  await loadLive();
  const rows = state.byCategory.get(state.category) || [];
  const next = rows.findIndex((p,index) => index > state.index && !savedReview(taskKey(state.category,p)) && claims(taskKey(state.category,p)).length < 2);
  if (next >= 0) state.index = next; state.openedAt = Date.now(); renderReview();
}
function move(delta) { const rows = state.byCategory.get(state.category) || []; state.index = Math.max(0, Math.min(rows.length - 1, state.index + delta)); state.openedAt = Date.now(); renderReview(); }

reviewerInput.addEventListener("input", () => { localStorage.setItem("staticver.reviewerName", reviewerInput.value); if (!document.querySelector("#reviewView").hidden) loadLive().catch(error => document.querySelector("#loadStatus").textContent = error.message); });
document.querySelector("#comment").addEventListener("input", () => { document.querySelector("#autosaveStatus").textContent = "Saving comment…"; clearTimeout(state.draftTimer); state.draftTimer = setTimeout(() => saveDraft().catch(error => document.querySelector("#autosaveStatus").textContent = error.message), 650); });
document.querySelector("#saveNext").addEventListener("click", () => saveAnswer().catch(error => document.querySelector("#autosaveStatus").textContent = error.message));
document.querySelector("#previousPermit").addEventListener("click", () => move(-1));
document.querySelector("#backToQueues").addEventListener("click", () => { document.querySelector("#reviewView").hidden = true; document.querySelector("#dashboardView").hidden = false; loadLive().catch(() => {}); });
document.querySelector("#helpButton").addEventListener("click", () => document.querySelector("#helpDialog").showModal());
document.querySelector("#closeHelp").addEventListener("click", () => document.querySelector("#helpDialog").close());
document.querySelector("#resetButton").addEventListener("click", async () => { if (!reviewerName() || !confirm("Delete your saved live answers, comments, category assignments, and timing records?")) return; const response = await fetch("/api/reset", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({reviewerName:reviewerName()})}); const result = await response.json(); document.querySelector("#loadStatus").textContent = response.ok ? `Reset ${result.deletedReviews} saved answers and ${result.deletedDrafts} drafts.` : apiError(result, "Reset failed"); await loadLive(); });
document.addEventListener("keydown", event => { if (document.querySelector("#reviewView").hidden || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return; if (event.key === "ArrowLeft") move(-1); if (event.key === "ArrowRight") move(1); });
reviewerInput.value = localStorage.getItem("staticver.reviewerName") || "";
boot();
