(() => {
  const RELAY = __RELAY_URL__;
  const TOKEN = __RELAY_TOKEN__;
  const ENTEL_ENDPOINT =
    "https://miperfil.entel.pe/PE_Web_Cobro_Online_EU/screenservices/" +
    "PE_Web_Cobro_Online_CW/OnlinePayment/OnlinePayment_Step2/DataActionGetData";

  window.__entelCaptureTemplate = null;
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__entelCaptureUrl = url;
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (String(this.__entelCaptureUrl || "").includes("Step2/DataActionGetData")) {
      try {
        window.__entelCaptureTemplate = JSON.parse(body);
        console.log("Template captured. The spinner should remain visible.");
      } catch (error) {
        console.error("Could not capture the Entel request template", error);
      }
      return;
    }
    return originalSend.apply(this, arguments);
  };

  async function relay(path, options = {}) {
    const response = await fetch(RELAY + path, {
      ...options,
      mode: "cors",
      cache: "no-store",
      headers: {
        "content-type": "application/json",
        "x-capture-token": TOKEN,
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      throw new Error(`Relay ${path} returned HTTP ${response.status}`);
    }
    return response.json();
  }

  async function lookup(ruc) {
    const started = performance.now();
    const tokenStarted = performance.now();
    const token = await grecaptcha.execute("0", {action: "SearchDebt"});
    const mintMs = Math.round(performance.now() - tokenStarted);
    const body = JSON.parse(JSON.stringify(window.__entelCaptureTemplate));
    body.screenData.variables.DocumentNumber = ruc;
    body.clientVariables.TokenCaptchaV3 = token;
    const match = decodeURIComponent(document.cookie).match(/crf=([^;]+)/);
    const response = await fetch(ENTEL_ENDPOINT, {
      method: "POST",
      credentials: "include",
      body: JSON.stringify(body),
      headers: {
        accept: "application/json",
        "content-type": "application/json; charset=UTF-8",
        "x-csrftoken": match ? match[1] : "",
      },
    });
    const payload = await response.json();
    const data = payload.data || {};
    const resource = performance.getEntriesByName(ENTEL_ENDPOINT).slice(-1)[0];
    const diagnostic = window.__entelCaptureDiagnostics
      ? await window.__entelCaptureDiagnostics("lookup", {
          ruc,
          transaction: {
            requestBodyBytes: JSON.stringify(body).length,
            csrfLength: match ? match[1].length : 0,
            tokenLength: token ? token.length : 0,
            mintMs,
            responseStatus: response.status,
            responseType: response.type,
            responseRedirected: response.redirected,
            serverHasError: data.HasErrorDebt,
            debtTotal: data.Debt ? data.Debt.DebtTotal : null,
            responseHeaders: Object.fromEntries(response.headers.entries()),
            resourceTiming: resource ? {
              duration: Math.round(resource.duration),
              transferSize: resource.transferSize,
              encodedBodySize: resource.encodedBodySize,
              decodedBodySize: resource.decodedBodySize,
              nextHopProtocol: resource.nextHopProtocol,
            } : null,
          },
        })
      : null;
    return {
      ruc,
      hasError: data.HasErrorDebt,
      debt: data.Debt || null,
      httpStatus: response.status,
      mintMs,
      tokenLength: token ? token.length : 0,
      elapsedMs: Math.round(performance.now() - started),
      diagnostic,
    };
  }

  async function run(button, output) {
    if (!window.__entelCaptureTemplate) {
      throw new Error("Drive the Entel form first. The loading spinner must hang.");
    }
    button.disabled = true;
    let completed = 0;
    while (true) {
      const next = await relay("/next");
      if (next.done) {
        output.textContent += `\nCompleted ${completed} client lookups.`;
        button.textContent = "DONE";
        console.log(`Entel collection complete: ${completed} lookups`);
        return;
      }
      let result;
      try {
        result = await lookup(next.ruc);
      } catch (error) {
        result = {ruc: next.ruc, exception: String(error && error.message || error)};
      }
      await relay("/result", {method: "POST", body: JSON.stringify(result)});
      completed += 1;
      const total = result.debt ? result.debt.DebtTotal : null;
      const line = `${next.ruc}: err=${result.hasError} total=${total}`;
      output.textContent += `\n${line}`;
      console.log(line, result);
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }

  let button = document.getElementById("entel-capture-go");
  if (!button) {
    const panel = document.createElement("div");
    panel.style = "position:fixed;top:50px;right:20px;z-index:2147483647;" +
      "background:white;color:#111;padding:12px;max-width:520px;" +
      "max-height:70vh;overflow:auto;border:2px solid #087f23";
    button = document.createElement("button");
    button.id = "entel-capture-go";
    button.textContent = "RUN CLIENTS";
    button.style = "padding:16px 24px;font-size:18px";
    const output = document.createElement("pre");
    output.id = "entel-capture-output";
    output.textContent = "Drive the RUC form, then click RUN CLIENTS.";
    button.onclick = async () => {
      try {
        await run(button, output);
      } catch (error) {
        button.disabled = false;
        output.textContent += `\nSTOPPED: ${error.message}`;
        console.error("Entel collector stopped", error);
      }
    };
    panel.append(button, output);
    document.body.appendChild(panel);
  }
  console.log("Entel capture ready. Drive the form and expect the spinner to hang.");
})();
