"use strict";

const MAX_FILE_BYTES = 20 * 1024 * 1024;
const POLL_INTERVAL_MS = 1000;
const FINAL_STATUSES = new Set(["completed", "partial", "failed", "interrupted"]);
const STATUS_DETAILS = {
  queued: { label: "排队中", percent: 5, message: "任务已提交，等待 GPU", tone: "is-running" },
  stage1: { label: "目标分割", percent: 25, message: "正在识别并分离目标物体", tone: "is-running" },
  stage2: { label: "场景重建", percent: 55, message: "正在重建相机、深度与背景", tone: "is-running" },
  stage3: { label: "物体生成", percent: 82, message: "正在生成 3D 几何与关键帧", tone: "is-running" },
  completed: { label: "生成完成", percent: 100, message: "所有产物已生成，可以下载", tone: "is-success" },
  partial: { label: "部分完成", percent: 100, message: "已保留成功生成的部分产物", tone: "is-error" },
  failed: { label: "生成失败", percent: 100, message: "生成未完成，请查看错误信息", tone: "is-error" },
  interrupted: { label: "任务中断", percent: 100, message: "服务重启或任务被中断", tone: "is-error" },
};

const PREVIEW_SLOTS = [
  { key: "input", label: "输入照片", terms: ["input", "source", "original", "upload"] },
  { key: "mask", label: "目标遮罩", terms: ["mask", "segmentation"] },
  { key: "background", label: "背景图", terms: ["background", "inpaint", "bg.", "bg_"] },
  { key: "depth", label: "深度图", terms: ["depth"] },
];

const elements = {
  notice: document.querySelector("#notice"),
  createSection: document.querySelector("#create-section"),
  cameraFrame: document.querySelector("#camera-frame"),
  cameraVideo: document.querySelector("#camera-video"),
  photoPreview: document.querySelector("#photo-preview"),
  captureCanvas: document.querySelector("#capture-canvas"),
  startCamera: document.querySelector("#start-camera"),
  capturePhoto: document.querySelector("#capture-photo"),
  retakePhoto: document.querySelector("#retake-photo"),
  filePickerLabel: document.querySelector("#file-picker-label"),
  filePicker: document.querySelector("#file-picker"),
  cameraStatus: document.querySelector("#camera-status"),
  form: document.querySelector("#job-form"),
  prompt: document.querySelector("#object-prompt"),
  promptError: document.querySelector("#prompt-error"),
  submitJob: document.querySelector("#submit-job"),
  jobSection: document.querySelector("#job-section"),
  jobTitle: document.querySelector("#job-title"),
  statusPill: document.querySelector("#status-pill"),
  progressPercent: document.querySelector("#progress-percent"),
  progressMessage: document.querySelector("#progress-message"),
  progressTrack: document.querySelector(".progress-track"),
  progressBar: document.querySelector("#progress-bar"),
  queuePosition: document.querySelector("#queue-position"),
  stageItems: Array.from(document.querySelectorAll(".stage-list li")),
  jobId: document.querySelector("#job-id"),
  errorCard: document.querySelector("#error-card"),
  errorSummary: document.querySelector("#error-summary"),
  previewSection: document.querySelector("#preview-section"),
  previewGrid: document.querySelector("#preview-grid"),
  artifactSection: document.querySelector("#artifact-section"),
  artifactList: document.querySelector("#artifact-list"),
  downloadAll: document.querySelector("#download-all"),
  newJob: document.querySelector("#new-job"),
  retryStatus: document.querySelector("#retry-status"),
};

let cameraStream = null;
let selectedImage = null;
let previewObjectUrl = null;
let currentJobId = null;
let pollTimer = null;
let polling = false;

function showNotice(message, isError = false) {
  elements.notice.textContent = message;
  elements.notice.classList.toggle("is-error", isError);
  elements.notice.hidden = !message;
}

function stopCamera() {
  if (cameraStream) {
    cameraStream.getTracks().forEach((track) => track.stop());
    cameraStream = null;
  }
  elements.cameraVideo.srcObject = null;
}

function cameraIsAvailable() {
  return Boolean(window.isSecureContext && navigator.mediaDevices?.getUserMedia);
}

