// Telegram Resumable Uploader Client App
let selectedFiles = [];
let currentTasks = [];
let ws = null;
let wsReconnectTimer = null;
let scanDebounceTimer = null;
let duplicateCheckDebounceTimer = null;
let duplicateMatches = {};

// Initialization
function initApp() {
  setupDropzone();
  checkStatus();
  fetchTasks();
  fetchDialogs();
  loadCacheStats();
  connectWebSocket();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initApp);
} else {
  initApp();
}

// Periodic status check & refresh when tab becomes active
setInterval(checkStatus, 6000);
window.addEventListener("focus", () => {
  checkStatus();
  fetchDialogs();
  fetchTasks();
});

// Toast Notifications
function showToast(message, type = "info") {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerText = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(100%)";
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

// Tab Switching
function switchTab(tabId) {
  document.querySelectorAll(".tab-btn").forEach(btn => btn.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));

  if (tabId === "upload") {
    document.querySelector(".tab-btn:nth-child(1)").classList.add("active");
    document.getElementById("tab-upload").classList.add("active");
  } else if (tabId === "queue") {
    document.querySelector(".tab-btn:nth-child(2)").classList.add("active");
    document.getElementById("tab-queue").classList.add("active");
    fetchTasks();
    loadCacheStats();
  } else if (tabId === "settings") {
    document.querySelector(".tab-btn:nth-child(3)").classList.add("active");
    document.getElementById("tab-settings").classList.add("active");
    loadSettings();
    loadCacheStats();
  }
}

// Dropzone & Multiple File/Folder Handling
function setupDropzone() {
  const dropzone = document.getElementById("dropzone");
  const miniDropzone = document.getElementById("mini-dropzone");
  const dropTargets = [dropzone, miniDropzone].filter(Boolean);

  // Prevent browser from navigating away or opening dropped files anywhere on the page
  window.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.stopPropagation();
  });
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
  });

  dropTargets.forEach(target => {
    let dragCounter = 0;

    target.addEventListener("dragenter", (e) => {
      e.preventDefault();
      e.stopPropagation();
      dragCounter++;
      target.classList.add("dragover");
    });

    target.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = "copy";
      target.classList.add("dragover");
    });

    target.addEventListener("dragleave", (e) => {
      e.preventDefault();
      e.stopPropagation();
      dragCounter--;
      if (dragCounter <= 0) {
        dragCounter = 0;
        target.classList.remove("dragover");
      }
    });

    target.addEventListener("drop", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      dragCounter = 0;
      target.classList.remove("dragover");

      console.log("Drop event triggered. Inspecting dataTransfer...");

      // 1. Immediately and synchronously grab raw files list before any await
      const directFiles = Array.from(e.dataTransfer.files || []);

      // 2. Synchronously extract entries from items before any async boundary
      const entries = [];
      if (e.dataTransfer.items && e.dataTransfer.items.length > 0) {
        for (let i = 0; i < e.dataTransfer.items.length; i++) {
          const item = e.dataTransfer.items[i];
          if (item && item.webkitGetAsEntry) {
            try {
              const entry = item.webkitGetAsEntry();
              if (entry) entries.push(entry);
            } catch (err) {
              console.warn("webkitGetAsEntry error:", err);
            }
          }
        }
      }

      // 3. If directory entries exist, recursively traverse them
      if (entries.length > 0) {
        showToast("Scanning dropped files and folders...", "info");
        try {
          const fileLists = await Promise.all(entries.map(entry => scanEntryRecursively(entry)));
          const extracted = fileLists.flat(Infinity).filter(f => f && f.name && f.size > 0);
          if (extracted.length > 0) {
            handleFilesSelected(extracted);
            return;
          }
        } catch (err) {
          console.warn("Entry traversal error, falling back to direct files:", err);
        }
      }

      // 4. Fallback to direct files
      if (directFiles.length > 0) {
        handleFilesSelected(directFiles);
      } else {
        showToast("No uploadable files found in the dropped item.", "warning");
      }
    });
  });
}

// Recursively traverse dropped directories and files
async function scanEntryRecursively(entry) {
  if (!entry) return [];
  if (entry.isFile) {
    return new Promise(resolve => {
      entry.file(
        file => resolve([file]),
        err => {
          console.warn("Could not read file entry:", err);
          resolve([]);
        }
      );
    });
  } else if (entry.isDirectory) {
    const dirReader = entry.createReader();
    const readEntriesBatch = () => new Promise(resolve => {
      dirReader.readEntries(
        entries => resolve(entries || []),
        err => {
          console.warn("Could not read directory batch:", err);
          resolve([]);
        }
      );
    });

    let allEntries = [];
    let batch = await readEntriesBatch();
    while (batch.length > 0) {
      allEntries.push(...batch);
      batch = await readEntriesBatch();
    }

    const subPromises = allEntries.map(child => scanEntryRecursively(child));
    const subFiles = await Promise.all(subPromises);
    return subFiles.flat(Infinity);
  }
  return [];
}

function handleFilesSelected(fileList) {
  if (!fileList || fileList.length === 0) return;
  
  let addedCount = 0;
  for (let i = 0; i < fileList.length; i++) {
    const f = fileList[i];
    if (!f || !f.name) continue;
    // Avoid exact duplicate additions by name & size
    const exists = selectedFiles.some(x => x.name === f.name && x.size === f.size);
    if (!exists) {
      selectedFiles.push(f);
      addedCount++;
    }
  }

  // Clear local path input to avoid ambiguity
  const localInput = document.getElementById("local-path-input");
  if (localInput) localInput.value = "";
  const preview = document.getElementById("local-scan-preview");
  if (preview) preview.style.display = "none";

  renderSelectedFiles();
  debounceCheckDuplicates();

  if (addedCount > 0) {
    showToast(`Added ${addedCount} file${addedCount > 1 ? 's' : ''} to upload queue! Total: ${selectedFiles.length}`, "success");
  } else {
    showToast("Selected files are already queued.", "info");
  }
}

