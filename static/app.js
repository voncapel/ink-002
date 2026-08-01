const form = document.querySelector("#print-form");
const textarea = document.querySelector("#text");
const count = document.querySelector("#char-count");
const fileInput = document.querySelector("#file");
const fileLabel = document.querySelector("#file-label");
const dropzone = document.querySelector(".dropzone");
const imageEditor = document.querySelector("#image-editor");
const previewCanvas = document.querySelector("#image-preview");
const previewStage = document.querySelector(".preview-stage");
const previewSize = document.querySelector("#preview-size");
const controls = document.querySelector(".controls");
const errorBox = document.querySelector("#form-error");
const printState = document.querySelector("#print-state");
const submitButton = form.querySelector("button[type=submit]");
const connection = document.querySelector("#connection");
const parcelForm = document.querySelector("#parcel-form");
const parcelFile = document.querySelector("#parcel-file");
const parcelFileLabel = document.querySelector("#parcel-file-label");
const parcelDropzone = document.querySelector(".parcel-dropzone");
const parcelError = document.querySelector("#parcel-error");
const parcelState = document.querySelector("#parcel-state");
const parcelManual = document.querySelector("#parcel-manual");
const manualCanvas = document.querySelector("#parcel-manual-canvas");
const manualContext = manualCanvas.getContext("2d");
const manualError = document.querySelector("#manual-error");
const manualCropSize = document.querySelector("#manual-crop-size");
const manualBandReadout = document.querySelector("#manual-band-readout");
const manualLinked = document.querySelector("#manual-linked");
const manualIndependent = document.querySelector("#manual-independent");
const manualAddBand = document.querySelector("#manual-add-cut");
const manualRemoveBand = document.querySelector("#manual-remove-cut");
const manualAuto = document.querySelector("#manual-auto");
const parcelCompose = document.querySelector("#parcel-compose");
const parcelResult = document.querySelector("#parcel-result");
const parcelPreview = document.querySelector("#parcel-preview");
const parcelPrintButton = document.querySelector("#parcel-print");
const parcelPrintError = document.querySelector("#parcel-print-error");
const parcelPrintState = document.querySelector("#parcel-print-state");
let mode = "text";
let sourceFrame = null;
let previewRequest = null;
let currentJobId = null;
let currentJobStateTarget = printState;
let currentJobErrorTarget = errorBox;
let pollTimer = null;
let currentParcelId = null;
let currentParcelDraftId = null;
let parcelDraft = null;
let parcelPageImage = null;
let manualCrop = { x0: .03, y0: .03, x1: .97, y1: .97 };
let manualCuts = [];
let manualMoveMode = "linked";
let activeManualCut = null;
let manualDrag = null;

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
  const printReady = image.naturalWidth === 554;
  const contentWidth = printReady ? 554 : 518;
  // A thermal roll has no page height. Only its physical width may constrain
  // the source; scaling against the preview viewport would shrink long jobs.
  const scale = Math.min(1, contentWidth / image.naturalWidth);
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
  return { values, width, height, margin: printReady ? 0 : 18, printReady };
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
  const margin = frame.margin ?? 18;
  previewCanvas.width = 554;
  previewCanvas.height = frame.height + margin * 2;
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
  context.putImageData(imageData, Math.floor((554 - frame.width) / 2), margin);
  previewStage.classList.toggle("long-roll", previewCanvas.height > 900);
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
  previewStage.classList.remove("long-roll");
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

function normalizedCropPayload() {
  return Object.fromEntries(Object.entries(manualCrop).map(([key, value]) => [key, Math.round(value * 1000)]));
}

function distributeManualBands(bandCount = manualCuts.length + 1) {
  const safeBandCount = Math.max(1, Math.min(40, Math.round(bandCount)));
  manualCuts = Array.from(
    { length: safeBandCount - 1 },
    (_, index) => (index + 1) / safeBandCount,
  );
  activeManualCut = manualCuts.length ? Math.min(activeManualCut ?? 0, manualCuts.length - 1) : null;
}

function equalizeManualCuts() {
  if (!parcelDraft) return;
  const cropHeightMm = (manualCrop.y1 - manualCrop.y0) * parcelDraft.height_mm;
  const bandCount = Math.max(1, Math.ceil(cropHeightMm / 46.9));
  distributeManualBands(bandCount);
}

