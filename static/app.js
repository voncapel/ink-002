const form = document.querySelector("#print-form");
const textarea = document.querySelector("#text");
const count = document.querySelector("#char-count");
const fileInput = document.querySelector("#file");
const fileLabel = document.querySelector("#file-label");
const dropzone = document.querySelector(".dropzone");
const imageEditor = document.querySelector("#image-editor");
const previewCanvas = document.querySelector("#image-preview");
const previewSize = document.querySelector("#preview-size");
const controls = document.querySelector(".controls");
const errorBox = document.querySelector("#form-error");
const printState = document.querySelector("#print-state");
const submitButton = form.querySelector("button[type=submit]");
const connection = document.querySelector("#connection");
let mode = "text";
let sourceFrame = null;
let previewRequest = null;
let currentJobId = null;
let pollTimer = null;

textarea.addEventListener("input", () => { count.textContent = textarea.value.length; });

document.querySelectorAll(".main-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const view = tab.dataset.view;
    document.querySelectorAll(".main-tab").forEach((item) => {
      const selected = item === tab;
      item.classList.toggle("active", selected);
      item.setAttribute("aria-selected", String(selected));
    });
    document.querySelectorAll(".view").forEach((item) => {
      const selected = item.id === `${view}-view`;
      item.hidden = !selected;
      item.classList.toggle("active", selected);
    });
  });
});

document.querySelectorAll(".source-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    mode = tab.dataset.mode;
    document.querySelectorAll(".source-tab").forEach((item) => {
      const selected = item === tab;
      item.classList.toggle("active", selected);
      item.setAttribute("aria-selected", String(selected));
    });
    document.querySelector("#text-panel").classList.toggle("hidden", mode !== "text");
    document.querySelector("#file-panel").classList.toggle("hidden", mode !== "file");
    document.querySelectorAll(".text-option").forEach((item) => item.classList.toggle("hidden", mode !== "text"));
    controls.classList.toggle("file-controls", mode === "file");
  });
});

document.querySelectorAll('input[name="dither"]').forEach((input) => {
  input.addEventListener("change", () => {
    document.querySelectorAll(".dither-card").forEach((card) => {
      card.classList.toggle("selected", card.contains(input));
    });
    schedulePreview();
  });
});

document.querySelectorAll('.range-control input[type="range"]').forEach((input) => {
  input.addEventListener("input", () => {
    document.querySelector(`output[for="${input.id}"]`).value = input.value;
    schedulePreview();
  });
});

function grayscaleFrame(image) {
  const contentWidth = 518;
  const maxPreviewHeight = 900;
  const scale = Math.min(1, contentWidth / image.naturalWidth, maxPreviewHeight / image.naturalHeight);
  const width = Math.max(1, Math.round(image.naturalWidth * scale));
  const height = Math.max(1, Math.round(image.naturalHeight * scale));
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.fillStyle = "#fff";
  context.fillRect(0, 0, width, height);
  context.drawImage(image, 0, 0, width, height);
  const frame = context.getImageData(0, 0, width, height);
  const values = new Float32Array(width * height);
  let low = 255;
  let high = 0;
  for (let index = 0; index < values.length; index += 1) {
    const offset = index * 4;
    const alpha = frame.data[offset + 3] / 255;
    const red = frame.data[offset] * alpha + 255 * (1 - alpha);
    const green = frame.data[offset + 1] * alpha + 255 * (1 - alpha);
    const blue = frame.data[offset + 2] * alpha + 255 * (1 - alpha);
    const gray = red * .299 + green * .587 + blue * .114;
    values[index] = gray;
    low = Math.min(low, gray);
    high = Math.max(high, gray);
  }
  const span = Math.max(1, high - low);
  for (let index = 0; index < values.length; index += 1) {
    values[index] = (values[index] - low) * 255 / span;
  }
  return { values, width, height };
}