function renderSelectedFiles() {
  const card = document.getElementById("selected-files-card");
  const list = document.getElementById("selected-files-list");
  const summary = document.getElementById("selected-summary");
  const totalSize = document.getElementById("selected-total-size");
  const dropzone = document.getElementById("dropzone");
  const dupBanner = document.getElementById("selected-duplicates-banner");
  const dupCountSpan = document.getElementById("dup-banner-count");

  if (!card || !list || !dropzone) return;

  if (selectedFiles.length === 0) {
    card.style.display = "none";
    dropzone.style.display = "block";
    if (dupBanner) dupBanner.style.display = "none";
    return;
  }

  card.style.display = "flex";
  dropzone.style.display = "none";

  const totalBytes = selectedFiles.reduce((acc, f) => acc + (f.size || 0), 0);
  summary.innerText = `${selectedFiles.length} file${selectedFiles.length > 1 ? 's' : ''} queued`;
  totalSize.innerText = `(${formatBytes(totalBytes)})`;

  // Count exact duplicates
  let exactDupCount = 0;

  list.innerHTML = selectedFiles.map((f, idx) => {
    const icon = getFileIcon(f.name, "auto");
    const key = `${f.name}::${f.size}`;
    const dupMatch = duplicateMatches[key];

    let badgeHtml = "";
    let itemStyle = "background: var(--bg-card); border: 1px solid var(--border-color);";

    if (dupMatch) {
      if (dupMatch.status === "exact_duplicate") {
        exactDupCount++;
        itemStyle = "background: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.35);";
        const msgRef = dupMatch.matched_with?.message_id ? `#${dupMatch.matched_with.message_id}` : 'In Group';
        badgeHtml = `<span style="background: rgba(239, 68, 68, 0.2); color: var(--accent-red); padding: 1px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; white-space: nowrap; flex-shrink: 0;">⚠️ In Group (${msgRef})</span>`;
      } else if (dupMatch.status === "same_name_diff_size") {
        itemStyle = "background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.35);";
        badgeHtml = `<span style="background: rgba(245, 158, 11, 0.2); color: var(--accent-amber); padding: 1px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; white-space: nowrap; flex-shrink: 0;">⚡ Name match</span>`;
      } else if (dupMatch.status === "same_size_diff_name") {
        badgeHtml = `<span style="background: rgba(59, 130, 246, 0.15); color: var(--tg-blue); padding: 1px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; white-space: nowrap; flex-shrink: 0;">ℹ️ Size match</span>`;
      }
    }

    return `
      <div style="display: flex; align-items: center; justify-content: space-between; padding: 0.4rem 0.65rem; border-radius: var(--radius-sm); ${itemStyle} font-size: 0.8rem; gap: 0.5rem; min-width: 0;">
        <div style="display: flex; align-items: center; gap: 0.4rem; overflow: hidden; white-space: nowrap; min-width: 0; flex: 1;">
          <span style="flex-shrink: 0;">${icon}</span>
          <span style="font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1;" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
          <span style="font-size: 0.72rem; color: var(--text-secondary); white-space: nowrap; flex-shrink: 0;">(${formatBytes(f.size)})</span>
          ${badgeHtml}
        </div>
        <button type="button" onclick="removeSelectedFile(${idx})" style="background: none; border: none; color: var(--accent-red); cursor: pointer; font-size: 0.9rem; padding: 2px 6px; flex-shrink: 0;" title="Remove this file">✕</button>
      </div>
    `;
  }).join("");

  // Update duplicate banner in card
  if (dupBanner) {
    if (exactDupCount > 0) {
      dupBanner.style.display = "flex";
      if (dupCountSpan) dupCountSpan.innerText = `${exactDupCount} file${exactDupCount > 1 ? 's' : ''}`;
    } else {
      dupBanner.style.display = "none";
    }
  }
}

function removeSelectedFile(index) {
  selectedFiles.splice(index, 1);
  renderSelectedFiles();
  debounceCheckDuplicates();
}

function clearSelectedFiles() {
  selectedFiles = [];
  duplicateMatches = {};
  const fileInput = document.getElementById("file-input");
  const folderInput = document.getElementById("folder-input");
  if (fileInput) fileInput.value = "";
  if (folderInput) folderInput.value = "";
  renderSelectedFiles();
}

// Remove files from active selection that are already exact duplicates in Telegram
function removeDuplicateSelectedFiles() {
  const beforeCount = selectedFiles.length;
  selectedFiles = selectedFiles.filter(f => {
    const key = `${f.name}::${f.size}`;
    const match = duplicateMatches[key];
    return !(match && match.status === "exact_duplicate");
  });
  const removed = beforeCount - selectedFiles.length;
  if (removed > 0) {
    showToast(`Removed ${removed} duplicate file${removed > 1 ? 's' : ''} from upload queue!`, "info");
  }
  renderSelectedFiles();
  debounceCheckDuplicates();
}

// Debounced Duplicate Verification
function debounceCheckDuplicates() {
  clearTimeout(duplicateCheckDebounceTimer);
  duplicateCheckDebounceTimer = setTimeout(checkDuplicatesForSelectedFiles, 350);
}

