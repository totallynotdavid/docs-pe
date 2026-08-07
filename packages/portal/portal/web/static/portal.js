function setupAccountMenu() {
  const account = document.querySelector("[data-account]");
  const button = document.querySelector("[data-account-trigger]");
  const menu = document.querySelector("[data-account-menu]");

  if (!account || !button || !menu) {
    return;
  }

  function close() {
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
  }

  button.addEventListener("click", () => {
    const isOpen = menu.hidden;

    menu.hidden = !isOpen;
    button.setAttribute("aria-expanded", String(isOpen));
  });

  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Node) || !account.contains(event.target)) {
      close();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") {
      return;
    }

    close();
    button.focus();
  });
}

function setupCsvDropzone() {
  const dropzone = document.querySelector("[data-csv-dropzone]");
  const input = document.querySelector("[data-csv-input]");
  const status = document.querySelector("[data-csv-status]");
  const title = document.querySelector("[data-csv-title]");
  const detection = document.querySelector("[data-csv-detection]");

  if (!dropzone || !input || !status || !title) {
    return;
  }

  function describe(file) {
    title.textContent = file.name;
    status.textContent = `${(file.size / 1024).toFixed(1)} KB · listo para consultar`;
  }

  function detectDocuments(file) {
    if (!detection) {
      return;
    }

    const reader = new FileReader();

    reader.onload = () => {
      const firstDocument = String(reader.result)
        .replace(/^\uFEFF/, "")
        .split(/\r?\n/)
        .find(Boolean)
        ?.split(",")[0]
        ?.trim();

      if (/^10\d{9}$/.test(firstDocument)) {
        detection.hidden = false;
        detection.textContent =
          "Detectamos RUC de personas naturales. “DNI y nombre” ya está seleccionado.";
        return;
      }

      if (/^20\d{9}$/.test(firstDocument)) {
        detection.hidden = false;
        detection.textContent =
          "Detectamos RUC de empresa. Puedes elegir “Representantes legales”.";
        return;
      }

      if (/^\d{8}$/.test(firstDocument)) {
        detection.hidden = false;
        detection.textContent = "Detectamos DNI. Puedes elegir “Líneas móviles”.";
        return;
      }

      detection.hidden = true;
    };

    reader.readAsText(file.slice(0, 4096));
  }

  function updateSelectedFile(file) {
    describe(file);
    detectDocuments(file);
  }

  function selectFile(file) {
    const files = new DataTransfer();

    files.items.add(file);
    input.files = files.files;

    updateSelectedFile(file);
  }

  input.addEventListener("change", () => {
    const file = input.files[0];

    if (file) {
      updateSelectedFile(file);
    }
  });

  for (const type of ["dragenter", "dragover"]) {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.add("csv-file--dragging");
    });
  }

  for (const type of ["dragleave", "drop"]) {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.remove("csv-file--dragging");
    });
  }

  dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files[0];

    if (file) {
      selectFile(file);
    }
  });
}

function setupProgressStream() {
  const target = document.querySelector("[data-sse-url]");

  if (!target) {
    return;
  }

  const source = new EventSource(target.dataset.sseUrl);

  source.addEventListener("progress", (event) => {
    target.innerHTML = event.data;
  });

  source.addEventListener("done", () => {
    source.close();
  });
}

setupAccountMenu();
setupCsvDropzone();
setupProgressStream();
