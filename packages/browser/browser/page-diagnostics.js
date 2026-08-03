(() => {
  if (window.__entelCaptureDiagnostics) return "already-installed";

  const fontCandidates = [
    "Arial",
    "Calibri",
    "Cambria",
    "Consolas",
    "Courier New",
    "Helvetica",
    "Noto Sans",
    "Segoe UI",
    "Times New Roman",
    "Ubuntu",
  ];

  function safeCall(callback, fallback = null) {
    try {
      return callback();
    } catch (error) {
      return fallback;
    }
  }

  function functionShape(value) {
    return safeCall(() => Function.prototype.toString.call(value).slice(0, 160));
  }

  function webglSignals() {
    return safeCall(
      () => {
        const canvas = document.createElement("canvas");
        const context = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
        if (!context) return { available: false };
        const extension = context.getExtension("WEBGL_debug_renderer_info");
        return {
          available: true,
          vendor: context.getParameter(context.VENDOR),
          renderer: context.getParameter(context.RENDERER),
          unmaskedVendor: extension ? context.getParameter(extension.UNMASKED_VENDOR_WEBGL) : null,
          unmaskedRenderer: extension
            ? context.getParameter(extension.UNMASKED_RENDERER_WEBGL)
            : null,
        };
      },
      { available: false },
    );
  }

  function templateSummary(template) {
    if (!template || typeof template !== "object") return null;
    const variables = (template.screenData && template.screenData.variables) || {};
    const clients = template.clientVariables || {};
    return {
      versionInfo: template.versionInfo || null,
      viewName: template.viewName || null,
      screenVariableKeys: Object.keys(variables).sort(),
      clientVariableKeys: Object.keys(clients).sort(),
      documentType: variables.DocumentType || null,
      recaptchaId: variables.RecaptchaId || null,
      serializedBytes: JSON.stringify(template).length,
    };
  }

  async function userAgentData() {
    if (!navigator.userAgentData) return null;
    const values = await safeCall(
      () =>
        navigator.userAgentData.getHighEntropyValues([
          "architecture",
          "bitness",
          "brands",
          "formFactors",
          "fullVersionList",
          "mobile",
          "model",
          "platform",
          "platformVersion",
          "uaFullVersion",
          "wow64",
        ]),
      null,
    );
    return values && typeof values.then === "function" ? await values : values;
  }

  async function permissionState(name) {
    const result = await safeCall(() => navigator.permissions.query({ name }), null);
    const resolved = result && typeof result.then === "function" ? await result : result;
    return resolved ? resolved.state : null;
  }

  window.__entelTemplateSummary = templateSummary;
  window.__entelCaptureDiagnostics = async function (stage, details = {}) {
    const cookieNames = document.cookie
      .split(";")
      .map((part) => part.split("=", 1)[0].trim())
      .filter(Boolean)
      .sort();
    const automationGlobals = Object.getOwnPropertyNames(window)
      .filter((name) => /cdc_|selenium|webdriver|driver|automation/i.test(name))
      .sort();
    const webdriverDescriptor = safeCall(() =>
      Object.getOwnPropertyDescriptor(Navigator.prototype, "webdriver"),
    );
    return {
      schemaVersion: 1,
      capturedAt: new Date().toISOString(),
      stage,
      page: {
        origin: location.origin,
        pathname: location.pathname,
        title: document.title,
        readyState: document.readyState,
        visibilityState: document.visibilityState,
        hasFocus: document.hasFocus(),
        secureContext: window.isSecureContext,
        historyLength: history.length,
      },
      navigator: {
        userAgent: navigator.userAgent,
        appVersion: navigator.appVersion,
        platform: navigator.platform,
        vendor: navigator.vendor,
        language: navigator.language,
        languages: [...navigator.languages],
        webdriver: navigator.webdriver,
        webdriverGetter: webdriverDescriptor ? functionShape(webdriverDescriptor.get) : null,
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory: navigator.deviceMemory || null,
        maxTouchPoints: navigator.maxTouchPoints,
        cookieEnabled: navigator.cookieEnabled,
        pdfViewerEnabled: navigator.pdfViewerEnabled,
        plugins: [...navigator.plugins].map((plugin) => plugin.name),
        mimeTypeCount: navigator.mimeTypes.length,
        userAgentData: await userAgentData(),
      },
      display: {
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        outerWidth: window.outerWidth,
        outerHeight: window.outerHeight,
        devicePixelRatio: window.devicePixelRatio,
        screenWidth: screen.width,
        screenHeight: screen.height,
        availWidth: screen.availWidth,
        availHeight: screen.availHeight,
        colorDepth: screen.colorDepth,
        pixelDepth: screen.pixelDepth,
      },
      environment: {
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        notificationPermission: await permissionState("notifications"),
        webgl: webglSignals(),
        fonts: Object.fromEntries(
          fontCandidates.map((font) => [font, document.fonts.check(`12px "${font}"`)]),
        ),
        chromeKeys: window.chrome ? Object.keys(window.chrome).sort() : [],
        automationGlobals,
        fetchShape: functionShape(window.fetch),
      },
      session: {
        cookieNames,
        documentCookieBytes: document.cookie.length,
        csrfPresent: /(?:^|;\s*)nr2Users=/.test(document.cookie),
        localStorageKeys: Object.keys(localStorage).sort(),
        sessionStorageKeys: Object.keys(sessionStorage).sort(),
      },
      recaptcha: {
        available: typeof window.grecaptcha !== "undefined",
        executeType: typeof (window.grecaptcha && window.grecaptcha.execute),
        enterpriseAvailable: Boolean(window.grecaptcha && window.grecaptcha.enterprise),
      },
      template: templateSummary(window.__entelBridgeTemplate || window.__entelTemplate || null),
      details,
    };
  };
  return "installed";
})();