async function checkDuplicatesForSelectedFiles() {
  const targetChat = (document.getElementById("target-chat-input")?.value || "").trim();
  const dupBanner = document.getElementById("selected-duplicates-banner");

  if (!targetChat || selectedFiles.length === 0) {
    duplicateMatches = {};
    if (dupBanner) dupBanner.style.display = "none";
    renderSelectedFiles();
    return;
  }

  try {
    const payload = {
      target_chat: targetChat,
      files: selectedFiles.map(f => ({ name: f.name, size: f.size }))
    };

    const res = await fetch("/api/check-duplicates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!res.ok) return;
    const data = await res.json();

    duplicateMatches = {};
    if (data.file_matches) {
      data.file_matches.forEach(m => {
        if (m.file && m.file.name) {
          const key = `${m.file.name}::${m.file.size}`;
          duplicateMatches[key] = m;
        }
      });
    }

    renderSelectedFiles();
  } catch (err) {
    console.warn("Duplicate check error:", err);
  }
}

// Duplicate Scanner Modal
async function openDuplicateScannerModal() {
  const modal = document.getElementById("duplicate-modal");
  const body = document.getElementById("duplicate-modal-body");
  const targetChat = (document.getElementById("target-chat-input")?.value || "").trim();

  if (!targetChat) {
    showToast("Please select or enter a destination chat first.", "warning");
    return;
  }

  if (modal) modal.style.display = "flex";
  if (body) {
    body.innerHTML = `
      <div style="text-align: center; padding: 2.5rem 1rem;">
        <div style="font-size: 2.2rem; margin-bottom: 0.75rem;">🔄</div>
        <div style="font-weight: 600; font-size: 1rem; color: var(--text-primary); margin-bottom: 0.25rem;">
          Scanning Group Media &amp; Upload History...
        </div>
        <div style="font-size: 0.8rem; color: var(--text-secondary);">
          Checking Telegram group dialog and indexed uploads for target: <code>${escapeHtml(targetChat)}</code>
        </div>
      </div>
    `;
  }

  try {
    const res = await fetch("/api/check-duplicates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_chat: targetChat,
        files: selectedFiles.map(f => ({ name: f.name, size: f.size }))
      })
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || `Server returned ${res.status}`);
    }

    const data = await res.json();
    renderDuplicateModalContent(data);
  } catch (err) {
    if (body) {
      body.innerHTML = `
        <div style="padding: 1.5rem; background: rgba(239, 68, 68, 0.1); border-radius: var(--radius-sm); border: 1px solid rgba(239, 68, 68, 0.3); color: var(--accent-red); font-size: 0.85rem;">
          <strong>Error scanning duplicates:</strong> ${escapeHtml(err.message)}
        </div>
      `;
    }
  }
}

function closeDuplicateScannerModal() {
  const modal = document.getElementById("duplicate-modal");
  if (modal) modal.style.display = "none";
}

function renderDuplicateModalContent(data) {
  const body = document.getElementById("duplicate-modal-body");
  if (!body) return;

  const totalFiles = data.total_known_files_in_group || 0;
  const internalDups = data.internal_duplicates || [];
  const selectedDups = (data.file_matches || []).filter(m => m.status === "exact_duplicate");
  const targetChat = data.target_chat || "";

  let html = `
    <div style="display: flex; flex-direction: column; gap: 1rem;">
      
      <!-- Stats Overview Cards -->
      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 0.75rem;">
        <div style="background: var(--bg-card); padding: 0.75rem; border-radius: var(--radius-sm); border: 1px solid var(--border-color); text-align: center;">
          <div style="font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase;">Indexed Files</div>
          <div style="font-size: 1.35rem; font-weight: 700; color: var(--tg-blue); margin-top: 2px;">${totalFiles}</div>
          <div style="font-size: 0.7rem; color: var(--text-secondary); margin-top: 2px;">Uploaded to this group</div>
        </div>

        <div style="background: var(--bg-card); padding: 0.75rem; border-radius: var(--radius-sm); border: 1px solid var(--border-color); text-align: center;">
          <div style="font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase;">Internal Duplicates</div>
          <div style="font-size: 1.35rem; font-weight: 700; color: ${internalDups.length > 0 ? 'var(--accent-red)' : 'var(--accent-green)'}; margin-top: 2px;">
            ${internalDups.length}
          </div>
          <div style="font-size: 0.7rem; color: var(--text-secondary); margin-top: 2px;">Duplicate copies in group</div>
        </div>

        <div style="background: var(--bg-card); padding: 0.75rem; border-radius: var(--radius-sm); border: 1px solid var(--border-color); text-align: center;">
          <div style="font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase;">Queue Matches</div>
          <div style="font-size: 1.35rem; font-weight: 700; color: ${selectedDups.length > 0 ? 'var(--accent-amber)' : 'var(--accent-green)'}; margin-top: 2px;">
            ${selectedDups.length}
          </div>
          <div style="font-size: 0.7rem; color: var(--text-secondary); margin-top: 2px;">Already in group</div>
        </div>
      </div>
  `;

  // Notice about bot privacy if present
  if (data.error_notice) {
    html += `
      <div style="padding: 0.65rem 0.85rem; background: rgba(59, 130, 246, 0.08); border-left: 3px solid var(--tg-blue); border-radius: 4px; font-size: 0.78rem; color: var(--text-secondary);">
        ℹ️ <strong>Group Media History:</strong> Deduplication is powered by your local indexed database (<strong>${data.db_history_count}</strong> completed uploads) which tracks every single file sent to this group with exact byte-size and filename matching.
      </div>
    `;
  }

  // Section 1: Queue Duplicates (if files are currently queued)
  if (selectedFiles.length > 0) {
    html += `
      <div style="background: var(--bg-card); border-radius: var(--radius-sm); border: 1px solid var(--border-color); padding: 0.85rem;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.6rem; flex-wrap: wrap; gap: 0.5rem;">
          <strong style="font-size: 0.88rem;">Queued Files Verification (${selectedFiles.length} files)</strong>
          ${selectedDups.length > 0 ? `
            <button type="button" class="btn btn-secondary btn-sm" onclick="removeDuplicateSelectedFiles(); openDuplicateScannerModal();" style="font-size: 0.75rem; padding: 2px 8px;">
              ⚡ Skip ${selectedDups.length} Duplicates from Queue
            </button>
          ` : `<span style="font-size: 0.75rem; color: var(--accent-green); font-weight: 600;">✅ All Queued Files are New</span>`}
        </div>

        <div style="max-height: 140px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px;">
          ${(data.file_matches || []).map(m => {
            const isDup = m.status === 'exact_duplicate';
            return `
              <div style="display: flex; align-items: center; justify-content: space-between; padding: 0.35rem 0.6rem; background: ${isDup ? 'rgba(239, 68, 68, 0.08)' : 'var(--bg-secondary)'}; border-radius: 4px; font-size: 0.78rem; gap: 0.5rem;">
                <div style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;">
                  <span>${isDup ? '⚠️' : '✅'}</span>
                  <span style="font-weight: 500;">${escapeHtml(m.file.name)}</span>
                  <span style="opacity: 0.7; font-size: 0.72rem;">(${formatBytes(m.file.size)})</span>
                </div>
                <span style="font-size: 0.72rem; color: ${isDup ? 'var(--accent-red)' : 'var(--accent-green)'}; font-weight: 600; white-space: nowrap;">
                  ${escapeHtml(m.reason)}
                </span>
              </div>
            `;
          }).join('')}
        </div>
      </div>
    `;
  }

  // Section 2: Internal duplicates inside group
  if (internalDups.length > 0) {
    html += `
      <div style="background: var(--bg-card); border-radius: var(--radius-sm); border: 1px solid rgba(239, 68, 68, 0.3); padding: 0.85rem;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.6rem;">
          <strong style="font-size: 0.88rem; color: var(--accent-red);">
            ⚠️ Duplicate Copies Found in Group (${internalDups.length})
          </strong>
        </div>
        <div style="max-height: 160px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px;">
          ${internalDups.map(dup => {
            const extraMsgs = dup.messages.slice(1).map(m => m.message_id).filter(Boolean);
            return `
              <div style="padding: 0.5rem 0.65rem; background: var(--bg-secondary); border-radius: 4px; font-size: 0.78rem; display: flex; justify-content: space-between; align-items: center; gap: 0.5rem;">
                <div>
                  <div style="font-weight: 600;">${escapeHtml(dup.file_name)} (${formatBytes(dup.file_size)})</div>
                  <div style="color: var(--text-secondary); font-size: 0.72rem; margin-top: 2px;">
                    Appears ${dup.occurrences} times &bull; Message IDs: ${dup.messages.map(m => '#' + (m.message_id || 'DB')).join(', ')}
                  </div>
                </div>
                ${extraMsgs.length > 0 ? `
                  <button type="button" class="btn btn-danger btn-sm" onclick="deleteDuplicateMessages('${escapeHtml(targetChat)}', [${extraMsgs.join(',')}])" style="font-size: 0.72rem; padding: 2px 7px; white-space: nowrap;">
                    🗑 Remove Extra Copy
                  </button>
                ` : ''}
              </div>
            `;
          }).join('')}
        </div>
      </div>
    `;
  } else {
    html += `
      <div style="padding: 0.85rem; background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.25); border-radius: var(--radius-sm); font-size: 0.82rem; color: var(--accent-green); display: flex; align-items: center; gap: 0.5rem;">
        <span style="font-size: 1.1rem;">✅</span>
        <div>
          <strong>No Duplicate Copies Found in Group:</strong> All ${totalFiles} files registered in this destination are completely unique!
        </div>
      </div>
    `;
  }

  html += `
      <div style="display: flex; justify-content: flex-end; margin-top: 0.5rem;">
        <button type="button" class="btn btn-secondary btn-sm" onclick="closeDuplicateScannerModal()">
          Close
        </button>
      </div>
    </div>
  `;

  body.innerHTML = html;
}

