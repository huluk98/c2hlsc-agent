"use strict";

const PDF_DB_NAME = "rtl-sprint-pdf-shelf";
const PDF_DB_VERSION = 1;
const PDF_META_STORE = "pdfMeta";
const PDF_BLOB_STORE = "pdfBlobs";
const PDF_SELECTION_KEY = "rtl-pdf-selected-id";

const pdfInput = document.querySelector("#pdf-input");
const choosePdf = document.querySelector("#choose-pdf");
const pdfDropZone = document.querySelector("#pdf-drop-zone");
const pdfList = document.querySelector("#pdf-list");
const pdfCount = document.querySelector("#pdf-count");
const pdfStatus = document.querySelector("#pdf-status");
const pdfTitle = document.querySelector("#pdf-title");
const pdfMeta = document.querySelector("#pdf-meta");
const pdfViewer = document.querySelector("#pdf-viewer");
const pdfPlaceholder = document.querySelector("#pdf-placeholder");
const openPdf = document.querySelector("#open-pdf");
const downloadPdf = document.querySelector("#download-pdf");
const removePdf = document.querySelector("#remove-pdf");
const coachStage = document.querySelector("#coach-stage");
const coachBadge = document.querySelector("#coach-badge");
const coachTitle = document.querySelector("#coach-title");
const coachSummary = document.querySelector("#coach-summary");
const coachAnnouncement = document.querySelector("#coach-announcement");
const coachSteps = document.querySelector("#coach-steps");
const coachReadings = document.querySelector("#coach-readings");
const coachRuns = document.querySelector("#coach-runs");
const coachSyncForm = document.querySelector("#coach-sync-form");
const coachSyncFields = document.querySelector("#coach-sync-fields");
const coachSyncState = document.querySelector("#coach-sync-state");
const coachSyncMessage = document.querySelector("#coach-sync-message");
const coachSourceType = document.querySelector("#coach-source-type");
const coachSourceIdentity = document.querySelector("#coach-source-identity");
const coachSourceVersion = document.querySelector("#coach-source-version");
const coachPageConvention = document.querySelector("#coach-page-convention");
const coachIdentityConfirmed = document.querySelector("#coach-identity-confirmed");
const coachLocator1 = document.querySelector("#coach-locator-1");
const coachLocator2 = document.querySelector("#coach-locator-2");
const coachLocator1Page = document.querySelector("#coach-locator-1-page");
const coachLocator2Page = document.querySelector("#coach-locator-2-page");
const coachLocator1Confirmed = document.querySelector("#coach-locator-1-confirmed");
const coachLocator2Confirmed = document.querySelector("#coach-locator-2-confirmed");
const saveCoachSync = document.querySelector("#save-coach-sync");
const coachSyncSaveStatus = document.querySelector("#coach-sync-save-status");
const pdfAnalysisConsent = document.querySelector("#pdf-analysis-consent");
const analyzePdfLocally = document.querySelector("#analyze-pdf-locally");
const applyAnalysisLocators = document.querySelector("#apply-analysis-locators");
const pdfAnalysisStatus = document.querySelector("#pdf-analysis-status");
const pdfAnalysisResults = document.querySelector("#pdf-analysis-results");
const pdfCodeStarters = document.querySelector("#pdf-code-starters");
const coachArtifact = document.querySelector("#coach-artifact");
const coachGate = document.querySelector("#coach-gate");
const coachOutputHelp = document.querySelector("#coach-output-help");
const coachOutputWorksheet = document.querySelector("#coach-output-worksheet");
const copyOutputWorksheet = document.querySelector("#copy-output-worksheet");
const coachOutputStatus = document.querySelector("#coach-output-status");
const coachPrompt = document.querySelector("#coach-prompt");
const copyCoachPrompt = document.querySelector("#copy-coach-prompt");
const coachStatus = document.querySelector("#coach-status");

let pdfDbPromise;
let activePdfUrl = "";
let activePdfRecord = null;
let selectionGeneration = 0;
let analysisGeneration = 0;
let analysisAbortController = null;
let activePdfAnalysis = null;

function setPdfStatus(message, isError = false) {
  pdfStatus.textContent = message;
  pdfStatus.setAttribute("role", isError ? "alert" : "status");
}

function setCoachStatus(message, isError = false) {
  coachStatus.textContent = message;
  coachStatus.setAttribute("role", isError ? "alert" : "status");
}

function setSyncSaveStatus(message, isError = false) {
  coachSyncSaveStatus.textContent = message;
  coachSyncSaveStatus.setAttribute("role", isError ? "alert" : "status");
}

function setOutputStatus(message, isError = false) {
  coachOutputStatus.textContent = message;
  coachOutputStatus.setAttribute("role", isError ? "alert" : "status");
}

function setPdfAnalysisStatus(message, isError = false) {
  pdfAnalysisStatus.textContent = message;
  pdfAnalysisStatus.setAttribute("role", isError ? "alert" : "status");
}

function getStoredCoachProfile(metadata) {
  const coach = window.RTL_READING_COACH;
  const fallback = coach?.createEmptyProfile?.() || {
    revision: 1,
    sourceOverride: "",
    identity: { titleAndAuthor: "", version: "", pageConvention: "", confirmed: false },
    blocks: {
      day1: { locators: [{ value: "", viewerPage: 0, confirmed: false }, { value: "", viewerPage: 0, confirmed: false }], analysis: null },
      day2: { locators: [{ value: "", viewerPage: 0, confirmed: false }, { value: "", viewerPage: 0, confirmed: false }], analysis: null },
      day3: { locators: [{ value: "", viewerPage: 0, confirmed: false }, { value: "", viewerPage: 0, confirmed: false }], analysis: null }
    }
  };
  return metadata?.coachProfile && typeof metadata.coachProfile === "object" ? metadata.coachProfile : fallback;
}