async function startCamera() {
  stopCamera();
  elements.startCamera.disabled = true;
  elements.cameraStatus.textContent = "正在请求摄像头权限…";
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 1920 },
        height: { ideal: 1440 },
      },
      audio: false,
    });
    elements.cameraVideo.srcObject = cameraStream;
    await elements.cameraVideo.play();
    elements.cameraFrame.className = "camera-frame is-live";
    elements.startCamera.hidden = true;
    elements.capturePhoto.hidden = false;
    elements.retakePhoto.hidden = true;
    elements.cameraStatus.textContent = "后置摄像头已开启。让桌面完整入镜，再点击拍摄。";
  } catch (error) {
    stopCamera();
    elements.startCamera.hidden = true;
    elements.capturePhoto.hidden = true;
    elements.filePickerLabel.classList.remove("button-secondary");
    elements.filePickerLabel.classList.add("button-primary");
    const denied = error?.name === "NotAllowedError" || error?.name === "SecurityError";
    elements.cameraStatus.textContent = denied
      ? "摄像头权限未开启，请点击“系统相机 / 相册”继续。"
      : "无法启动实时摄像头，请点击“系统相机 / 相册”继续。";
  } finally {
    elements.startCamera.disabled = false;
  }
}

function setSelectedImage(blob, filename) {
  if (previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
  selectedImage = new File([blob], filename || "capture.jpg", {
    type: blob.type || "image/jpeg",
    lastModified: Date.now(),
  });
  previewObjectUrl = URL.createObjectURL(selectedImage);
  elements.photoPreview.src = previewObjectUrl;
  elements.cameraFrame.className = "camera-frame has-photo";
  elements.capturePhoto.hidden = true;
  elements.startCamera.hidden = true;
  elements.retakePhoto.hidden = false;
  elements.cameraStatus.textContent = `${selectedImage.name} · ${formatBytes(selectedImage.size)}`;
  stopCamera();
  updateSubmitState();
}

function capturePhoto() {
  const video = elements.cameraVideo;
  if (!video.videoWidth || !video.videoHeight) {
    elements.cameraStatus.textContent = "摄像头画面尚未准备好，请稍后重试。";
    return;
  }
  const canvas = elements.captureCanvas;
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d", { alpha: false }).drawImage(video, 0, 0, canvas.width, canvas.height);
  canvas.toBlob(
    (blob) => {
      if (!blob) {
        elements.cameraStatus.textContent = "照片处理失败，请重新拍摄。";
        return;
      }
      setSelectedImage(blob, `capture-${new Date().toISOString().replaceAll(":", "-")}.jpg`);
    },
    "image/jpeg",
    0.92,
  );
}

function clearSelectedImage() {
  selectedImage = null;
  if (previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
  previewObjectUrl = null;
  elements.photoPreview.removeAttribute("src");
  elements.filePicker.value = "";
  elements.cameraFrame.className = "camera-frame is-empty";
  elements.retakePhoto.hidden = true;
  elements.startCamera.hidden = !cameraIsAvailable();
  elements.capturePhoto.hidden = true;
  elements.cameraStatus.textContent = "";
  updateSubmitState();
}

async function handleFileSelection(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    elements.cameraStatus.textContent = "请选择浏览器可读取的 JPEG、PNG 等图片文件。";
    event.target.value = "";
    return;
  }
  if (file.size > MAX_FILE_BYTES) {
    elements.cameraStatus.textContent = "图片超过 20MB，请降低相机分辨率后重试。";
    event.target.value = "";
    return;
  }
  try {
    await verifyImageCanLoad(file);
    setSelectedImage(file, file.name || "phone-photo.jpg");
  } catch {
    elements.cameraStatus.textContent = "无法读取这张图片，请换一张重试。";
    event.target.value = "";
  }
}

function verifyImageCanLoad(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve();
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("invalid image"));
    };
    image.src = url;
  });
}