function adjustedPixels(frame) {
  const brightness = Number(document.querySelector("#brightness").value) / 100;
  const contrast = Number(document.querySelector("#contrast").value) / 100;
  const sharpness = Number(document.querySelector("#sharpness").value) / 100;
  const values = Float32Array.from(frame.values, (value) => Math.max(0, Math.min(255, value * brightness)));
  let mean = 0;
  values.forEach((value) => { mean += value; });
  mean /= values.length;
  for (let index = 0; index < values.length; index += 1) {
    values[index] = Math.max(0, Math.min(255, mean + (values[index] - mean) * contrast));
  }
  if (sharpness === 1) return values;
  const original = new Float32Array(values);
  const { width, height } = frame;
  for (let y = 1; y < height - 1; y += 1) {
    for (let x = 1; x < width - 1; x += 1) {
      const index = y * width + x;
      let weighted = original[index] * 5;
      for (let dy = -1; dy <= 1; dy += 1) {
        for (let dx = -1; dx <= 1; dx += 1) {
          if (dx !== 0 || dy !== 0) weighted += original[(y + dy) * width + x + dx];
        }
      }
      const smooth = weighted / 13;
      values[index] = Math.max(0, Math.min(255, smooth + (original[index] - smooth) * sharpness));
    }
  }
  return values;
}

function diffuse(source, width, height, kernel) {
  const values = new Float32Array(source);
  const output = new Uint8ClampedArray(values.length);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = y * width + x;
      const oldValue = values[index];
      const newValue = oldValue >= 128 ? 255 : 0;
      output[index] = newValue;
      const error = oldValue - newValue;
      kernel.forEach(([dx, dy, weight]) => {
        const nx = x + dx;
        const ny = y + dy;
        if (nx >= 0 && nx < width && ny < height) {
          const target = ny * width + nx;
          values[target] = Math.max(0, Math.min(255, values[target] + error * weight));
        }
      });
    }
  }
  return output;
}

const bayer4 = [
  [0, 8, 2, 10],
  [12, 4, 14, 6],
  [3, 11, 1, 9],
  [15, 7, 13, 5],
];
const bayer8 = Array.from({ length: 8 }, (_, y) => (
  Array.from({ length: 8 }, (_, x) => {
    const quadrant = [[0, 2], [3, 1]];
    return 4 * bayer4[y % 4][x % 4] + quadrant[Math.floor(y / 4)][Math.floor(x / 4)];
  })
));

function ordered(source, width, height, matrix) {
  const size = matrix.length;
  const levels = size * size;
  const output = new Uint8ClampedArray(source.length);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = y * width + x;
      const threshold = (matrix[y % size][x % size] + .5) * 255 / levels;
      output[index] = source[index] >= threshold ? 255 : 0;
    }
  }
  return output;
}

function ditherPixels(values, width, height, preset) {
  if (preset === "threshold") return Uint8ClampedArray.from(values, (value) => value >= 140 ? 255 : 0);
  if (preset === "floyd") {
    return diffuse(values, width, height, [[1, 0, 7 / 16], [-1, 1, 3 / 16], [0, 1, 5 / 16], [1, 1, 1 / 16]]);
  }
  if (preset === "atkinson") {
    return diffuse(values, width, height, [[1, 0, 1 / 8], [2, 0, 1 / 8], [-1, 1, 1 / 8], [0, 1, 1 / 8], [1, 1, 1 / 8], [0, 2, 1 / 8]]);
  }
  return ordered(values, width, height, preset === "bayer4" ? bayer4 : bayer8);
}

function paintPreview(frame, pixels) {
  previewCanvas.width = 554;
  previewCanvas.height = frame.height + 36;
  const context = previewCanvas.getContext("2d");
  context.fillStyle = "#fff";
  context.fillRect(0, 0, previewCanvas.width, previewCanvas.height);
  const imageData = context.createImageData(frame.width, frame.height);
  pixels.forEach((value, index) => {
    const offset = index * 4;
    imageData.data[offset] = value;
    imageData.data[offset + 1] = value;
    imageData.data[offset + 2] = value;
    imageData.data[offset + 3] = 255;
  });
  context.putImageData(imageData, Math.floor((554 - frame.width) / 2), 18);
  previewSize.textContent = `554 × ${previewCanvas.height} DOTS`;
}