function profileForCoach(metadata) {
  return { ...getStoredCoachProfile(metadata), pdfId: metadata?.id || "" };
}

function resetSyncControls() {
  invalidatePdfAnalysis();
  coachSyncFields.disabled = true;
  saveCoachSync.disabled = true;
  pdfAnalysisConsent.checked = false;
  analyzePdfLocally.disabled = true;
  coachSyncState.textContent = "No PDF selected";
  coachSyncState.classList.remove("ready");
  coachSyncMessage.textContent = "Select a PDF, then confirm its identity and two locators for the current day. Filename matching is only a suggestion.";
  coachSourceType.value = "";
  coachSourceIdentity.value = "";
  coachSourceVersion.value = "";
  coachPageConvention.value = "";
  coachIdentityConfirmed.checked = false;
  coachLocator1.value = "";
  coachLocator2.value = "";
  coachLocator1Page.value = "";
  coachLocator2Page.value = "";
  coachLocator1Confirmed.checked = false;
  coachLocator2Confirmed.checked = false;
  clearPdfAnalysisPanel();
  setSyncSaveStatus("");
}

function renderSyncControls(metadata, assignment) {
  if (!metadata || !assignment) {
    resetSyncControls();
    return;
  }
  const profile = getStoredCoachProfile(metadata);
  const blockProfile = profile.blocks?.[coachStage.value] || {};
  const locators = Array.isArray(blockProfile.locators) ? blockProfile.locators : [];
  coachSyncFields.disabled = false;
  saveCoachSync.disabled = false;
  analyzePdfLocally.disabled = !pdfAnalysisConsent.checked;
  coachSyncState.textContent = assignment.sync.ready
    ? "Sync ready"
    : assignment.sync.identityConfirmed ? "Locators pending" : "Identity pending";
  coachSyncState.classList.toggle("ready", assignment.sync.ready);
  coachSyncMessage.textContent = assignment.sync.message;
  coachSourceType.value = profile.sourceOverride || "";
  coachSourceIdentity.value = profile.identity?.titleAndAuthor || "";
  coachSourceVersion.value = profile.identity?.version || "";
  coachPageConvention.value = profile.identity?.pageConvention || "";
  coachIdentityConfirmed.checked = Boolean(profile.identity?.confirmed);
  coachLocator1.value = locators[0]?.value || "";
  coachLocator2.value = locators[1]?.value || "";
  coachLocator1Page.value = locators[0]?.viewerPage || "";
  coachLocator2Page.value = locators[1]?.viewerPage || "";
  coachLocator1Confirmed.checked = Boolean(locators[0]?.confirmed);
  coachLocator2Confirmed.checked = Boolean(locators[1]?.confirmed);
}

function copyProfileForEdit(profile) {
  const coach = window.RTL_READING_COACH;
  const copy = coach?.createEmptyProfile?.() || getStoredCoachProfile(null);
  copy.revision = Number.isInteger(profile.revision) && profile.revision > 0 ? profile.revision : 1;
  copy.sourceOverride = profile.sourceOverride || "";
  copy.identity = {
    titleAndAuthor: profile.identity?.titleAndAuthor || "",
    version: profile.identity?.version || "",
    pageConvention: profile.identity?.pageConvention || "",
    confirmed: Boolean(profile.identity?.confirmed)
  };
  for (const block of ["day1", "day2", "day3"]) {
    const locators = profile.blocks?.[block]?.locators;
    copy.blocks[block] = {
      locators: [0, 1].map((index) => ({
        value: Array.isArray(locators) ? locators[index]?.value || "" : "",
        viewerPage: Array.isArray(locators) && Number.isSafeInteger(Number(locators[index]?.viewerPage))
          ? Math.max(0, Number(locators[index]?.viewerPage))
          : 0,
        confirmed: Boolean(Array.isArray(locators) && locators[index]?.confirmed)
      })),
      analysis: profile.blocks?.[block]?.analysis || null
    };
  }
  return copy;
}