function validatePrompt(showError = false) {
  const prompt = elements.prompt.value.trim();
  const englishOnly = /^[A-Za-z][A-Za-z0-9 _'-]*$/.test(prompt);
  let error = "";
  if (!prompt) error = "请输入英文物体名称。";
  else if (!englishOnly) error = "请以英文字母开头，只使用英文、数字、空格、下划线、连字符或英文撇号。";
  if (showError || !error) elements.promptError.textContent = error;
  return !error;
}

function updateSubmitState() {
  elements.submitJob.disabled = !(selectedImage && validatePrompt(false));
}

async function submitJob(event) {
  event.preventDefault();
  if (!selectedImage) {
    elements.cameraStatus.textContent = "请先拍摄或选择一张照片。";
    return;
  }
  if (!validatePrompt(true)) return;

  elements.submitJob.disabled = true;
  elements.submitJob.querySelector("span").textContent = "正在上传照片…";
  showNotice("");
  const formData = new FormData();
  formData.append("image", selectedImage, selectedImage.name);
  formData.append("prompt", elements.prompt.value.trim());

  try {
    const response = await fetch("/api/jobs", { method: "POST", body: formData });
    const payload = await readResponse(response);
    if (!response.ok) throw new Error(apiErrorMessage(payload, `提交失败（HTTP ${response.status}）`));
    const jobId = payload.id || payload.job_id || payload.job?.id;
    if (!jobId) throw new Error("服务器未返回任务 ID。请检查 Web 服务日志。");
    openJob(jobId, payload);
  } catch (error) {
    showNotice(error.message || "任务提交失败，请检查网络后重试。", true);
    elements.submitJob.disabled = false;
  } finally {
    elements.submitJob.querySelector("span").textContent = "开始生成 3D";
  }
}

function openJob(jobId, initialPayload = null) {
  stopCamera();
  currentJobId = String(jobId);
  elements.createSection.hidden = true;
  elements.jobSection.hidden = false;
  elements.jobId.textContent = currentJobId;
  elements.downloadAll.href = `/api/jobs/${encodeURIComponent(currentJobId)}/download`;
  elements.retryStatus.hidden = true;
  const url = new URL(window.location.href);
  url.searchParams.set("job", currentJobId);
  window.history.replaceState({ job: currentJobId }, "", url);
  if (initialPayload) renderJob(initialPayload);
  pollJob(true);
  elements.jobSection.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function pollJob(immediate = false) {
  window.clearTimeout(pollTimer);
  if (!currentJobId || polling) return;
  polling = true;
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(currentJobId)}`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    const payload = await readResponse(response);
    if (!response.ok) throw new Error(apiErrorMessage(payload, `无法获取任务（HTTP ${response.status}）`));
    renderJob(payload);
    elements.retryStatus.hidden = true;
    if (!FINAL_STATUSES.has(normalizeStatus(payload.status || payload.stage))) {
      pollTimer = window.setTimeout(pollJob, immediate ? 250 : POLL_INTERVAL_MS);
    }
  } catch (error) {
    showNotice(`状态更新失败：${error.message}`, true);
    elements.retryStatus.hidden = false;
    pollTimer = window.setTimeout(pollJob, 5000);
  } finally {
    polling = false;
  }
}

function renderJob(payload) {
  const rawStatus = payload.status || payload.stage || "queued";
  const status = normalizeStatus(rawStatus);
  const details = STATUS_DETAILS[status] || STATUS_DETAILS.queued;
  const apiProgress = Number(payload.progress ?? payload.progress_percent);
  const percent = Number.isFinite(apiProgress)
    ? Math.max(0, Math.min(100, apiProgress <= 1 ? apiProgress * 100 : apiProgress))
    : details.percent;
  const message = payload.message || payload.status_message || details.message;

  elements.statusPill.textContent = details.label;
  elements.statusPill.className = `status-pill ${details.tone}`;
  elements.statusPill.dataset.status = status;
  elements.progressPercent.textContent = `${Math.round(percent)}%`;
  elements.progressMessage.textContent = message;
  elements.progressBar.style.width = `${percent}%`;
  elements.progressTrack.setAttribute("aria-valuenow", String(Math.round(percent)));
  const queuePosition = payload.queue_position ?? payload.position;
  elements.queuePosition.textContent = status === "queued" && Number.isFinite(Number(queuePosition))
    ? `队列第 ${Math.max(1, Number(queuePosition))} 位${Number(queuePosition) === 1 ? " · 下一个执行" : ""}`
    : "";
  elements.jobTitle.textContent = status === "completed"
    ? "你的 3D 物体已生成"
    : status === "partial"
      ? "任务已结束，部分产物可用"
      : ["failed", "interrupted"].includes(status)
        ? "这次生成没有完成"
        : "正在生成你的 3D 物体";
  updateStages(status);

  const error = extractError(payload);
  elements.errorCard.hidden = !error;
  elements.errorSummary.textContent = error;

  const artifacts = normalizeArtifacts(payload.artifacts || payload.outputs || payload.files, currentJobId);
  renderPreviews(artifacts);
  renderArtifacts(artifacts);
}

function normalizeStatus(value) {
  const status = String(value || "").toLowerCase().replaceAll("-", "").replaceAll("_", "");
  if (["stage1", "promptinpaint", "segmenting", "segmentation"].includes(status)) return "stage1";
  if (["stage2", "anysplat", "reconstructing", "reconstruction"].includes(status)) return "stage2";
  if (["stage3", "sam3d", "generating", "generation"].includes(status)) return "stage3";
  if (["complete", "completed", "success", "succeeded"].includes(status)) return "completed";
  if (["partial", "partiallycompleted"].includes(status)) return "partial";
  if (["failed", "error"].includes(status)) return "failed";
  if (["interrupted", "cancelled", "canceled"].includes(status)) return "interrupted";
  return "queued";
}

function updateStages(status) {
  const stageIndex = { queued: -1, stage1: 0, stage2: 1, stage3: 2, completed: 3, partial: 3 }[status] ?? -1;
  elements.stageItems.forEach((item, index) => {
    item.classList.toggle("is-complete", index < stageIndex || status === "completed");
    item.classList.toggle("is-active", index === stageIndex);
    const marker = item.querySelector("i");
    marker.textContent = index < stageIndex || status === "completed" ? "✓" : String(index + 1);
  });
}

function normalizeArtifacts(value, jobId) {
  if (!value) return [];
  let entries;
  if (Array.isArray(value)) {
    entries = value.map((artifact, index) => [artifact?.id || artifact?.artifact_id || String(index), artifact]);
  } else if (typeof value === "object") {
    entries = Object.entries(value);
  } else {
    return [];
  }
  return entries
    .map(([key, raw]) => {
      const artifact = typeof raw === "string" ? { path: raw } : raw || {};
      const id = String(artifact.id || artifact.artifact_id || key);
      const path = String(artifact.path || artifact.filename || artifact.name || id);
      const name = String(artifact.name || artifact.filename || path.split("/").pop() || id);
      return {
        id,
        name,
        path,
        kind: String(artifact.kind || artifact.type || artifact.category || "").toLowerCase(),
        size: Number(artifact.size || artifact.size_bytes || 0),
        url: artifact.url || artifact.download_url || `/api/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(id)}`,
      };
    })
    .filter((artifact) => artifact.id && artifact.url);
}

function renderPreviews(artifacts) {
  elements.previewGrid.replaceChildren();
  const usedIds = new Set();
  const previews = PREVIEW_SLOTS.map((slot) => {
    const candidates = artifacts.filter((candidate) => {
      if (usedIds.has(candidate.id)) return false;
      const haystack = `${candidate.id} ${candidate.name} ${candidate.path} ${candidate.kind}`.toLowerCase();
      return slot.terms.some((term) => haystack.includes(term));
    });
    const artifact = candidates.find((candidate) => /\.(avif|jpe?g|png|webp)$/i.test(candidate.name)) || candidates[0];
    if (artifact) usedIds.add(artifact.id);
    return { slot, artifact };
  }).filter(({ artifact }) => artifact);

  previews.forEach(({ slot, artifact }) => {
    const link = document.createElement("a");
    link.className = "preview-item";
    link.href = artifact.url;
    link.target = "_blank";
    link.rel = "noopener";
    const imageBox = document.createElement("div");
    imageBox.className = "preview-image";
    const image = document.createElement("img");
    image.src = artifact.url;
    image.alt = slot.label;
    image.loading = "lazy";
    image.onerror = () => {
      image.remove();
      imageBox.textContent = fileExtension(artifact.name);
    };
    imageBox.append(image);
    const label = document.createElement("p");
    label.textContent = slot.label;
    link.append(imageBox, label);
    elements.previewGrid.append(link);
  });
  elements.previewSection.hidden = previews.length === 0;
}

function renderArtifacts(artifacts) {
  elements.artifactList.replaceChildren();
  artifacts.forEach((artifact) => {
    const row = document.createElement("div");
    row.className = "artifact-row";
    const info = document.createElement("div");
    info.className = "artifact-info";
    const type = document.createElement("span");
    type.className = "file-type";
    type.textContent = fileExtension(artifact.name);
    const text = document.createElement("div");
    const name = document.createElement("p");
    name.textContent = artifact.name;
    name.title = artifact.path;
    const size = document.createElement("small");
    size.textContent = artifact.size ? formatBytes(artifact.size) : artifact.kind || "生成产物";
    text.append(name, size);
    info.append(type, text);
    const download = document.createElement("a");
    download.className = "artifact-download";
    download.href = artifact.url;
    download.download = artifact.name;
    download.textContent = "下载";
    row.append(info, download);
    elements.artifactList.append(row);
  });
  elements.artifactSection.hidden = artifacts.length === 0;
}

function extractError(payload) {
  const status = normalizeStatus(payload.status || payload.stage);
  const parts = [];
  if (payload.error_code) parts.push(`错误代码：${payload.error_code}`);
  const value = payload.error_summary || payload.error_detail || payload.error || payload.error_message || payload.detail;
  if (value) {
    if (typeof value === "string") parts.push(value);
    else if (typeof value === "object") parts.push(String(value.message || value.detail || JSON.stringify(value)));
    else parts.push(String(value));
  }
  if (["failed", "partial", "interrupted"].includes(status) && payload.log_tail) {
    const logTail = Array.isArray(payload.log_tail) ? payload.log_tail.join("\n") : String(payload.log_tail);
    if (logTail.trim()) parts.push(`最近日志：\n${logTail}`);
  }
  return parts.join("\n\n").slice(0, 2400);
}

function apiErrorMessage(payload, fallback) {
  if (!payload) return fallback;
  const detail = payload.detail || payload.error || payload.message;
  if (Array.isArray(detail)) return detail.map((item) => item.msg || String(item)).join("；");
  if (typeof detail === "object" && detail) return detail.message || JSON.stringify(detail);
  return detail ? String(detail) : fallback;
}

async function readResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return response.json();
  const text = await response.text();
  return text ? { detail: text.slice(0, 1000) } : {};
}

function fileExtension(name) {
  const extension = String(name).split(".").pop();
  return extension && extension.length <= 6 ? extension.toUpperCase() : "FILE";
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function resetForNewJob() {
  window.clearTimeout(pollTimer);
  currentJobId = null;
  elements.jobSection.hidden = true;
  elements.createSection.hidden = false;
  elements.errorCard.hidden = true;
  elements.previewSection.hidden = true;
  elements.artifactSection.hidden = true;
  elements.prompt.value = "";
  elements.promptError.textContent = "";
  clearSelectedImage();
  showNotice("");
  const url = new URL(window.location.href);
  url.searchParams.delete("job");
  window.history.replaceState({}, "", url);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function initializeCameraChoice() {
  if (cameraIsAvailable()) {
    elements.startCamera.hidden = false;
    elements.cameraStatus.textContent = "HTTPS 已就绪，可使用实时后置摄像头。";
  } else {
    elements.startCamera.hidden = true;
    elements.filePickerLabel.classList.remove("button-secondary");
    elements.filePickerLabel.classList.add("button-primary");
    elements.cameraStatus.textContent = window.isSecureContext
      ? "当前浏览器不支持实时摄像头，请使用系统相机 / 相册。"
      : "局域网 HTTP 无法开启实时取景，请使用系统相机拍摄；Tailscale HTTPS 可开启实时取景。";
  }
}

elements.startCamera.addEventListener("click", startCamera);
elements.capturePhoto.addEventListener("click", capturePhoto);
elements.retakePhoto.addEventListener("click", () => {
  clearSelectedImage();
  if (cameraIsAvailable()) startCamera();
  else elements.filePicker.click();
});
elements.filePicker.addEventListener("change", handleFileSelection);
elements.prompt.addEventListener("input", () => {
  if (elements.promptError.textContent) validatePrompt(true);
  updateSubmitState();
});
elements.prompt.addEventListener("blur", () => validatePrompt(true));
elements.form.addEventListener("submit", submitJob);
elements.newJob.addEventListener("click", resetForNewJob);
elements.retryStatus.addEventListener("click", () => pollJob(true));
window.addEventListener("pagehide", stopCamera);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && currentJobId && !FINAL_STATUSES.has(elements.statusPill.dataset.status)) pollJob(true);
});

initializeCameraChoice();
const restoredJobId = new URLSearchParams(window.location.search).get("job");
if (restoredJobId) openJob(restoredJobId);