function updateManualReadout() {
  if (!parcelDraft) return;
  const widthMm = (manualCrop.x1 - manualCrop.x0) * parcelDraft.width_mm;
  const heightMm = (manualCrop.y1 - manualCrop.y0) * parcelDraft.height_mm;
  const boundaries = [0, ...manualCuts, 1];
  const bands = boundaries.slice(0, -1).map((value, index) => (boundaries[index + 1] - value) * heightMm);
  const invalid = bands.some((value) => value > 46.9 + .05);
  manualCropSize.textContent = `${widthMm.toFixed(1)} × ${heightMm.toFixed(1)} MM`;
  manualBandReadout.textContent = `${String(bands.length).padStart(2, "0")} BANDES · ${bands.map((value) => value.toFixed(1)).join(" / ")} MM`;
  manualBandReadout.classList.toggle("invalid", invalid);
  manualAddBand.disabled = bands.length >= 40;
  manualRemoveBand.disabled = bands.length <= 1;
  parcelCompose.disabled = invalid;
  parcelCompose.title = invalid ? "Une bande dépasse 46,9 mm" : "";
}

function drawManualEditor() {
  if (!parcelPageImage) return;
  const width = manualCanvas.width;
  const height = manualCanvas.height;
  manualContext.clearRect(0, 0, width, height);
  manualContext.drawImage(parcelPageImage, 0, 0, width, height);
  const x0 = manualCrop.x0 * width;
  const y0 = manualCrop.y0 * height;
  const x1 = manualCrop.x1 * width;
  const y1 = manualCrop.y1 * height;

  manualContext.fillStyle = "rgba(8, 10, 13, .68)";
  manualContext.fillRect(0, 0, width, y0);
  manualContext.fillRect(0, y1, width, height - y1);
  manualContext.fillRect(0, y0, x0, y1 - y0);
  manualContext.fillRect(x1, y0, width - x1, y1 - y0);
  manualContext.strokeStyle = "#ff7a21";
  manualContext.lineWidth = Math.max(3, width / 420);
  manualContext.strokeRect(x0, y0, x1 - x0, y1 - y0);

  const handleSize = Math.max(12, width / 70);
  manualContext.fillStyle = "#ff7a21";
  [[x0, y0], [x1, y0], [x0, y1], [x1, y1]].forEach(([x, y]) => {
    manualContext.fillRect(x - handleSize / 2, y - handleSize / 2, handleSize, handleSize);
  });

  manualCuts.forEach((position, index) => {
    const y = y0 + position * (y1 - y0);
    const selected = index === activeManualCut;
    manualContext.strokeStyle = selected ? "#ff7a21" : "#03c3cc";
    manualContext.lineWidth = selected ? Math.max(5, width / 300) : Math.max(3, width / 430);
    manualContext.beginPath();
    manualContext.moveTo(x0, y);
    manualContext.lineTo(x1, y);
    manualContext.stroke();
    const labelWidth = Math.max(58, width / 16);
    const labelHeight = Math.max(22, width / 52);
    manualContext.fillStyle = selected ? "#ff7a21" : "#03c3cc";
    manualContext.fillRect(x0, y - labelHeight, labelWidth, labelHeight);
    manualContext.fillStyle = "#101216";
    manualContext.font = `700 ${Math.max(11, width / 105)}px monospace`;
    manualContext.fillText(`CUT ${index + 1}`, x0 + 7, y - 7);
  });
  updateManualReadout();
}

function manualCanvasPoint(event) {
  const bounds = manualCanvas.getBoundingClientRect();
  return {
    x: (event.clientX - bounds.left) * manualCanvas.width / bounds.width,
    y: (event.clientY - bounds.top) * manualCanvas.height / bounds.height,
    hit: 13 * manualCanvas.width / bounds.width,
  };
}