function collectCoachProfile() {
  const sourceOverride = coachSourceType.value;
  const identity = {
    titleAndAuthor: coachSourceIdentity.value.trim(),
    version: coachSourceVersion.value.trim(),
    pageConvention: coachPageConvention.value.trim(),
    confirmed: coachIdentityConfirmed.checked
  };
  const locatorValues = [coachLocator1.value.trim(), coachLocator2.value.trim()];
  const locatorPages = [Number(coachLocator1Page.value), Number(coachLocator2Page.value)];
  const locatorChecks = [coachLocator1Confirmed.checked, coachLocator2Confirmed.checked];
  if (!sourceOverride) throw new Error("Choose the actual source type after checking the opened PDF.");
  if (identity.confirmed && (!identity.titleAndAuthor || !identity.version || !identity.pageConvention)) {
    throw new Error("Fill the title/author, version, and page-number convention before confirming identity.");
  }
  locatorChecks.forEach((confirmed, index) => {
    if (confirmed && !locatorValues[index]) throw new Error(`Enter Reading ${index + 1}'s locator before checking it.`);
    if (confirmed && (!Number.isSafeInteger(locatorPages[index]) || locatorPages[index] < 1)) {
      throw new Error(`Enter Reading ${index + 1}'s positive PDF viewer page before checking it.`);
    }
  });

  const previous = getStoredCoachProfile(activePdfRecord);
  const next = copyProfileForEdit(previous);
  const previousIdentityKey = [
    previous.sourceOverride || "",
    previous.identity?.titleAndAuthor || "",
    previous.identity?.version || "",
    previous.identity?.pageConvention || ""
  ].join("\n");
  const nextIdentityKey = [sourceOverride, identity.titleAndAuthor, identity.version, identity.pageConvention].join("\n");
  const hadSavedIdentity = Boolean(previous.sourceOverride || previous.identity?.titleAndAuthor || previous.identity?.version);
  const identityChanged = hadSavedIdentity && previousIdentityKey !== nextIdentityKey;
  const previousLocators = previous.blocks?.[coachStage.value]?.locators || [];
  const previousLocatorKey = [0, 1].map((index) => [
    previousLocators[index]?.value || "",
    Number(previousLocators[index]?.viewerPage) || 0
  ].join("\n")).join("\n---\n");
  const nextLocatorKey = locatorValues.map((value, index) => [value, locatorPages[index] || 0].join("\n")).join("\n---\n");
  const hadSavedLocators = previousLocators.some((locator) => locator?.value || Number(locator?.viewerPage) > 0);
  const locatorChanged = hadSavedLocators && previousLocatorKey !== nextLocatorKey;
  const changeRequiresReconfirmation = identityChanged || locatorChanged;
  if (changeRequiresReconfirmation) {
    next.revision += 1;
    const blocksToInvalidate = identityChanged ? ["day1", "day2", "day3"] : [coachStage.value];
    for (const block of blocksToInvalidate) {
      next.blocks[block].locators.forEach((locator) => { locator.confirmed = false; });
    }
  }
  next.sourceOverride = sourceOverride;
  next.identity = identity;
  next.blocks[coachStage.value] = {
    locators: locatorValues.map((value, index) => ({
      value,
      viewerPage: Number.isSafeInteger(locatorPages[index]) && locatorPages[index] > 0 ? locatorPages[index] : 0,
      confirmed: changeRequiresReconfirmation ? false : locatorChecks[index]
    })),
    analysis: next.blocks[coachStage.value]?.analysis || null
  };
  return next;
}

function renderCoachPlaceholder(container, message) {
  const placeholder = document.createElement("p");
  placeholder.className = "empty-state";
  placeholder.textContent = message;
  container.replaceChildren(placeholder);
}

function appendCoachDetail(card, label, value, className = "") {
  const row = document.createElement("p");
  if (className) row.className = className;
  const heading = document.createElement("strong");
  heading.textContent = `${label}: `;
  row.append(heading, document.createTextNode(value));
  card.appendChild(row);
}

function invalidatePdfAnalysis() {
  analysisGeneration += 1;
  analysisAbortController?.abort();
  analysisAbortController = null;
}

function clearPdfAnalysisPanel(message = "No local analysis run for this PDF and day.") {
  activePdfAnalysis = null;
  applyAnalysisLocators.disabled = true;
  pdfAnalysisResults.replaceChildren();
  const placeholder = document.createElement("p");
  placeholder.className = "empty-state";
  placeholder.textContent = message;
  pdfAnalysisResults.appendChild(placeholder);
  pdfCodeStarters.replaceChildren();
  setPdfAnalysisStatus("");
}

function validateLocalAnalysis(candidate) {
  const allowedStarterIds = new Set([
    "xor_half_adder", "mux", "registered_stage", "fsm", "self_checking_tb", "hls_reference"
  ]);
  if (!candidate || candidate.schemaVersion !== 1 || candidate.algorithmVersion !== "rtl-study-v1") {
    throw new Error("The local analyzer returned an unsupported result version.");
  }
  if (!Array.isArray(candidate.candidates) || candidate.candidates.length !== 2) {
    throw new Error("The local analyzer did not return exactly two reading candidates.");
  }
  const pages = new Set();
  candidate.candidates.forEach((item) => {
    if (!item?.available || !Number.isSafeInteger(item.viewerPage) || item.viewerPage < 1 || pages.has(item.viewerPage)) {
      throw new Error("The local analyzer returned an invalid or duplicate viewer page.");
    }
    pages.add(item.viewerPage);
    for (const field of ["id", "title", "location", "confidence", "stage", "artifact", "gate", "snippet"]) {
      if (typeof item[field] !== "string") throw new Error(`The local analyzer candidate is missing ${field}.`);
    }
    if (item.snippet.length > 520 || !Array.isArray(item.matchedTerms)) {
      throw new Error("The local analyzer candidate evidence is malformed.");
    }
  });
  if (!Array.isArray(candidate.starters) || candidate.starters.length > 2) {
    throw new Error("The local analyzer returned too many code starters.");
  }
  candidate.starters.forEach((starter) => {
    if (!allowedStarterIds.has(starter?.id) || typeof starter.code !== "string" || starter.code.length > 6000) {
      throw new Error("The local analyzer returned an unsupported code starter.");
    }
    for (const field of ["title", "language", "detectedFrom", "caveat"]) {
      if (typeof starter[field] !== "string") throw new Error(`The code starter is missing ${field}.`);
    }
  });
  if (!/^[0-9a-f]{64}$/.test(candidate.pdfSha256 || "")) {
    throw new Error("The local analyzer result is not bound to a PDF digest.");
  }
  return candidate;
}

