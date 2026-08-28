function setupSidebarCollapse() {
  const shell = document.querySelector(".app-shell");
  const toggles = document.querySelectorAll("[data-sidebar-toggle]");
  const key = "portal-sidebar-collapsed";

  if (!shell || !toggles.length) {
    return;
  }

  try {
    if (localStorage.getItem(key) === "1") {
      shell.dataset.sidebarCollapsed = "1";
    }
  } catch {
    // No persisted preference to read; the shell stays expanded for this load.
  }

  for (const toggle of toggles) {
    toggle.addEventListener("click", () => {
      // Animate user-triggered changes, not the page-load correction.
      shell.classList.add("app-shell--transitioning");

      const collapsed = shell.dataset.sidebarCollapsed === "1";

      if (collapsed) {
        delete shell.dataset.sidebarCollapsed;
      } else {
        shell.dataset.sidebarCollapsed = "1";
      }

      try {
        localStorage.setItem(key, collapsed ? "0" : "1");
      } catch {
        // Toggles for this load regardless; just doesn't persist.
      }
    });
  }
}

function setupDropdownMenus() {
  // One handler per [data-dropdown] container: the account menu and the
  // team switcher both use this, independently, on the same page.
  const containers = document.querySelectorAll("[data-dropdown]");

  for (const container of containers) {
    const button = container.querySelector("[data-dropdown-trigger]");
    const menu = container.querySelector("[data-dropdown-menu]");

    if (!button || !menu) {
      continue;
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
      if (!(event.target instanceof Node) || !container.contains(event.target)) {
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
}

function setupMobileNav() {
  const shell = document.querySelector(".app-shell");
  const toggle = document.querySelector("[data-mobile-nav-toggle]");
  const panel = document.querySelector("[data-mobile-nav-panel]");

  if (!shell || !toggle || !panel) {
    return;
  }

  function setOpen(open) {
    if (open) {
      shell.dataset.mobileNavOpen = "1";
    } else {
      delete shell.dataset.mobileNavOpen;
    }

    toggle.setAttribute("aria-expanded", String(open));
  }

  toggle.addEventListener("click", () => {
    setOpen(shell.dataset.mobileNavOpen !== "1");
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && shell.dataset.mobileNavOpen === "1") {
      setOpen(false);
      toggle.focus();
    }
  });
}

function setupCsvDropzone() {
  const dropzone = document.querySelector("[data-csv-dropzone]");
  const input = document.querySelector("[data-csv-input]");
  const status = document.querySelector("[data-csv-status]");
  const title = document.querySelector("[data-csv-title]");
  const detection = document.querySelector("[data-csv-detection]");
  // The banner keeps a static icon beside the text, so the copy goes into an
  // inner span rather than replacing the banner's children.
  const detectionText = document.querySelector("[data-csv-detection-text]");

  if (!dropzone || !input || !status || !title) {
    return;
  }

  function describe(file) {
    title.textContent = file.name;
    status.textContent = `${(file.size / 1024).toFixed(1)} KB · listo para consultar`;
  }

  // A file over the limit still uploads and hangs the tab instead of
  // failing fast: the server rejects it by Content-Length before reading
  // the body, and the ASGI server closes the connection mid-upload rather
  // than draining it, which browsers don't surface as a clean error.
  // Blocking submission here means those bytes never go over the wire.
  function validateSize(file) {
    const maxBytes = Number(input.dataset.csvMaxBytes);

    if (!maxBytes || file.size <= maxBytes) {
      input.setCustomValidity("");
      return true;
    }

    const message = `el archivo CSV no puede superar los ${input.dataset.csvMaxMb} MB`;

    input.setCustomValidity(message);
    status.textContent = message;
    return false;
  }

  function detectDocuments(file) {
    if (!detection || !detectionText) {
      return;
    }

    const reader = new FileReader();

    function announce(message) {
      detection.hidden = false;
      detectionText.textContent = message;
    }

    reader.onload = () => {
      const firstDocument = String(reader.result)
        .replace(/^\uFEFF/, "")
        .split(/\r?\n/)
        .find(Boolean)
        ?.split(",")[0]
        ?.trim();

      if (/^10\d{9}$/.test(firstDocument)) {
        announce("Detectamos RUC de personas naturales. “DNI y nombre” ya está seleccionado.");
        return;
      }

      if (/^20\d{9}$/.test(firstDocument)) {
        announce("Detectamos RUC de empresa. Puedes elegir “Representantes legales”.");
        return;
      }

      if (/^\d{8}$/.test(firstDocument)) {
        announce("Detectamos DNI. Puedes elegir “Líneas móviles”.");
        return;
      }

      detection.hidden = true;
    };

    reader.readAsText(file.slice(0, 4096));
  }

  function updateSelectedFile(file) {
    describe(file);

    if (validateSize(file)) {
      detectDocuments(file);
    } else if (detection) {
      detection.hidden = true;
    }
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

function setupNavSections() {
  const sections = document.querySelectorAll("[data-nav-section]");

  for (const section of sections) {
    const key = `portal-nav-section:${section.dataset.navSection}`;

    try {
      if (localStorage.getItem(key) === "closed") {
        section.removeAttribute("open");
      }
    } catch {
      continue;
    }

    section.addEventListener("toggle", () => {
      try {
        localStorage.setItem(key, section.open ? "open" : "closed");
      } catch {
        // Nothing to persist to; the section still toggles for this load.
      }
    });
  }
}

function setupConfirmSubmit() {
  const forms = document.querySelectorAll("[data-confirm]");

  for (const form of forms) {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
      }
    });
  }
}

function setupRecoveryCodes() {
  const button = document.querySelector("[data-recovery-copy]");
  const label = document.querySelector("[data-recovery-copy-label]");
  const codes = document.querySelectorAll("[data-recovery-code]");

  if (!button || !label || !codes.length) {
    return;
  }

  const initialLabel = label.textContent.trim();
  let resetTimer;

  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(
        Array.from(codes, (code) => code.textContent.trim()).join("\n"),
      );
    } catch {
      return;
    }

    label.textContent = "Códigos copiados";
    clearTimeout(resetTimer);
    resetTimer = setTimeout(() => {
      label.textContent = initialLabel;
    }, 1500);
  });
}