function manualHitTarget(point) {
  const width = manualCanvas.width;
  const height = manualCanvas.height;
  const x0 = manualCrop.x0 * width;
  const y0 = manualCrop.y0 * height;
  const x1 = manualCrop.x1 * width;
  const y1 = manualCrop.y1 * height;
  const handles = [
    ["nw", x0, y0], ["ne", x1, y0], ["sw", x0, y1], ["se", x1, y1],
  ];
  const handle = handles.find(([, x, y]) => Math.hypot(point.x - x, point.y - y) <= point.hit * 1.5);
  if (handle) return { type: "handle", corner: handle[0] };
  const line = manualCuts.findIndex((position) => {
    const y = y0 + position * (y1 - y0);
    return point.x >= x0 && point.x <= x1 && Math.abs(point.y - y) <= point.hit;
  });
  if (line !== -1) return { type: "line", index: line };
  if (point.x >= x0 && point.x <= x1 && point.y >= y0 && point.y <= y1) return { type: "crop" };
  return null;
}

manualCanvas.addEventListener("pointerdown", (event) => {
  if (!parcelPageImage) return;
  const point = manualCanvasPoint(event);
  const target = manualHitTarget(point);
  if (!target) return;
  event.preventDefault();
  if (target.type === "line") activeManualCut = target.index;
  if (target.type === "line" && manualMoveMode === "linked") {
    distributeManualBands();
    drawManualEditor();
    return;
  }
  manualCanvas.setPointerCapture(event.pointerId);
  manualDrag = {
    ...target,
    start: point,
    crop: { ...manualCrop },
    cuts: [...manualCuts],
  };
  drawManualEditor();
});

manualCanvas.addEventListener("pointermove", (event) => {
  if (!manualDrag) return;
  const point = manualCanvasPoint(event);
  const dx = (point.x - manualDrag.start.x) / manualCanvas.width;
  const dy = (point.y - manualDrag.start.y) / manualCanvas.height;
  if (manualDrag.type === "crop") {
    const width = manualDrag.crop.x1 - manualDrag.crop.x0;
    const height = manualDrag.crop.y1 - manualDrag.crop.y0;
    const x0 = Math.max(0, Math.min(1 - width, manualDrag.crop.x0 + dx));
    const y0 = Math.max(0, Math.min(1 - height, manualDrag.crop.y0 + dy));
    manualCrop = { x0, y0, x1: x0 + width, y1: y0 + height };
  } else if (manualDrag.type === "handle") {
    const minimum = .06;
    manualCrop = { ...manualDrag.crop };
    if (manualDrag.corner.includes("w")) manualCrop.x0 = Math.max(0, Math.min(manualCrop.x1 - minimum, manualDrag.crop.x0 + dx));
    if (manualDrag.corner.includes("e")) manualCrop.x1 = Math.min(1, Math.max(manualCrop.x0 + minimum, manualDrag.crop.x1 + dx));
    if (manualDrag.corner.includes("n")) manualCrop.y0 = Math.max(0, Math.min(manualCrop.y1 - minimum, manualDrag.crop.y0 + dy));
    if (manualDrag.corner.includes("s")) manualCrop.y1 = Math.min(1, Math.max(manualCrop.y0 + minimum, manualDrag.crop.y1 + dy));
  } else if (manualDrag.type === "line") {
    const cropHeight = (manualDrag.crop.y1 - manualDrag.crop.y0) * manualCanvas.height;
    const delta = (point.y - manualDrag.start.y) / cropHeight;
    const index = manualDrag.index;
    const low = index ? manualDrag.cuts[index - 1] + .01 : .01;
    const high = index < manualDrag.cuts.length - 1 ? manualDrag.cuts[index + 1] - .01 : .99;
    manualCuts = [...manualDrag.cuts];
    manualCuts[index] = Math.max(low, Math.min(high, manualDrag.cuts[index] + delta));
  }
  if (manualMoveMode === "linked" && manualDrag.type !== "line") distributeManualBands();
  drawManualEditor();
});

function endManualDrag(event) {
  if (manualDrag && manualCanvas.hasPointerCapture(event.pointerId)) manualCanvas.releasePointerCapture(event.pointerId);
  manualDrag = null;
}
manualCanvas.addEventListener("pointerup", endManualDrag);
manualCanvas.addEventListener("pointercancel", endManualDrag);