function renderPdfAnalysis(analysis, restored = false) {
  if (!analysis) {
    clearPdfAnalysisPanel();
    return;
  }
  let validated;
  try {
    validated = validateLocalAnalysis(analysis);
  } catch (error) {
    clearPdfAnalysisPanel("Saved local analysis is invalid; run it again.");
    setPdfAnalysisStatus(error.message, true);
    return;
  }
  activePdfAnalysis = validated;
  pdfAnalysisResults.replaceChildren();
  validated.candidates.forEach((candidate, index) => {
    const card = document.createElement("article");
    card.className = "pdf-analysis-card";
    const title = document.createElement("h6");
    title.textContent = `${index + 1}. ${candidate.title}`;
    card.appendChild(title);
    appendCoachDetail(card, "Candidate page", String(candidate.viewerPage));
    appendCoachDetail(card, "Heading hint", candidate.location);
    appendCoachDetail(card, "Confidence", `${candidate.confidence} (score ${candidate.score})`);
    appendCoachDetail(card, "Matched concepts", candidate.matchedTerms.join(", "));
    appendCoachDetail(card, "Stage", candidate.stage);
    appendCoachDetail(card, "Produce", candidate.artifact);
    appendCoachDetail(card, "Gate", candidate.gate);
    const snippet = document.createElement("p");
    snippet.className = "analysis-snippet";
    snippet.textContent = `Extracted hint: ${candidate.snippet}`;
    card.appendChild(snippet);
    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "button secondary";
    preview.textContent = `Preview PDF page ${candidate.viewerPage}`;
    preview.addEventListener("click", () => jumpToPdfPage(candidate.viewerPage));
    card.appendChild(preview);
    pdfAnalysisResults.appendChild(card);
  });
  pdfCodeStarters.replaceChildren();
  validated.starters.forEach((starter) => {
    const card = document.createElement("article");
    card.className = "pdf-starter";
    const title = document.createElement("h6");
    title.textContent = starter.title;
    const note = document.createElement("p");
    note.textContent = `${starter.language} - ${starter.caveat}`;
    const code = document.createElement("pre");
    code.textContent = starter.code;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "button secondary";
    copy.textContent = "Copy code starter";
    copy.addEventListener("click", async () => {
      try {
        if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
        await navigator.clipboard.writeText(starter.code);
        setPdfAnalysisStatus(`${starter.title} copied. Verify it against your specification and gates.`);
      } catch (error) {
        setPdfAnalysisStatus("Automatic copy is unavailable. Select and copy the visible starter manually.", true);
      }
    });
    card.append(title, note, code, copy);
    pdfCodeStarters.appendChild(card);
  });
  applyAnalysisLocators.disabled = false;
  setPdfAnalysisStatus(restored
    ? "Restored saved text-only candidates for this PDF and day. Preview before applying."
    : "Two text-only candidates found and saved locally. Preview both before applying.");
}

async function persistPdfAnalysis(recordId, block, analysis) {
  const db = await openPdfDb();
  const transaction = db.transaction(PDF_META_STORE, "readwrite");
  const completed = transactionComplete(transaction);
  const store = transaction.objectStore(PDF_META_STORE);
  const record = await requestResult(store.get(recordId));
  if (!record) {
    await completed;
    throw new DOMException("The selected PDF metadata no longer exists.", "NotFoundError");
  }
  const profile = copyProfileForEdit(getStoredCoachProfile(record));
  profile.blocks[block].analysis = { ...analysis, analyzedAt: new Date().toISOString() };
  const updatedRecord = { ...record, coachProfile: profile };
  store.put(updatedRecord);
  await completed;
  return updatedRecord;
}

async function responseJson(response) {
  if (!response.headers.get("Content-Type")?.includes("application/json")) return {};
  try {
    return await response.json();
  } catch (error) {
    return {};
  }
}

function localAnalysisError(response, payload) {
  if ([404, 501].includes(response.status)) {
    return "The static server cannot analyze PDFs. Stop it, then restart this lab with make serve.";
  }
  return payload?.error?.message || `Local PDF analysis failed with HTTP ${response.status}.`;
}

function jumpToPdfPage(requestedPage) {
  const page = Number(requestedPage);
  if (!activePdfRecord || !activePdfUrl || !Number.isSafeInteger(page) || page < 1) {
    setPdfStatus("Select a synchronized PDF reading with a valid viewer page first.", true);
    return;
  }
  pdfViewer.src = `${activePdfUrl}#page=${page}`;
  const scrollBehavior = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  pdfViewer.scrollIntoView({ behavior: scrollBehavior, block: "start" });
  setPdfStatus(`Requested PDF viewer page ${page} for ${activePdfRecord.name}.`);
}

function renderCoachResources(container, resources) {
  container.replaceChildren();
  resources.forEach((resource) => {
    const card = document.createElement("article");
    card.className = "coach-resource";

    const top = document.createElement("div");
    top.className = "coach-resource-top";
    const kind = document.createElement("span");
    kind.className = "coach-resource-kind";
    kind.textContent = resource.kind;
    const stage = document.createElement("span");
    stage.className = "coach-resource-stage";
    stage.textContent = resource.stage;
    top.append(kind, stage);

    const title = document.createElement("h5");
    if (resource.url) {
      const link = document.createElement("a");
      link.href = resource.url;
      link.textContent = resource.title;
      if (resource.url.startsWith("https://")) {
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      }
      title.appendChild(link);
    } else {
      title.textContent = resource.title;
    }

    const detail = document.createElement("p");
    detail.textContent = resource.detail;
    card.append(top, title, detail);
    appendCoachDetail(card, "Produce", resource.artifact);
    appendCoachDetail(card, "Gate", resource.gate);
    appendCoachDetail(card, "Source", resource.source);
    if (resource.kind === "reading") {
      appendCoachDetail(
        card,
        resource.locatorConfirmed ? "Synced locator" : "Locator",
        resource.locator,
        resource.locatorConfirmed ? "coach-resource-locator" : ""
      );
      if (resource.locatorConfirmed && Number.isSafeInteger(resource.viewerPage) && resource.viewerPage > 0) {
        const jumpButton = document.createElement("button");
        jumpButton.type = "button";
        jumpButton.className = "button secondary coach-jump";
        jumpButton.textContent = `Jump to PDF page ${resource.viewerPage}`;
        jumpButton.addEventListener("click", () => jumpToPdfPage(resource.viewerPage));
        card.appendChild(jumpButton);
      }
    }
    appendCoachDetail(card, "Availability", resource.availability, "coach-resource-availability");
    container.appendChild(card);
  });
}

