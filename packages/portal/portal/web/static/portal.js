// Progressive enhancement for the two widgets htmx cannot express as attributes.
// Each block leaves the page alone when its markup is absent, so one file can be
// loaded from every layout.

(() => {
  const account = document.querySelector('[data-cuenta]');
  const button = document.querySelector('[data-cuenta-activador]');
  const menu = document.querySelector('[data-cuenta-menu]');
  if (!account || !button || !menu) return;
  const close = () => { menu.hidden = true; button.setAttribute('aria-expanded', 'false'); };
  button.addEventListener('click', () => {
    const open = menu.hidden;
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('click', event => { if (!account.contains(event.target)) close(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') { close(); button.focus(); } });
})();

(() => {
  const dropzone = document.querySelector('[data-csv-dropzone]');
  const input = document.querySelector('[data-csv-input]');
  const status = document.querySelector('[data-csv-status]');
  const title = document.querySelector('[data-csv-title]');
  const detection = document.querySelector('[data-csv-detection]');
  if (!dropzone || !input || !status || !title) return;
  const describe = file => { title.textContent = file.name; status.textContent = `${(file.size / 1024).toFixed(1)} KB · listo para consultar`; };
  const detectDocuments = file => {
    if (!detection || !file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const first = String(reader.result).replace(/^\uFEFF/, '').split(/\r?\n/).find(Boolean)?.split(',')[0]?.trim();
      if (/^10\d{9}$/.test(first)) { detection.hidden = false; detection.textContent = 'Detectamos RUC de personas naturales. “DNI y nombre” ya está seleccionado.'; }
      else if (/^20\d{9}$/.test(first)) { detection.hidden = false; detection.textContent = 'Detectamos RUC de empresa. Puedes elegir “Representantes legales”.'; }
      else if (/^\d{8}$/.test(first)) { detection.hidden = false; detection.textContent = 'Detectamos DNI. Puedes elegir “Líneas móviles”.'; }
      else { detection.hidden = true; }
    };
    reader.readAsText(file.slice(0, 4096));
  };
  input.addEventListener('change', () => { if (input.files[0]) { describe(input.files[0]); detectDocuments(input.files[0]); } });
  ['dragenter', 'dragover'].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.add('archivo-csv--arrastrando'); }));
  ['dragleave', 'drop'].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.remove('archivo-csv--arrastrando'); }));
  dropzone.addEventListener('drop', event => { const [file] = event.dataTransfer.files; if (!file) return; const files = new DataTransfer(); files.items.add(file); input.files = files.files; describe(file); detectDocuments(file); });
})();
