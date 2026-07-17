const state = {
  limit: "10",
  sort: "modified",
  search: "",
  documents: [],
  selected: new Set(),
  selectedDocuments: new Map(),
  loading: false,
  exporting: false,
  loadRequestId: 0,
};

const els = {
  connectionStatus: document.querySelector("#connectionStatus"),
  refreshButton: document.querySelector("#refreshButton"),
  searchForm: document.querySelector("#searchForm"),
  searchButton: document.querySelector("#searchButton"),
  searchInput: document.querySelector("#searchInput"),
  limitButtons: [...document.querySelectorAll("[data-limit]")],
  sortButtons: [...document.querySelectorAll("[data-sort]")],
  syncToggle: document.querySelector("#syncToggle"),
  cleanExportsToggle: document.querySelector("#cleanExportsToggle"),
  singleDeckToggle: document.querySelector("#singleDeckToggle"),
  singleDeckName: document.querySelector("#singleDeckName"),
  documentList: document.querySelector("#documentList"),
  selectedCount: document.querySelector("#selectedCount"),
  statusText: document.querySelector("#statusText"),
  selectVisibleButton: document.querySelector("#selectVisibleButton"),
  clearSelectionButton: document.querySelector("#clearSelectionButton"),
  exportButton: document.querySelector("#exportButton"),
  toast: document.querySelector("#toast"),
  resultPanel: document.querySelector("#resultPanel"),
  resultTitle: document.querySelector("#resultTitle"),
  resultList: document.querySelector("#resultList"),
  closeResultButton: document.querySelector("#closeResultButton"),
};

els.selectVisibleButton.textContent = "Tout sélectionner";

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setBusy(isBusy) {
  state.loading = isBusy;
  els.refreshButton.disabled = isBusy || state.exporting;
  els.searchButton.disabled = state.exporting;
  els.searchInput.disabled = state.exporting;
  els.cleanExportsToggle.disabled = state.exporting;
  els.singleDeckToggle.disabled = state.exporting || state.selected.size < 2;
  els.singleDeckName.disabled =
    state.exporting || state.selected.size < 2 || !els.singleDeckToggle.checked;
  els.limitButtons.forEach((button) => {
    button.disabled = isBusy || state.exporting;
  });
  els.sortButtons.forEach((button) => {
    button.disabled = isBusy || state.exporting;
  });
  updateActions();
}

function updateActions() {
  const selectedCount = state.selected.size;
  els.selectedCount.textContent = `${selectedCount} sélectionné${selectedCount > 1 ? "s" : ""}`;
  els.exportButton.disabled = selectedCount === 0 || state.loading || state.exporting;
  els.clearSelectionButton.disabled = selectedCount === 0 || state.exporting;
  els.selectVisibleButton.disabled = state.documents.length === 0 || state.exporting;
  els.cleanExportsToggle.disabled = state.exporting;
  els.singleDeckToggle.disabled = state.exporting || selectedCount < 2;
  els.singleDeckName.disabled =
    state.exporting || selectedCount < 2 || !els.singleDeckToggle.checked;
}

function showToast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle("error", isError);
  els.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    els.toast.hidden = true;
  }, 4200);
}