function renderReadingCoach(metadata) {
  setCoachStatus("");
  setOutputStatus("");
  coachAnnouncement.textContent = "";
  if (!metadata) {
    coachBadge.textContent = "No PDF selected";
    coachTitle.textContent = "Select a PDF to get a reading assignment";
    coachSummary.textContent = "The coach treats filenames only as hints. Optional detection runs only after consent on this computer; PDFs are never sent to Codex or the internet.";
    coachSteps.replaceChildren();
    [
      "Add or select a PDF from your shelf.",
      "Confirm the source identity before following its assignment."
    ].forEach((step) => {
      const item = document.createElement("li");
      item.textContent = step;
      coachSteps.appendChild(item);
    });
    renderCoachPlaceholder(coachReadings, "Select a PDF to map two readings.");
    renderCoachPlaceholder(coachRuns, "Select a PDF to load two trusted runs.");
    renderSyncControls(null, null);
    coachArtifact.textContent = "A stage-specific artifact will appear here.";
    coachGate.textContent = "A matching evidence gate will appear here.";
    coachOutputHelp.textContent = "Synchronize the PDF identity and both reading locators to unlock a worksheet tied to this PDF and day.";
    coachOutputWorksheet.textContent = "Select and synchronize a PDF to generate the worksheet.";
    copyOutputWorksheet.disabled = true;
    coachPrompt.textContent = "Select a PDF to generate the request.";
    copyCoachPrompt.disabled = true;
    return;
  }

  const coach = window.RTL_READING_COACH;
  if (!coach) {
    coachBadge.textContent = "Coach unavailable";
    coachTitle.textContent = "Reload the study page";
    coachSummary.textContent = "The reading-coach data did not load; your PDF remains stored and untouched.";
    coachSteps.replaceChildren();
    renderCoachPlaceholder(coachReadings, "Reading assignments are unavailable until the page reloads.");
    renderCoachPlaceholder(coachRuns, "Video and guided-run assignments are unavailable until the page reloads.");
    renderSyncControls(null, null);
    coachArtifact.textContent = "Use the full PDF workflow manually.";
    coachGate.textContent = "Do not mark a reading stage complete until the coach or map is available.";
    coachOutputHelp.textContent = "The output worksheet is unavailable until the reading coach reloads.";
    coachOutputWorksheet.textContent = "Reload the page to generate the worksheet.";
    copyOutputWorksheet.disabled = true;
    coachPrompt.textContent = "Reload to generate the analysis request.";
    copyCoachPrompt.disabled = true;
    return;
  }

  const profile = profileForCoach(metadata);
  const assignment = coach.getReadingAssignment(metadata.name, coachStage.value, profile);
  coachBadge.textContent = assignment.badge;
  coachTitle.textContent = assignment.title;
  coachSummary.textContent = assignment.summary;
  coachSteps.replaceChildren();
  assignment.steps.forEach((step) => {
    const item = document.createElement("li");
    item.textContent = step;
    coachSteps.appendChild(item);
  });
  renderCoachResources(coachReadings, assignment.readings);
  renderCoachResources(coachRuns, assignment.runs);
  renderSyncControls(metadata, assignment);
  renderPdfAnalysis(getStoredCoachProfile(metadata).blocks?.[coachStage.value]?.analysis || null, true);
  coachArtifact.textContent = assignment.artifact;
  coachGate.textContent = assignment.gate;
  coachOutputHelp.textContent = assignment.sync.ready
    ? "This worksheet is tied to the selected PDF record, identity revision, day, two locators, and two trusted runs."
    : "SYNC PENDING: save a learner-confirmed identity plus both section labels and positive viewer pages for this day.";
  coachOutputWorksheet.textContent = coach.buildOutputWorksheet(metadata.name, coachStage.value, profile);
  copyOutputWorksheet.disabled = !assignment.sync.ready;
  coachPrompt.textContent = coach.buildCodexPrompt(metadata.name, coachStage.value, profile);
  copyCoachPrompt.disabled = false;
  coachAnnouncement.textContent = `${assignment.blockLabel}: two readings and two video/guided runs loaded. ${assignment.sync.message}`;
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function transactionComplete(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error || new DOMException("Storage transaction aborted", "AbortError"));
    transaction.onerror = () => reject(transaction.error || new DOMException("Storage transaction failed", "UnknownError"));
  });
}