// navigator.credentials wants ArrayBuffers where the server's JSON carries
// base64url text (challenge, credential ids, the public key user handle),
// and wants the reverse on the way back. These four functions are the only
// place that conversion happens.

function base64urlToBuffer(value) {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  const withPadding = padded.padEnd(padded.length + ((4 - (padded.length % 4)) % 4), "=");
  const binary = atob(withPadding);
  const bytes = new Uint8Array(binary.length);

  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }

  return bytes.buffer;
}

function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";

  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }

  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decodeCredentialOptions(options, kind) {
  const decoded = { ...options, challenge: base64urlToBuffer(options.challenge) };

  if (kind === "create") {
    decoded.user = { ...options.user, id: base64urlToBuffer(options.user.id) };

    if (options.excludeCredentials) {
      decoded.excludeCredentials = options.excludeCredentials.map((credential) => ({
        ...credential,
        id: base64urlToBuffer(credential.id),
      }));
    }
  } else if (options.allowCredentials) {
    decoded.allowCredentials = options.allowCredentials.map((credential) => ({
      ...credential,
      id: base64urlToBuffer(credential.id),
    }));
  }

  return decoded;
}

function encodeRegistrationCredential(credential) {
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      attestationObject: bufferToBase64url(credential.response.attestationObject),
      transports: credential.response.getTransports
        ? credential.response.getTransports()
        : [],
    },
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

function encodeAuthenticationCredential(credential) {
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      authenticatorData: bufferToBase64url(credential.response.authenticatorData),
      signature: bufferToBase64url(credential.response.signature),
      userHandle: credential.response.userHandle
        ? bufferToBase64url(credential.response.userHandle)
        : null,
    },
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

  return { ok: response.ok, data: await response.json() };
}

// Shared by Login.jinja (no pending_mfa cookie: passwordless) and
// MfaChallenge.jinja (cookie present: a passkey offered instead of a TOTP
// code). Which case applies is decided server-side from that cookie, so the
// client script is identical either way.
function setupPasskeyLogin() {
  const form = document.querySelector('form[action="/login/passkey/verify"]');

  if (!form) {
    return;
  }

  const button = form.querySelector("[data-passkey-login]");
  const status = form.querySelector("[data-passkey-status]");
  const loginTokenInput = form.querySelector("[data-passkey-login-token]");
  const responseInput = form.querySelector("[data-passkey-response]");

  if (!window.PublicKeyCredential) {
    button.hidden = true;
    return;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    if (status) status.textContent = "Esperando tu dispositivo...";

    try {
      const { ok: optionsOk, data: optionsData } = await postJson(
        "/login/passkey/options",
      );

      if (!optionsOk) {
        throw new Error("options");
      }

      const assertion = await navigator.credentials.get({
        publicKey: decodeCredentialOptions(optionsData.options, "get"),
      });

      loginTokenInput.value = optionsData.loginToken;
      responseInput.value = JSON.stringify(encodeAuthenticationCredential(assertion));
      form.submit();
    } catch (error) {
      if (status) {
        status.textContent =
          error && error.name === "NotAllowedError"
            ? "Operación cancelada."
            : "No se pudo verificar la clave de acceso.";
      }

      button.disabled = false;
    }
  });
}

// Add a passkey to the signed-in account and submit the WebAuthn result.
function setupPasskeyEnrollment() {
  const form = document.querySelector('form[action="/security/passkey/register"]');

  if (!form) {
    return;
  }

  const button = form.querySelector("[data-passkey-add]");
  const status = form.querySelector("[data-passkey-add-status]");
  const setupTokenInput = form.querySelector("[data-passkey-setup-token]");
  const responseInput = form.querySelector("[data-passkey-response]");

  if (!window.PublicKeyCredential) {
    button.disabled = true;
    if (status) status.textContent = "Este navegador no admite claves de acceso.";
    return;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    if (status) status.textContent = "Esperando tu dispositivo...";

    try {
      const { ok: optionsOk, data: optionsData } = await postJson(
        "/security/passkey/options",
        { csrf_token: form.elements.csrf_token.value },
      );

      if (!optionsOk) {
        throw new Error("options");
      }

      const credential = await navigator.credentials.create({
        publicKey: decodeCredentialOptions(optionsData.options, "create"),
      });

      setupTokenInput.value = optionsData.setupToken;
      responseInput.value = JSON.stringify(encodeRegistrationCredential(credential));
      // Not requestSubmit(): that re-fires this same "submit" listener.
      form.submit();
    } catch (error) {
      if (status) {
        status.textContent =
          error && error.name === "NotAllowedError"
            ? "Operación cancelada."
            : "No se pudo registrar la clave de acceso.";
      }

      button.disabled = false;
    }
  });
}

setupSidebarCollapse();
setupDropdownMenus();
setupMobileNav();
setupNavSections();
setupConfirmSubmit();
setupCsvDropzone();
setupProgressStream();
setupRecoveryCodes();
setupPasskeyLogin();
setupPasskeyEnrollment();
