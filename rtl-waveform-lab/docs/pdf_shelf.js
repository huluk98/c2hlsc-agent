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

let pdfDbPromise;
let activePdfUrl = "";
let activePdfRecord = null;
let selectionGeneration = 0;

function setPdfStatus(message, isError = false) {
  pdfStatus.textContent = message;
  pdfStatus.setAttribute("role", isError ? "alert" : "status");
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
}

async function selectPdf(metadata) {
  const generation = ++selectionGeneration;
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
renderPdfList(true);