function openPdfDb() {
  if (!("indexedDB" in window)) {
    return Promise.reject(new DOMException("IndexedDB is unavailable", "InvalidStateError"));
  }
  if (pdfDbPromise) return pdfDbPromise;

  pdfDbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(PDF_DB_NAME, PDF_DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(PDF_META_STORE)) {
        db.createObjectStore(PDF_META_STORE, { keyPath: "id" });
      }
      if (!db.objectStoreNames.contains(PDF_BLOB_STORE)) {
        db.createObjectStore(PDF_BLOB_STORE, { keyPath: "id" });
      }
    };
    request.onblocked = () => reject(new DOMException("Close other tabs using this study page, then reload.", "InvalidStateError"));
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const db = request.result;
      db.onversionchange = () => {
        db.close();
        pdfDbPromise = undefined;
        setPdfStatus("The PDF shelf changed in another tab. Reload this page before continuing.", true);
      };
      resolve(db);
    };
  });
  return pdfDbPromise;
}

async function listPdfMetadata() {
  const db = await openPdfDb();
  const transaction = db.transaction(PDF_META_STORE, "readonly");
  const completed = transactionComplete(transaction);
  const records = await requestResult(transaction.objectStore(PDF_META_STORE).getAll());
  await completed;
  return records.sort((left, right) => right.addedAt - left.addedAt || right.name.localeCompare(left.name));
}

async function getPdfBlob(id) {
  const db = await openPdfDb();
  const transaction = db.transaction(PDF_BLOB_STORE, "readonly");
  const completed = transactionComplete(transaction);
  const record = await requestResult(transaction.objectStore(PDF_BLOB_STORE).get(id));
  await completed;
  return record;
}

async function updatePdfMetadata(metadata) {
  const db = await openPdfDb();
  const transaction = db.transaction(PDF_META_STORE, "readwrite");
  const completed = transactionComplete(transaction);
  transaction.objectStore(PDF_META_STORE).put(metadata);
  await completed;
}

async function storePdf(file) {
  const db = await openPdfDb();
  const id = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const metadata = {
    id,
    name: file.name,
    size: file.size,
    type: file.type || "application/pdf",
    lastModified: file.lastModified || 0,
    addedAt: Date.now()
  };
  const transaction = db.transaction([PDF_META_STORE, PDF_BLOB_STORE], "readwrite");
  const completed = transactionComplete(transaction);
  transaction.objectStore(PDF_META_STORE).put(metadata);
  transaction.objectStore(PDF_BLOB_STORE).put({ id, blob: file });
  await completed;
  return metadata;
}

async function deleteStoredPdf(id) {
  const db = await openPdfDb();
  const transaction = db.transaction([PDF_META_STORE, PDF_BLOB_STORE], "readwrite");
  const completed = transactionComplete(transaction);
  transaction.objectStore(PDF_META_STORE).delete(id);
  transaction.objectStore(PDF_BLOB_STORE).delete(id);
  await completed;
}

function readableStorageError(error) {
  if (error?.name === "QuotaExceededError") return "Not enough browser storage. Remove a shelf PDF or free device/site storage, then retry.";
  if (["SecurityError", "InvalidStateError"].includes(error?.name)) return error?.message || "Browser storage is unavailable. Use the served 127.0.0.1 page and check private-mode settings.";
  if (error?.name === "NotReadableError") return "The browser could not read that file. Select it again from disk.";
  if (["AbortError", "UnknownError"].includes(error?.name)) return "The PDF was not committed to storage. Please retry.";
  if (error?.name === "VersionError") return "This shelf was created by newer code. It was left untouched; reload with the newer page.";
  return error?.message || "The PDF shelf could not complete that action.";
}