function renderPreview() {
  previewRequest = null;
  if (!sourceFrame) return;
  const preset = document.querySelector('input[name="dither"]:checked').value;
  const values = adjustedPixels(sourceFrame);
  paintPreview(sourceFrame, ditherPixels(values, sourceFrame.width, sourceFrame.height, preset));
}

function schedulePreview() {
  if (!sourceFrame || previewRequest !== null) return;
  previewRequest = requestAnimationFrame(renderPreview);
}

function renderImage(file) {
  const isImage = file && file.type.startsWith("image/");
  imageEditor.classList.toggle("hidden", !isImage);
  sourceFrame = null;
  if (!isImage) return;
  const objectUrl = URL.createObjectURL(file);
  const image = new Image();
  image.onload = () => {
    sourceFrame = grayscaleFrame(image);
    imageEditor.classList.remove("hidden");
    schedulePreview();
    URL.revokeObjectURL(objectUrl);
  };
  image.onerror = () => {
    URL.revokeObjectURL(objectUrl);
    imageEditor.classList.add("hidden");
  };
  image.src = objectUrl;
}

function useFiles(files) {
  const file = files?.[0];
  fileLabel.textContent = file?.name || "Choisir une image";
  renderImage(file);
}

fileInput.addEventListener("change", () => useFiles(fileInput.files));

["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragging");
}));
dropzone.addEventListener("drop", (event) => {
  if (event.dataTransfer.files.length) {
    fileInput.files = event.dataTransfer.files;
    useFiles(event.dataTransfer.files);
  }
});

async function refreshHealth() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    if (!response.ok) throw new Error("offline");
    const data = await response.json();
    const busy = data.active_jobs > 0;
    connection.className = `connection ${busy ? "busy" : "ready"}`;
    connection.querySelector("span:last-child").textContent = busy ? "S002 · impression" : connection.dataset.idleLabel;
  } catch (_error) {
    connection.className = "connection";
    connection.querySelector("span:last-child").textContent = "S002 · hors ligne";
  }
}

async function refreshCurrentJob() {
  if (!currentJobId) return;
  try {
    const response = await fetch(`/api/jobs/${currentJobId}`, { cache: "no-store" });
    if (!response.ok) return;
    const job = await response.json();
    if (job.status === "queued") printState.textContent = "Impression en attente";
    if (job.status === "printing") printState.textContent = "Impression en cours…";
    if (job.status === "done") {
      printState.textContent = "Impression terminée ✓";
      currentJobId = null;
    }
    if (job.status === "failed") {
      printState.textContent = "";
      errorBox.textContent = job.error || "Échec de l’impression";
      currentJobId = null;
    }
  } catch (_error) {
    // The connection badge reports relay availability; keep the transient job state intact.
  }
}

function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    await Promise.all([refreshHealth(), refreshCurrentJob()]);
    schedulePoll();
  }, 1600);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.textContent = "";
  printState.textContent = "";
  const data = new FormData(form);
  if (mode === "text") data.delete("file");
  if (mode === "file") data.delete("text");
  submitButton.disabled = true;
  submitButton.querySelector(".button-label").textContent = "Préparation…";
  try {
    const response = await fetch("/api/jobs", { method: "POST", body: data });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Impossible de lancer l’impression");
    currentJobId = result.id;
    printState.textContent = "Impression en attente";
    submitButton.querySelector(".button-label").textContent = "Dans la file ✓";
    await refreshHealth();
    setTimeout(() => { submitButton.querySelector(".button-label").textContent = "Imprimer"; }, 1200);
  } catch (error) {
    errorBox.textContent = error.message;
    submitButton.querySelector(".button-label").textContent = "Imprimer";
  } finally {
    submitButton.disabled = false;
  }
});

refreshHealth();
schedulePoll();