function setManualCropPreset(name) {
  if (name === "right") manualCrop = { x0: .58, y0: .06, x1: .98, y1: .94 };
  else if (name === "chronopost") manualCrop = { x0: .608, y0: .155, x1: .98, y1: .842 };
  else manualCrop = { x0: .03, y0: .03, x1: .97, y1: .97 };
  equalizeManualCuts();
  drawManualEditor();
}

document.querySelectorAll("[data-crop-preset]").forEach((button) => {
  button.addEventListener("click", () => setManualCropPreset(button.dataset.cropPreset));
});

manualLinked.addEventListener("click", () => {
  manualMoveMode = "linked";
  manualLinked.classList.add("active");
  manualIndependent.classList.remove("active");
  distributeManualBands();
  drawManualEditor();
});
manualIndependent.addEventListener("click", () => {
  manualMoveMode = "independent";
  manualIndependent.classList.add("active");
  manualLinked.classList.remove("active");
});

manualAddBand.addEventListener("click", () => {
  distributeManualBands(manualCuts.length + 2);
  activeManualCut = manualCuts.length ? manualCuts.length - 1 : null;
  drawManualEditor();
});

manualRemoveBand.addEventListener("click", () => {
  if (!manualCuts.length) return;
  distributeManualBands(manualCuts.length);
  drawManualEditor();
});

async function loadParcelDraft(file) {
  parcelFileLabel.textContent = file?.name || "Choisir un bordereau";
  parcelError.textContent = "";
  manualError.textContent = "";
  parcelState.textContent = "Chargement de la page complète…";
  parcelManual.classList.add("hidden");
  parcelResult.classList.add("hidden");
  currentParcelId = null;
  currentParcelDraftId = null;
  if (!file) return;
  try {
    const data = new FormData();
    data.set("file", file);
    const response = await fetch("/api/parcels/prepare", { method: "POST", body: data });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Impossible d’ouvrir le bordereau");
    parcelDraft = result;
    currentParcelDraftId = result.id;
    parcelPageImage = new Image();
    await new Promise((resolve, reject) => {
      parcelPageImage.onload = resolve;
      parcelPageImage.onerror = reject;
      parcelPageImage.src = `${result.page_url}?v=${Date.now()}`;
    });
    manualCanvas.width = parcelPageImage.naturalWidth;
    manualCanvas.height = parcelPageImage.naturalHeight;
    document.querySelector("#manual-page-size").textContent = `${result.width_mm} × ${result.height_mm} MM`;
    manualCrop = { x0: .03, y0: .03, x1: .97, y1: .97 };
    equalizeManualCuts();
    parcelManual.classList.remove("hidden");
    parcelState.textContent = "Page chargée · ajustez le cadre orange et les traits cyan";
    drawManualEditor();
    parcelManual.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    parcelState.textContent = "";
    parcelError.textContent = error.message;
  }
}

parcelFile.addEventListener("change", () => loadParcelDraft(parcelFile.files?.[0]));
parcelForm.addEventListener("submit", (event) => event.preventDefault());