async function validatePdf(file) {
  if (!file || file.size === 0) throw new Error("Empty files cannot be added.");
  if (!file.name.toLowerCase().endsWith(".pdf")) throw new Error(`${file.name} does not use a .pdf filename.`);
  if (file.type && file.type !== "application/pdf") throw new Error(`${file.name} is not identified as a PDF.`);
  const header = await file.slice(0, 1024).text();
  if (!header.includes("%PDF-")) throw new Error(`${file.name} does not contain a PDF header.`);
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function updateSelectedPdfButton() {
  document.querySelectorAll(".pdf-item").forEach((button) => {
    const selected = button.dataset.id === activePdfRecord?.id;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}

function revokeActivePdfUrl() {
  if (activePdfUrl) URL.revokeObjectURL(activePdfUrl);
  activePdfUrl = "";
}

function clearPdfViewer() {
  selectionGeneration += 1;
  revokeActivePdfUrl();
  activePdfRecord = null;
  pdfViewer.removeAttribute("src");
  pdfViewer.hidden = true;
  pdfPlaceholder.hidden = false;
  pdfTitle.textContent = "Select a PDF to read";
  pdfMeta.textContent = "The full document will appear below.";
  openPdf.href = "#";
  openPdf.setAttribute("aria-disabled", "true");
  openPdf.tabIndex = -1;
  removePdf.disabled = true;
  downloadPdf.href = "#";
  downloadPdf.removeAttribute("download");
  downloadPdf.setAttribute("aria-disabled", "true");
  downloadPdf.tabIndex = -1;
  localStorage.removeItem(PDF_SELECTION_KEY);
  updateSelectedPdfButton();
  renderReadingCoach(null);
}

async function selectPdf(metadata) {
  const generation = ++selectionGeneration;
  invalidatePdfAnalysis();
  pdfAnalysisConsent.checked = false;
  analyzePdfLocally.disabled = true;
  pdfList.setAttribute("aria-busy", "true");
  try {
    const stored = await getPdfBlob(metadata.id);
    if (generation !== selectionGeneration) return;
    if (!stored?.blob) throw new Error("The PDF bytes are missing from browser storage.");
    revokeActivePdfUrl();
    activePdfRecord = metadata;
    activePdfUrl = URL.createObjectURL(stored.blob);
    pdfTitle.textContent = metadata.name;
    pdfMeta.textContent = `${formatBytes(metadata.size)} · added ${new Date(metadata.addedAt).toLocaleDateString()}`;
    pdfViewer.title = `PDF reader: ${metadata.name}`;
    pdfViewer.src = activePdfUrl;
    pdfViewer.hidden = false;
    pdfPlaceholder.hidden = true;
    openPdf.href = activePdfUrl;
    openPdf.setAttribute("aria-disabled", "false");
    openPdf.tabIndex = 0;
    removePdf.disabled = false;
    removePdf.setAttribute("aria-label", `Remove ${metadata.name}`);
    downloadPdf.href = activePdfUrl;
    downloadPdf.download = metadata.name;
    downloadPdf.setAttribute("aria-disabled", "false");
    downloadPdf.tabIndex = 0;
    localStorage.setItem(PDF_SELECTION_KEY, metadata.id);
    updateSelectedPdfButton();
    renderReadingCoach(metadata);
    setPdfStatus(`${metadata.name} is ready to read.`);
  } catch (error) {
    if (generation === selectionGeneration) setPdfStatus(readableStorageError(error), true);
  } finally {
    if (generation === selectionGeneration) pdfList.setAttribute("aria-busy", "false");
  }
}

async function renderPdfList(restoreSelection = false) {
  pdfList.setAttribute("aria-busy", "true");
  try {
    const records = await listPdfMetadata();
    pdfCount.textContent = `${records.length} ${records.length === 1 ? "file" : "files"}`;
    pdfList.replaceChildren();
    if (records.length === 0) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No PDFs added yet.";
      pdfList.appendChild(empty);
      if (activePdfRecord) clearPdfViewer();
      return records;
    }
    records.forEach((record) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "pdf-item";
      button.dataset.id = record.id;
      button.setAttribute("aria-pressed", String(record.id === activePdfRecord?.id));
      const name = document.createElement("strong");
      name.textContent = record.name;
      const details = document.createElement("span");
      details.textContent = `${formatBytes(record.size)} · ${new Date(record.addedAt).toLocaleDateString()}`;
      button.append(name, details);
      button.addEventListener("click", () => selectPdf(record));
      pdfList.appendChild(button);
    });
    if (restoreSelection) {
      const savedId = localStorage.getItem(PDF_SELECTION_KEY);
      const saved = records.find((record) => record.id === savedId);
      if (saved) await selectPdf(saved);
      else if (savedId) localStorage.removeItem(PDF_SELECTION_KEY);
    } else {
      updateSelectedPdfButton();
    }
    return records;
  } catch (error) {
    setPdfStatus(readableStorageError(error), true);
    return [];
  } finally {
    pdfList.setAttribute("aria-busy", "false");
  }
}

async function addPdfFiles(fileList) {
  const files = Array.from(fileList || []);
  if (files.length === 0) return;
  pdfList.setAttribute("aria-busy", "true");
  let newest = null;
  const failures = [];
  for (const file of files) {
    try {
      await validatePdf(file);
      newest = await storePdf(file);
    } catch (error) {
      failures.push(`${file.name || "Unnamed file"}: ${readableStorageError(error)}`);
    }
  }
  pdfInput.value = "";
  await renderPdfList();
  if (newest) await selectPdf(newest);
  const successCount = files.length - failures.length;
  if (failures.length) {
    setPdfStatus(`${successCount} PDF${successCount === 1 ? "" : "s"} added; ${failures.length} rejected. ${failures.join(" ")}`, true);
  } else {
    setPdfStatus(`${successCount} full PDF${successCount === 1 ? "" : "s"} saved on this device.`);
  }
  pdfList.setAttribute("aria-busy", "false");
}

choosePdf.addEventListener("click", () => pdfInput.click());
pdfInput.addEventListener("change", () => addPdfFiles(pdfInput.files));
["dragenter", "dragover"].forEach((eventName) => {
  pdfDropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    pdfDropZone.classList.add("dragover");
  });
});
["dragleave", "drop"].forEach((eventName) => {
  pdfDropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    pdfDropZone.classList.remove("dragover");
  });
});
pdfDropZone.addEventListener("drop", (event) => addPdfFiles(event.dataTransfer.files));
coachStage.addEventListener("change", () => {
  invalidatePdfAnalysis();
  pdfAnalysisConsent.checked = false;
  analyzePdfLocally.disabled = true;
  setSyncSaveStatus("");
  renderReadingCoach(activePdfRecord);
});
pdfAnalysisConsent.addEventListener("change", () => {
  analyzePdfLocally.disabled = !activePdfRecord || !pdfAnalysisConsent.checked;
});
analyzePdfLocally.addEventListener("click", async () => {
  if (!activePdfRecord || !pdfAnalysisConsent.checked) return;
  invalidatePdfAnalysis();
  analysisAbortController = new AbortController();
  const controller = analysisAbortController;
  const runGeneration = analysisGeneration;
  const selectedGeneration = selectionGeneration;
  const record = activePdfRecord;
  const block = coachStage.value;
  const priorAnalysis = getStoredCoachProfile(record).blocks?.[block]?.analysis || null;
  analyzePdfLocally.disabled = true;
  applyAnalysisLocators.disabled = true;
  setPdfAnalysisStatus("Extracting and ranking this PDF on 127.0.0.1...");
  try {
    const stored = await getPdfBlob(record.id);
    if (!stored?.blob) throw new Error("The selected PDF bytes are missing from browser storage.");
    const sessionResponse = await fetch("/api/v1/session", {
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
      signal: controller.signal
    });
    const sessionPayload = await responseJson(sessionResponse);
    if (!sessionResponse.ok || typeof sessionPayload.token !== "string") {
      throw new Error(localAnalysisError(sessionResponse, sessionPayload));
    }
    const analysisResponse = await fetch(`/api/v1/pdf-candidates/${block}`, {
      method: "POST",
      body: stored.blob,
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
      headers: {
        "Content-Type": "application/pdf",
        "X-Study-Token": sessionPayload.token
      },
      signal: controller.signal
    });
    const payload = await responseJson(analysisResponse);
    if (!analysisResponse.ok) throw new Error(localAnalysisError(analysisResponse, payload));
    const validated = validateLocalAnalysis(payload);
    if (runGeneration !== analysisGeneration || selectedGeneration !== selectionGeneration || activePdfRecord?.id !== record.id || coachStage.value !== block) return;
    const updatedRecord = await persistPdfAnalysis(record.id, block, validated);
    if (runGeneration !== analysisGeneration || selectedGeneration !== selectionGeneration || activePdfRecord?.id !== record.id || coachStage.value !== block) return;
    activePdfRecord = updatedRecord;
    await renderPdfList();
    renderReadingCoach(activePdfRecord);
    renderPdfAnalysis(validated, false);
  } catch (error) {
    if (error.name !== "AbortError" && runGeneration === analysisGeneration) {
      if (priorAnalysis) renderPdfAnalysis(priorAnalysis, true);
      else clearPdfAnalysisPanel("No qualified local candidates are available for this PDF and day.");
      setPdfAnalysisStatus(error.message || "Local PDF analysis failed.", true);
    }
  } finally {
    if (runGeneration === analysisGeneration) {
      analysisAbortController = null;
      analyzePdfLocally.disabled = !activePdfRecord || !pdfAnalysisConsent.checked;
    }
  }
});
applyAnalysisLocators.addEventListener("click", () => {
  if (!activePdfAnalysis || activePdfAnalysis.candidates.length !== 2) return;
  const [first, second] = activePdfAnalysis.candidates;
  coachLocator1.value = first.location;
  coachLocator1Page.value = String(first.viewerPage);
  coachLocator1Confirmed.checked = false;
  coachLocator2.value = second.location;
  coachLocator2Page.value = String(second.viewerPage);
  coachLocator2Confirmed.checked = false;
  setSyncSaveStatus("Candidates filled as unconfirmed locators. Preview both pages, then save; check them only after the content matches.");
});
coachSyncForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!activePdfRecord) return;
  let coachProfile;
  try {
    coachProfile = collectCoachProfile();
  } catch (error) {
    setSyncSaveStatus(error.message || "The PDF synchronization fields are incomplete.", true);
    return;
  }

  const recordId = activePdfRecord.id;
  const generation = selectionGeneration;
  const updatedRecord = { ...activePdfRecord, coachProfile };
  saveCoachSync.disabled = true;
  setSyncSaveStatus("Saving synchronization on this device...");
  try {
    await updatePdfMetadata(updatedRecord);
    if (generation !== selectionGeneration || activePdfRecord?.id !== recordId) return;
    activePdfRecord = updatedRecord;
    await renderPdfList();
    renderReadingCoach(activePdfRecord);
    const assignment = window.RTL_READING_COACH.getReadingAssignment(
      activePdfRecord.name,
      coachStage.value,
      profileForCoach(activePdfRecord)
    );
    setSyncSaveStatus(assignment.sync.ready
      ? "Saved. Both reading cards now jump to this PDF, and the output worksheet is unlocked."
      : assignment.sync.message);
  } catch (error) {
    setSyncSaveStatus(readableStorageError(error), true);
  } finally {
    if (generation === selectionGeneration && activePdfRecord?.id === recordId) saveCoachSync.disabled = false;
  }
});
copyOutputWorksheet.addEventListener("click", async () => {
  if (!activePdfRecord || copyOutputWorksheet.disabled || !coachOutputWorksheet.textContent) return;
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
    await navigator.clipboard.writeText(coachOutputWorksheet.textContent);
    setOutputStatus("Output worksheet copied. Fill it as you read and run each resource.");
  } catch (error) {
    setOutputStatus("Automatic copy is unavailable. Select and copy the visible worksheet manually.", true);
  }
});
copyCoachPrompt.addEventListener("click", async () => {
  if (!activePdfRecord || !coachPrompt.textContent) return;
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
    await navigator.clipboard.writeText(coachPrompt.textContent);
    setCoachStatus("Analysis request copied. Attach the PDF in this conversation before pasting it.");
  } catch (error) {
    setCoachStatus("Automatic copy is unavailable. Select and copy the visible request manually.", true);
  }
});
removePdf.addEventListener("click", async () => {
  if (!activePdfRecord) return;
  const record = activePdfRecord;
  if (!window.confirm(`Remove ${record.name} from this browser shelf? Your original file is not affected.`)) return;
  pdfList.setAttribute("aria-busy", "true");
  try {
    await deleteStoredPdf(record.id);
    clearPdfViewer();
    await renderPdfList();
    setPdfStatus(`${record.name} was removed from this browser shelf.`);
    choosePdf.focus();
  } catch (error) {
    setPdfStatus(readableStorageError(error), true);
  } finally {
    pdfList.setAttribute("aria-busy", "false");
  }
});
window.addEventListener("pagehide", (event) => {
  if (!event.persisted) revokeActivePdfUrl();
});
renderReadingCoach(null);
renderPdfList(true);