async function deleteDuplicateMessages(targetChat, messageIds) {
  if (!confirm(`Are you sure you want to delete ${messageIds.length} duplicate message(s) from Telegram?`)) {
    return;
  }
  showToast("Deleting duplicate messages from Telegram...", "info");
  try {
    const res = await fetch("/api/group/delete-messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_chat: targetChat,
        message_ids: messageIds
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to delete messages");

    showToast(`Successfully deleted ${data.deleted_count} duplicate message(s)!`, "success");
    openDuplicateScannerModal();
  } catch (err) {
    showToast(`Delete failed: ${err.message}`, "error");
  }
}

// Local Server Path / Folder Scanning
function debounceScanLocalPath() {
  clearTimeout(scanDebounceTimer);
  scanDebounceTimer = setTimeout(scanLocalPath, 500);
}

async function scanLocalPath() {
  const pathVal = document.getElementById("local-path-input").value.trim();
  const preview = document.getElementById("local-scan-preview");
  const summary = document.getElementById("local-scan-summary");
  const filesList = document.getElementById("local-scan-files");
  const recursive = document.getElementById("local-recursive-checkbox").checked;

  if (!pathVal) {
    preview.style.display = "none";
    return;
  }

  try {
    const res = await fetch("/api/scan-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_path: pathVal, recursive: recursive })
    });
    const data = await res.json();
    if (!res.ok) {
      preview.style.display = "block";
      summary.innerHTML = `<span style="color: var(--accent-red);">⚠️ ${escapeHtml(data.detail || "Path not found")}</span>`;
      filesList.innerHTML = "";
      return;
    }

    preview.style.display = "block";
    const typeLabel = data.is_dir ? "Directory" : "Single File";
    summary.innerHTML = `✅ ${typeLabel} with <strong>${data.count}</strong> file${data.count === 1 ? '' : 's'} (${formatBytes(data.total_size)}) ready to queue`;

    if (data.files && data.files.length > 0) {
      filesList.innerHTML = data.files.slice(0, 15).map(f => `
        <div style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
          📄 ${escapeHtml(f.name)} <span style="opacity: 0.7;">(${formatBytes(f.size)})</span>
        </div>
      `).join("") + (data.count > 15 ? `<div style="font-style: italic; opacity: 0.7;">...and ${data.count - 15} more files</div>` : "");
    } else {
      filesList.innerHTML = `<div style="color: var(--accent-amber);">No uploadable files found in this directory.</div>`;
    }
  } catch (err) {
    preview.style.display = "block";
    summary.innerHTML = `<span style="color: var(--accent-red);">Error scanning path: ${escapeHtml(err.message)}</span>`;
    filesList.innerHTML = "";
  }
}

function handleDialogSelected(val) {
  if (val) {
    document.getElementById("target-chat-input").value = val;
    debounceCheckDuplicates();
  }
}

// Fetch Status & Connection Info
async function checkStatus() {
  const badge = document.getElementById("auth-status-badge");
  const text = document.getElementById("auth-status-text");

  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    updateAuthUI(data.auth);
    updateStatsUI(data.stats);
  } catch (err) {
    console.error("Status check failed:", err);
    if (badge && text) {
      badge.className = "status-badge disconnected";
      text.innerText = "Offline (Reconnecting...)";
    }
  }
}