async function fetchJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    throw new Error(
      "Impossible de joindre le serveur local. Si le reMarkable dort, reveille-le manuellement puis clique Actualiser pour reessayer."
    );
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Erreur HTTP ${response.status}`);
  }
  return payload;
}

async function loadDocuments(force = false) {
  const requestId = ++state.loadRequestId;
  setBusy(true);
  els.statusText.textContent = "Chargement";
  els.documentList.innerHTML = `<div class="loading-state">Chargement</div>`;
  const params = new URLSearchParams({
    limit: state.limit,
    sort: state.sort,
    search: state.search,
    force: force ? "1" : "0",
  });
  try {
    const payload = await fetchJson(`/api/documents?${params.toString()}`);
    if (requestId !== state.loadRequestId) {
      return;
    }
    state.documents = payload.documents;
    state.documents.forEach((doc) => {
      if (state.selected.has(doc.uuid)) {
        state.selectedDocuments.set(doc.uuid, doc);
      }
    });
    els.connectionStatus.textContent = payload.host ? `reMarkable ${payload.host}` : "Local";
    els.statusText.textContent = `${payload.total} fichier${payload.total > 1 ? "s" : ""}`;
    renderDocuments();
  } catch (error) {
    if (requestId !== state.loadRequestId) {
      return;
    }
    state.documents = [];
    els.statusText.textContent = "Erreur";
    els.documentList.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
    showToast(error.message, true);
  } finally {
    if (requestId === state.loadRequestId) {
      setBusy(false);
    }
  }
}

function applySearch(force = false) {
  state.search = els.searchInput.value;
  loadDocuments(force);
}

function renderDocuments() {
  const rows = displayDocuments();
  if (rows.length === 0) {
    els.documentList.innerHTML = `<div class="empty-state">Aucun fichier</div>`;
    updateActions();
    return;
  }

  els.documentList.innerHTML = rows
    .map((doc) => {
      const selected = state.selected.has(doc.uuid);
      const typeClass = doc.type.toLowerCase();
      return `
        <button class="doc-row${selected ? " selected" : ""}" type="button" data-uuid="${escapeHtml(doc.uuid)}" aria-pressed="${selected}">
          <span class="select-dot" aria-hidden="true"></span>
          <span class="type-pill ${escapeHtml(typeClass)}">${escapeHtml(doc.type)}</span>
          <span class="file-name">
            <strong>${escapeHtml(doc.name)}</strong>
            <span>${escapeHtml(doc.uuid)}</span>
          </span>
          <span class="modified">${escapeHtml(doc.modifiedText)}</span>
        </button>
      `;
    })
    .join("");

  document.querySelectorAll(".doc-row").forEach((row) => {
    row.addEventListener("click", () => toggleDocument(row.dataset.uuid));
  });
  updateActions();
}

function displayDocuments() {
  const visibleIds = new Set(state.documents.map((doc) => doc.uuid));
  const pinnedSelected = [...state.selectedDocuments.values()].filter(
    (doc) => state.selected.has(doc.uuid) && !visibleIds.has(doc.uuid)
  );
  return [...pinnedSelected, ...state.documents];
}

function toggleDocument(uuid) {
  if (!uuid || state.exporting) {
    return;
  }
  if (state.selected.has(uuid)) {
    state.selected.delete(uuid);
    state.selectedDocuments.delete(uuid);
  } else {
    state.selected.add(uuid);
    const doc = displayDocuments().find((item) => item.uuid === uuid);
    if (doc) {
      state.selectedDocuments.set(uuid, doc);
    }
  }
  renderDocuments();
}

function selectVisible() {
  state.documents.forEach((doc) => {
    state.selected.add(doc.uuid);
    state.selectedDocuments.set(doc.uuid, doc);
  });
  renderDocuments();
}

function clearSelection() {
  state.selected.clear();
  state.selectedDocuments.clear();
  renderDocuments();
}

function selectedUuidsInDisplayOrder() {
  const visibleSelected = displayDocuments()
    .map((doc) => doc.uuid)
    .filter((uuid) => state.selected.has(uuid));
  const visibleSet = new Set(visibleSelected);
  const hiddenSelected = [...state.selected].filter((uuid) => !visibleSet.has(uuid));
  return [...visibleSelected, ...hiddenSelected];
}

async function exportSelected() {
  if (state.selected.size === 0 || state.exporting) {
    return;
  }
  const uuids = selectedUuidsInDisplayOrder();
  const useSingleDeck = els.singleDeckToggle.checked && uuids.length > 1;
  state.exporting = true;
  updateActions();
  els.exportButton.textContent = "Export";
  els.statusText.textContent = "Export en cours";
  els.resultTitle.textContent = "Export en cours";
  els.resultList.innerHTML = "";
  els.resultPanel.hidden = false;
  try {
    const job = await fetchJson("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        uuids,
        syncAnkiweb: els.syncToggle.checked,
        singleDeck: useSingleDeck,
        singleDeckName: els.singleDeckName.value,
        cleanExportsAfterSuccess: els.cleanExportsToggle.checked,
      }),
    });
    await pollExportJob(job.id);
  } catch (error) {
    els.statusText.textContent = "Erreur export";
    els.resultTitle.textContent = "Erreur export";
    els.resultList.innerHTML = renderJobLog([], error.message);
    els.resultPanel.hidden = false;
    showToast(error.message, true);
    state.exporting = false;
    els.exportButton.textContent = "Exporter";
    updateActions();
  }
}

async function pollExportJob(jobId) {
  while (state.exporting) {
    const job = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}`);
    renderJobProgress(job);
    if (job.status === "done") {
      els.resultTitle.textContent = "export terminé";
      renderResults((job.result && job.result.results) || []);
      els.statusText.textContent = "Export terminé";
      showToast("export terminé");
      state.exporting = false;
      els.exportButton.textContent = "Exporter";
      updateActions();
      return;
    }
    if (job.status === "error") {
      renderJobProgress(job);
      els.resultTitle.textContent = "Erreur export";
      els.statusText.textContent = "Erreur export";
      showToast(job.error || "Erreur export", true);
      state.exporting = false;
      els.exportButton.textContent = "Exporter";
      updateActions();
      return;
    }
    await delay(1000);
  }
}