["dragenter", "dragover"].forEach((name) => parcelDropzone.addEventListener(name, (event) => {
  event.preventDefault();
  parcelDropzone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => parcelDropzone.addEventListener(name, (event) => {
  event.preventDefault();
  parcelDropzone.classList.remove("dragging");
}));
parcelDropzone.addEventListener("drop", (event) => {
  if (event.dataTransfer.files.length) {
    parcelFile.files = event.dataTransfer.files;
    loadParcelDraft(event.dataTransfer.files[0]);
  }
});

function presentParcel(result) {
  currentParcelId = result.id;
  document.querySelector("#parcel-carrier").textContent = result.carrier;
  document.querySelector("#parcel-confidence").textContent = result.model === "manual" ? "MANUEL" : `${Math.round(result.confidence * 100)}%`;
  document.querySelector("#parcel-format").textContent = `${result.label_width_mm} × ${result.label_height_mm} mm`;
  document.querySelector("#parcel-band-count").textContent = String(result.band_count).padStart(2, "0");
  const rollMillimeters = Math.round(result.roll_height / 300 * 25.4);
  document.querySelector("#parcel-roll-length").textContent = `${rollMillimeters} mm`;
  document.querySelector("#parcel-side").textContent = result.document_side === "custom" ? "ZONE PERSONNALISÉE" : `ZONE ${result.document_side.toUpperCase()}`;
  document.querySelector("#parcel-preview-size").textContent = `${result.band_count} BANDES · 300 DPI`;
  document.querySelector("#parcel-notes").textContent = result.notes || "Cadre et coupes préparés manuellement.";
  const bandList = document.querySelector("#parcel-band-list");
  bandList.replaceChildren(...result.band_heights_mm.map((height, index) => {
    const chip = document.createElement("span");
    chip.textContent = `${String(index + 1).padStart(2, "0")} · ${height} MM`;
    return chip;
  }));
  parcelPreview.src = `${result.preview_url}?v=${Date.now()}`;
  parcelResult.classList.remove("hidden");
  parcelResult.scrollIntoView({ behavior: "smooth", block: "start" });
}

manualAuto.addEventListener("click", async () => {
  if (!currentParcelDraftId) return;
  manualError.textContent = "";
  manualAuto.disabled = true;
  manualAuto.querySelector("span:first-child").childNodes[0].textContent = "Calcul en cours ";
  try {
    const response = await fetch(`/api/parcels/drafts/${currentParcelDraftId}/auto`, { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Auto calcul indisponible");
    manualCrop = Object.fromEntries(Object.entries(result.crop).map(([key, value]) => [key, value / 1000]));
    manualCuts = result.cuts.map((value) => value / 1000).sort((a, b) => a - b);
    activeManualCut = manualCuts.length ? 0 : null;
    if (manualMoveMode === "linked") distributeManualBands();
    parcelState.textContent = `Suggestion ${result.carrier} · ${Math.round(result.confidence * 100)}% · vérifiez tout`;
    drawManualEditor();
  } catch (error) {
    manualError.textContent = error.message;
  } finally {
    manualAuto.disabled = false;
    manualAuto.querySelector("span:first-child").childNodes[0].textContent = "Auto calcul ";
  }
});

parcelCompose.addEventListener("click", async () => {
  if (!currentParcelDraftId) return;
  manualError.textContent = "";
  parcelCompose.disabled = true;
  parcelCompose.querySelector("span:first-child").textContent = "Construction du rouleau…";
  try {
    const response = await fetch(`/api/parcels/drafts/${currentParcelDraftId}/compose`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        crop: normalizedCropPayload(),
        cuts: manualCuts,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Impossible de préparer les bandes");
    presentParcel(result);
    parcelState.textContent = "Sortie construite localement · vérifiez l’aperçu avant impression";
  } catch (error) {
    manualError.textContent = error.message;
  } finally {
    parcelCompose.querySelector("span:first-child").textContent = "Préparer les bandes";
    updateManualReadout();
  }
});

parcelPrintButton.addEventListener("click", async () => {
  if (!currentParcelId) return;
  parcelPrintError.textContent = "";
  parcelPrintState.textContent = "";
  parcelPrintButton.disabled = true;
  parcelPrintButton.querySelector("span:first-child").textContent = "Préparation du rouleau…";
  try {
    const data = new FormData();
    data.set("density", document.querySelector("#parcel-density").value);
    const response = await fetch(`/api/parcels/${currentParcelId}/print`, { method: "POST", body: data });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Impossible de lancer l’impression");
    currentJobId = result.id;
    currentJobStateTarget = parcelPrintState;
    currentJobErrorTarget = parcelPrintError;
    parcelPrintState.textContent = "Bandes en attente d’impression";
    await refreshHealth();
  } catch (error) {
    parcelPrintError.textContent = error.message;
  } finally {
    parcelPrintButton.disabled = false;
    parcelPrintButton.querySelector("span:first-child").textContent = "Imprimer les bandes";
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
    if (job.status === "queued") currentJobStateTarget.textContent = "Impression en attente";
    if (job.status === "printing") currentJobStateTarget.textContent = "Impression en cours…";
    if (job.status === "done") {
      currentJobStateTarget.textContent = "Impression terminée ✓";
      currentJobId = null;
    }
    if (job.status === "failed") {
      currentJobStateTarget.textContent = "";
      currentJobErrorTarget.textContent = job.error || "Échec de l’impression";
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
    currentJobStateTarget = printState;
    currentJobErrorTarget = errorBox;
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