function updateAuthUI(auth) {
  const badge = document.getElementById("auth-status-badge");
  const text = document.getElementById("auth-status-text");
  const logoutContainer = document.getElementById("logout-container");
  const loggedUserName = document.getElementById("logged-user-name");
  const loggedUserSub = document.getElementById("logged-user-sub");

  if (!badge || !text) return;

  if (auth && auth.is_authorized && auth.user) {
    badge.className = "status-badge connected";
    const name = auth.user.first_name || auth.user.username || "Connected";
    const role = auth.user.is_bot ? "Bot" : "User";
    text.innerText = `${name} (${role})`;

    if (logoutContainer) logoutContainer.style.display = "block";
    if (loggedUserName) loggedUserName.innerText = name;
    if (loggedUserSub) loggedUserSub.innerText = `@${auth.user.username || auth.user.id} • ${role}`;
  } else {
    badge.className = "status-badge disconnected";
    text.innerText = (auth && auth.error) ? "Auth Error (Click Settings)" : "Disconnected (Click Settings)";
    if (logoutContainer) logoutContainer.style.display = "none";
  }
}

function updateStatsUI(stats) {
  if (!stats) return;
  const activeEl = document.getElementById("stat-active");
  const completedEl = document.getElementById("stat-completed");
  const transferredEl = document.getElementById("stat-transferred");
  const queueBadge = document.getElementById("queue-badge");

  if (activeEl) activeEl.innerText = stats.active_tasks || 0;
  if (completedEl) completedEl.innerText = stats.completed_tasks || 0;
  if (transferredEl) transferredEl.innerText = formatBytes(stats.total_bytes_transferred || 0);

  if (queueBadge) {
    if (stats.active_tasks > 0) {
      queueBadge.innerText = stats.active_tasks;
      queueBadge.style.display = "inline-block";
    } else {
      queueBadge.style.display = "none";
    }
  }
}

// Fetch Dialogs / Chats
async function fetchDialogs() {
  const select = document.getElementById("dialog-select");
  const targetInput = document.getElementById("target-chat-input");
  if (!select) return;

  try {
    const res = await fetch("/api/dialogs");
    const data = await res.json();
    select.innerHTML = '<option value="">-- Choose from your Telegram dialogs --</option>';

    if (data.dialogs && data.dialogs.length > 0) {
      data.dialogs.forEach(d => {
        const opt = document.createElement("option");
        opt.value = d.id;
        const icon = d.type === "channel" ? "📢" : (d.type === "group" ? "👥" : (d.type === "saved" ? "⭐" : "👤"));
        opt.innerText = `${icon} ${d.title} (${d.type})`;
        select.appendChild(opt);
      });

      // If user hasn't typed a destination yet, pre-select the detected dialog!
      if (targetInput && (!targetInput.value.trim() || targetInput.value.trim().length === 0)) {
        select.selectedIndex = 1;
        targetInput.value = data.dialogs[0].id;
        debounceCheckDuplicates();
      }
    }
  } catch (err) {
    console.error("Failed to fetch dialogs:", err);
  }
}

// Live Streaming Notice Toggle
function toggleStreamingNotice() {
  const isDoc = document.getElementById("force-doc-checkbox").checked;
  const mediaType = document.getElementById("media-type-select").value;
  const notice = document.getElementById("streaming-badge-notice");
  if (!notice) return;

  if (isDoc || mediaType === "document") {
    notice.style.background = "rgba(245, 158, 11, 0.1)";
    notice.style.borderColor = "rgba(245, 158, 11, 0.25)";
    notice.style.color = "var(--accent-amber)";
    notice.innerHTML = `<span>📁</span><span><strong>Document Mode:</strong> Sent as an uncompressed file. Telegram will require downloading it before playing.</span>`;
  } else {
    notice.style.background = "rgba(16, 185, 129, 0.1)";
    notice.style.borderColor = "rgba(16, 185, 129, 0.25)";
    notice.style.color = "var(--accent-green)";
    notice.innerHTML = `<span>▶️</span><span><strong>Instant Streaming Enabled:</strong> Media will play inline in the Telegram chat player without downloading the file.</span>`;
  }
}