function delay(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function renderJobProgress(job) {
  const progress = job.progress || {};
  const total = progress.total || 0;
  const current = progress.current || 0;
  const phase = progress.phase || "export";
  const currentDocument = progress.currentDocument || "";
  const title = total ? `Export ${current}/${total}` : "Export en cours";
  els.resultTitle.textContent = currentDocument ? `${title} · ${currentDocument}` : title;
  els.statusText.textContent = currentDocument
    ? `${phase} · ${currentDocument}`
    : phase;

  const documents = progress.documents || [];
  const documentsHtml = documents
    .map((document) => `
      <div class="result-item ${document.status === "error" ? "error" : ""}">
        <div>
          <strong>${escapeHtml(document.name)}</strong>
          <span>${escapeHtml(document.message || document.status)}</span>
        </div>
        <span>${escapeHtml(document.status)}</span>
      </div>
    `)
    .join("");
  els.resultList.innerHTML = documentsHtml + renderJobLog(job.log, job.error);
}

function renderResults(results) {
  if (results.length === 0) {
    els.resultList.innerHTML = "";
    return;
  }
  els.resultList.innerHTML = results
    .map((result) => {
      const anki = result.anki;
      const hasHighlights = result.highlightCount > 0;
      const deckText = anki && anki.deck ? ` - ${anki.deck}` : "";
      const cleanText = result.cleanedExports
        ? ` · exports nettoyés (${result.cleanedExports})`
        : "";
      const ankiText = anki
        ? `${anki.added} ajouté${anki.added > 1 ? "s" : ""}, ${anki.updated} mis à jour, ${anki.deleted} supprimé${anki.deleted > 1 ? "s" : ""}`
        : hasHighlights
          ? "Anki non modifié"
          : "Aucun highlight détecté";
      const syncText = anki
        ? anki.webSync
          ? "Sync AnkiWeb ok"
          : "Sync AnkiWeb désactivée"
        : hasHighlights
          ? "Sync AnkiWeb non lancée"
          : "Anki non ouvert";
      return `
        <div class="result-item">
          <div>
            <strong>${escapeHtml(result.document)}</strong>
            <span>${result.highlightCount} highlight${result.highlightCount > 1 ? "s" : ""} · ${escapeHtml(ankiText)}${escapeHtml(cleanText)}</span>
            <span>${escapeHtml(deckText)}</span>
          </div>
          <span>${escapeHtml(syncText)}</span>
        </div>
      `;
    })
    .join("");
}

function renderJobLog(log = [], error = "") {
  const lines = [...(log || [])];
  if (error && !lines.some((line) => line.includes(error))) {
    lines.push(`Erreur: ${error}`);
  }
  if (lines.length === 0) {
    return "";
  }
  return `<pre class="job-log">${escapeHtml(lines.join("\n"))}</pre>`;
}

els.refreshButton.addEventListener("click", () => applySearch(true));
els.searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  applySearch(false);
});
els.searchInput.addEventListener("input", (event) => {
  if (event.target.value === "" && state.search !== "") {
    applySearch(false);
  }
});
els.singleDeckToggle.addEventListener("change", updateActions);
els.limitButtons.forEach((button) => {
  button.addEventListener("click", () => {
    state.limit = button.dataset.limit;
    state.search = els.searchInput.value;
    els.limitButtons.forEach((item) => item.classList.toggle("active", item === button));
    loadDocuments(false);
  });
});
els.sortButtons.forEach((button) => {
  button.addEventListener("click", () => {
    state.sort = button.dataset.sort;
    state.search = els.searchInput.value;
    els.sortButtons.forEach((item) => item.classList.toggle("active", item === button));
    loadDocuments(false);
  });
});
els.selectVisibleButton.addEventListener("click", selectVisible);
els.clearSelectionButton.addEventListener("click", clearSelection);
els.exportButton.addEventListener("click", exportSelected);
els.closeResultButton.addEventListener("click", () => {
  els.resultPanel.hidden = true;
});

loadDocuments(false);