// Queue Upload Action (Single or Multiple / Folder)
async function queueUpload() {
  const targetChat = document.getElementById("target-chat-input").value.trim();
  const caption = document.getElementById("caption-input").value.trim();
  const mediaType = document.getElementById("media-type-select").value;
  const forceDocument = document.getElementById("force-doc-checkbox").checked;
  const localPath = document.getElementById("local-path-input").value.trim();
  const recursive = document.getElementById("local-recursive-checkbox").checked;
  const skipDuplicatesCheckbox = document.getElementById("skip-duplicates-checkbox");
  const skipDuplicates = skipDuplicatesCheckbox ? skipDuplicatesCheckbox.checked : true;

  if (!targetChat) {
    showToast("Please select or enter a destination chat/group.", "error");
    return;
  }

  let chatTitle = "";
  const selectElem = document.getElementById("dialog-select");
  if (selectElem && selectElem.selectedIndex > 0 && String(selectElem.value) === String(targetChat)) {
    chatTitle = selectElem.options[selectElem.selectedIndex].text;
  }

  const btn = document.getElementById("btn-queue-upload");
  btn.disabled = true;
  btn.innerText = "Queueing...";

  try {
    // Priority 1: Browser files selected or dropped
    if (selectedFiles.length > 0) {
      const formData = new FormData();
      for (const f of selectedFiles) {
        formData.append("files", f);
      }
      formData.append("target_chat", targetChat);
      if (chatTitle) formData.append("chat_title", chatTitle);
      formData.append("caption", caption);
      formData.append("media_type", mediaType);
      formData.append("force_document", forceDocument);
      formData.append("skip_duplicates", skipDuplicates);

      const res = await fetch("/api/upload", {
        method: "POST",
        body: formData
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to upload files");

      const count = data.queued_count || 0;
      let toastMsg = `Queued ${count} file${count > 1 ? 's' : ''}! Starting upload...`;
      if (data.skipped_duplicates_count > 0) {
        toastMsg = `Queued ${count} new file${count > 1 ? 's' : ''} (${data.skipped_duplicates_count} duplicate${data.skipped_duplicates_count > 1 ? 's' : ''} auto-skipped)`;
      }
      showToast(toastMsg, count > 0 ? "success" : "info");
      clearSelectedFiles();
    } else if (localPath) {
      // Priority 2: Local server path or folder
      const res = await fetch("/api/upload-local", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          file_path: localPath,
          target_chat: targetChat,
          chat_title: chatTitle,
          caption: caption,
          media_type: mediaType,
          force_document: forceDocument,
          recursive: recursive,
          skip_duplicates: skipDuplicates
        })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to queue local files");

      const count = data.queued_count || 0;
      let toastMsg = `Queued ${count} file${count > 1 ? 's' : ''} from local path!`;
      if (data.skipped_duplicates_count > 0) {
        toastMsg = `Queued ${count} new file${count > 1 ? 's' : ''} (${data.skipped_duplicates_count} duplicate${data.skipped_duplicates_count > 1 ? 's' : ''} auto-skipped)`;
      }
      showToast(toastMsg, count > 0 ? "success" : "info");
      document.getElementById("local-path-input").value = "";
      document.getElementById("local-scan-preview").style.display = "none";
    } else {
      showToast("Please select files, a folder, or specify a local server path.", "error");
      btn.disabled = false;
      btn.innerText = "🚀 Start / Queue Upload";
      return;
    }

    switchTab("queue");
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.innerText = "🚀 Start / Queue Upload";
  }
}

// Fetch Tasks / Queue List
async function fetchTasks() {
  try {
    const res = await fetch("/api/tasks");
    const data = await res.json();
    currentTasks = data.tasks || [];
    renderTasks();
  } catch (err) {
    console.error("Failed to fetch tasks:", err);
  }
}

function renderTasks() {
  const list = document.getElementById("task-list");
  const empty = document.getElementById("empty-queue");

  if (!currentTasks || currentTasks.length === 0) {
    list.innerHTML = "";
    empty.style.display = "block";
    return;
  }

  empty.style.display = "none";
  list.innerHTML = currentTasks.map(t => createTaskCardHtml(t)).join("");
}

function createTaskCardHtml(t) {
  const percent = t.file_size > 0 ? Math.min(100, Math.round((t.bytes_uploaded / t.file_size) * 100)) : 0;
  const isPulse = t.status === "uploading" ? "pulse" : "";
  const icon = getFileIcon(t.file_name, t.media_type);

  // Status text & badge
  let badgeClass = t.status;
  let statusText = t.status.toUpperCase();
  if (t.status === "retrying") {
    statusText = "AUTO-RETRYING";
  }

  let actionsHtml = "";
  if (t.status === "uploading" || t.status === "queued") {
    actionsHtml = `<button class="btn btn-secondary btn-sm" onclick="pauseTask(${t.id})">⏸ Pause</button>`;
  } else if (t.status === "paused") {
    actionsHtml = `<button class="btn btn-primary btn-sm" onclick="resumeTask(${t.id})">▶ Resume</button>`;
  } else if (t.status === "retrying" || t.status === "failed") {
    actionsHtml = `<button class="btn btn-primary btn-sm" onclick="retryTask(${t.id})">🔁 Retry Now</button>`;
  }
  if (t.status !== "completed" && t.status !== "cancelled") {
    actionsHtml += `<button class="btn btn-danger btn-sm" onclick="deleteTask(${t.id})">✕ Cancel</button>`;
  }

  // Auto-resume notice if retrying
  let noticeHtml = "";
  if (t.status === "retrying" && t.error_message) {
    noticeHtml = `<div class="auto-resume-notice">⚡ Auto-resume: ${escapeHtml(t.error_message)}</div>`;
  }

  const actionsSection = actionsHtml
    ? `<!-- Actions -->
      <div class="task-actions">
        ${actionsHtml}
      </div>`
    : "";

  return `
    <div id="task-card-${t.id}" class="task-item">
      <div class="task-item-header">
        <div class="task-info-main">
          <div class="task-icon">${icon}</div>
          <div class="task-names">
            <h3>${escapeHtml(t.file_name)}</h3>
            <div class="task-meta">
              Target: <strong>${escapeHtml(t.chat_title || t.target_chat)}</strong> &bull;
              Size: ${formatBytes(t.file_size)} &bull;
              Parts: <span id="parts-${t.id}">${t.completed_parts}/${t.total_parts}</span>
            </div>
          </div>
        </div>
        <span id="badge-${t.id}" class="task-badge ${badgeClass}">${statusText}</span>
      </div>

      <!-- Progress bar -->
      <div class="progress-bar-container">
        <div id="bar-${t.id}" class="progress-bar-fill ${isPulse}" style="width: ${percent}%;"></div>
      </div>

      <!-- Metrics -->
      <div class="task-metrics">
        <span id="metric-bytes-${t.id}">${formatBytes(t.bytes_uploaded)} of ${formatBytes(t.file_size)} (${percent}%)</span>
        <span id="metric-speed-${t.id}">${t.status === 'completed' ? 'Delivered' : ''}</span>
      </div>

      ${noticeHtml}

      ${actionsSection}
    </div>
  `;
}

// WebSocket Connection & Real-Time Event Handling
function connectWebSocket() {
  if (wsReconnectTimer) clearTimeout(wsReconnectTimer);

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws/progress`;

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    console.log("WebSocket connected to Telegram Uploader.");
    fetchTasks();
    checkStatus();
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleWsEvent(msg);
    } catch (e) {
      console.error("WS Parse error:", e);
    }
  };

  ws.onclose = () => {
    console.log("WebSocket disconnected. Reconnecting in 3s...");
    wsReconnectTimer = setTimeout(connectWebSocket, 3000);
  };

  ws.onerror = (err) => {
    console.error("WS error:", err);
    ws.close();
  };
}

function handleWsEvent(msg) {
  const type = msg.type;
  const d = msg.data || {};

  if (type === "init") {
    if (msg.stats) updateStatsUI(msg.stats);
  } else if (type === "progress") {
    updateProgressUI(d);
  } else if (type === "task_started" || type === "task_queued" || type === "task_completed" || type === "task_retrying" || type === "task_failed" || type === "task_paused" || type === "task_resumed" || type === "task_cancelled") {
    fetchTasks();
    checkStatus();
    if (type === "task_completed") {
      showToast(`Delivered to Telegram!`, "success");
    } else if (type === "task_retrying") {
      showToast(`Upload interrupted. Auto-resuming in ${d.retry_in || 'a few'}s...`, "warning");
    }
  }
}

function updateProgressUI(d) {
  const taskId = d.task_id;
  const bar = document.getElementById(`bar-${taskId}`);
  const metricBytes = document.getElementById(`metric-bytes-${taskId}`);
  const metricSpeed = document.getElementById(`metric-speed-${taskId}`);
  const partsSpan = document.getElementById(`parts-${taskId}`);

  if (bar) {
    bar.style.width = `${d.percent}%`;
    bar.classList.add("pulse");
  }

  if (metricBytes) {
    metricBytes.innerText = `${formatBytes(d.uploaded_bytes)} of ${formatBytes(d.total_bytes)} (${d.percent}%)`;
  }

  if (metricSpeed) {
    const speedStr = d.speed > 0 ? `${formatBytes(d.speed)}/s` : "Uploading...";
    const etaStr = d.eta > 0 ? ` • ETA: ${formatTime(d.eta)}` : "";
    metricSpeed.innerText = `${speedStr}${etaStr}`;
  }

  if (partsSpan) {
    partsSpan.innerText = `${d.completed_parts}/${d.total_parts}`;
  }
}

// Task Actions
async function pauseTask(taskId) {
  try {
    await fetch(`/api/tasks/${taskId}/pause`, { method: "POST" });
    fetchTasks();
  } catch (e) {
    showToast(e.message, "error");
  }
}

async function resumeTask(taskId) {
  try {
    await fetch(`/api/tasks/${taskId}/resume`, { method: "POST" });
    fetchTasks();
  } catch (e) {
    showToast(e.message, "error");
  }
}

async function retryTask(taskId) {
  try {
    await fetch(`/api/tasks/${taskId}/retry`, { method: "POST" });
    fetchTasks();
  } catch (e) {
    showToast(e.message, "error");
  }
}

async function deleteTask(taskId) {
  if (!confirm("Are you sure you want to cancel and remove this upload task?")) return;
  try {
    await fetch(`/api/tasks/${taskId}`, { method: "DELETE" });
    fetchTasks();
    checkStatus();
  } catch (e) {
    showToast(e.message, "error");
  }
}

async function clearCompleted() {
  try {
    const res = await fetch("/api/tasks/clear-completed", { method: "POST" });
    const data = await res.json();
    showToast(`Cleared ${data.cleared_count} finished tasks.`, "info");
    fetchTasks();
    checkStatus();
    loadCacheStats();
  } catch (e) {
    showToast(e.message, "error");
  }
}

async function loadCacheStats() {
  try {
    const res = await fetch("/api/cache/stats");
    if (!res.ok) return;
    const s = await res.json();

    const upEl = document.getElementById("cache-uploads-size");
    const thEl = document.getElementById("cache-thumbs-size");
    const prEl = document.getElementById("cache-protected-size");
    const totEl = document.getElementById("cache-total-size");
    const queueBtn = document.getElementById("btn-queue-clear-cache");
    const clearBtn = document.getElementById("btn-clear-cache");

    if (upEl) upEl.innerText = `${formatBytes(s.uploads_bytes)} (${s.uploads_count} file${s.uploads_count === 1 ? '' : 's'})`;
    if (thEl) thEl.innerText = `${formatBytes(s.thumbs_bytes)} (${s.thumbs_count} file${s.thumbs_count === 1 ? '' : 's'})`;
    if (prEl) prEl.innerText = `${formatBytes(s.protected_bytes)} (${s.protected_count} active)`;
    if (totEl) totEl.innerText = formatBytes(s.reclaimable_bytes);

    if (queueBtn) {
      queueBtn.innerText = s.reclaimable_bytes > 0
        ? `🧹 Clear Cache (${formatBytes(s.reclaimable_bytes)})`
        : "🧹 Clear Cache";
    }
    if (clearBtn) {
      clearBtn.innerText = s.reclaimable_bytes > 0
        ? `🧹 Clear Upload Cache (${formatBytes(s.reclaimable_bytes)})`
        : "🧹 Upload Cache Clean (0 B)";
    }
    return s;
  } catch (e) {
    console.error("Failed to fetch cache stats:", e);
  }
}

async function clearUploadCache() {
  const stats = await loadCacheStats();
  const reclaimableBytes = stats ? stats.reclaimable_bytes : 0;
  const reclaimableCount = stats ? stats.reclaimable_count : 0;

  if (stats && reclaimableCount === 0) {
    showToast("Upload cache is already clean (0 B reclaimable).", "info");
    return;
  }

  const protectedNote = stats && stats.protected_count > 0
    ? `\n\n(${stats.protected_count} active/queued file(s) remain protected.)`
    : "";

  if (!confirm(
    `Clear ${formatBytes(reclaimableBytes)} across ${reclaimableCount} cached temporary file(s)?\n\n` +
    `Media already sent to Telegram and your original folders will NOT be affected.${protectedNote}`
  )) {
    return;
  }

  const clearBtn = document.getElementById("btn-clear-cache");
  if (clearBtn) clearBtn.disabled = true;

  try {
    const res = await fetch("/api/cache/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ include_thumbs: true })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to clear cache");

    showToast(
      `Freed ${formatBytes(data.freed_bytes)} (${data.deleted_files_count} cached files deleted)!`,
      "success"
    );
    await loadCacheStats();
  } catch (e) {
    showToast(`Failed to clear cache: ${e.message}`, "error");
  } finally {
    if (clearBtn) clearBtn.disabled = false;
  }
}

// Settings & Auth Functions
function setAuthMode(mode) {
  const botBtn = document.getElementById("auth-mode-bot-btn");
  const userBtn = document.getElementById("auth-mode-user-btn");
  const botForm = document.getElementById("auth-bot-form");
  const userForm = document.getElementById("auth-user-form");

  if (mode === "bot") {
    botBtn.className = "btn btn-sm btn-primary";
    userBtn.className = "btn btn-sm btn-secondary";
    botForm.style.display = "block";
    userForm.style.display = "none";
  } else {
    userBtn.className = "btn btn-sm btn-primary";
    botBtn.className = "btn btn-sm btn-secondary";
    userForm.style.display = "block";
    botForm.style.display = "none";
  }
}

async function loginBot() {
  const apiId = document.getElementById("api-id-input").value.trim();
  const apiHash = document.getElementById("api-hash-input").value.trim();
  const botToken = document.getElementById("bot-token-input").value.trim();

  if (!botToken) {
    showToast("Please provide your Bot Token from @BotFather.", "error");
    return;
  }

  showToast("Connecting to Telegram...", "info");
  try {
    const res = await fetch("/api/auth/bot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        bot_token: botToken,
        api_id: apiId ? parseInt(apiId) : null,
        api_hash: apiHash || null
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to login as Bot");

    showToast("Connected as Bot successfully!", "success");
    checkStatus();
    fetchDialogs();
  } catch (e) {
    showToast(e.message, "error");
  }
}

async function sendPhoneCode() {
  const apiId = document.getElementById("api-id-input").value.trim();
  const apiHash = document.getElementById("api-hash-input").value.trim();
  const phone = document.getElementById("phone-input").value.trim();

  if (!apiId || !apiHash || !phone) {
    showToast("Please provide API ID, API Hash, and Phone Number.", "error");
    return;
  }

  showToast(`Requesting code for ${phone}...`, "info");
  try {
    const res = await fetch("/api/auth/phone/send-code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_id: parseInt(apiId),
        api_hash: apiHash,
        phone: phone
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to send code");

    document.getElementById("phone-verify-section").style.display = "block";
    showToast(data.message, "success");
  } catch (e) {
    showToast(e.message, "error");
  }
}

async function verifyPhoneCode() {
  const code = document.getElementById("phone-code-input").value.trim();
  const password = document.getElementById("phone-password-input").value.trim() || null;

  if (!code) {
    showToast("Please enter the verification code.", "error");
    return;
  }

  showToast("Verifying code...", "info");
  try {
    const res = await fetch("/api/auth/phone/verify-code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code, password: password })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Verification failed");

    if (data.requires_2fa) {
      showToast(data.message, "warning");
      document.getElementById("phone-password-input").focus();
      return;
    }

    showToast("Logged in successfully!", "success");
    checkStatus();
    fetchDialogs();
  } catch (e) {
    showToast(e.message, "error");
  }
}

async function logout() {
  if (!confirm("Are you sure you want to disconnect?")) return;
  try {
    await fetch("/api/auth/logout", { method: "POST" });
    showToast("Disconnected from Telegram.", "info");
    checkStatus();
    fetchDialogs();
  } catch (e) {
    showToast(e.message, "error");
  }
}

async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    const data = await res.json();
    const s = data.settings || {};

    if (s.api_id) document.getElementById("api-id-input").value = s.api_id;
    if (s.api_hash) document.getElementById("api-hash-input").value = s.api_hash;
    if (s.bot_token) document.getElementById("bot-token-input").value = s.bot_token;
    if (s.phone) document.getElementById("phone-input").value = s.phone;
    if (s.chunk_size_kb) document.getElementById("setting-chunk-size").value = s.chunk_size_kb;
    if (s.max_concurrent_chunks) document.getElementById("setting-concurrent").value = s.max_concurrent_chunks;
    if (s.max_retries) document.getElementById("setting-max-retries").value = s.max_retries;
    if (s.retry_base_delay) document.getElementById("setting-base-delay").value = s.retry_base_delay;
    if (s.auto_delete_local_on_finish !== undefined) {
      document.getElementById("setting-auto-delete").checked = s.auto_delete_local_on_finish;
    }
  } catch (e) {
    console.error("Failed to load settings:", e);
  }
}

async function saveSettings() {
  const updates = {
    chunk_size_kb: parseInt(document.getElementById("setting-chunk-size").value),
    max_concurrent_chunks: parseInt(document.getElementById("setting-concurrent").value),
    max_retries: parseInt(document.getElementById("setting-max-retries").value),
    retry_base_delay: parseFloat(document.getElementById("setting-base-delay").value),
    auto_delete_local_on_finish: document.getElementById("setting-auto-delete").checked
  };

  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates)
    });
    if (!res.ok) throw new Error("Failed to save settings");
    showToast("Engine settings saved!", "success");
  } catch (e) {
    showToast(e.message, "error");
  }
}

// Utility Helpers
function formatBytes(bytes, decimals = 1) {
  if (!bytes || bytes === 0) return "0 B";
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + " " + sizes[i];
}

function formatTime(seconds) {
  if (!seconds || seconds <= 0) return "0s";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const remS = s % 60;
  return `${m}m ${remS}s`;
}

function getFileIcon(filename, mediaType) {
  if (mediaType === "video") return "🎬";
  if (mediaType === "photo") return "🖼";
  if (mediaType === "audio") return "🎵";

  const ext = (filename.split(".").pop() || "").toLowerCase();
  if (["mp4", "mkv", "mov", "avi", "webm"].includes(ext)) return "🎬";
  if (["jpg", "jpeg", "png", "gif", "webp", "bmp"].includes(ext)) return "🖼";
  if (["mp3", "flac", "wav", "m4a", "ogg"].includes(ext)) return "🎵";
  if (["zip", "rar", "7z", "tar", "gz"].includes(ext)) return "📦";
  if (["pdf", "doc", "docx", "txt"].includes(ext)) return "📄";
  return "📁";
}

function escapeHtml(text) {
  if (!text) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
