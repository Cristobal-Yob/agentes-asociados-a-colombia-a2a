const byId = (id) => document.getElementById(id);
let csrf = "";
let busy = false;
let selected = null;
let operations = [];

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

async function api(path, data) {
  const options = { credentials: "same-origin", headers: {} };
  if (data !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf };
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (response.status === 401) showAccess();
  if (!response.ok) throw new Error(result.detail || "No se pudo completar la solicitud.");
  return result;
}

function status(text, error = false) {
  byId("status").textContent = text;
  byId("status").dataset.error = String(error);
}

function setBusy(value) {
  busy = value;
  document.querySelectorAll("form button, #send, #recover").forEach((button) => {
    button.disabled = value || button.dataset.locked === "true";
  });
  byId("result").setAttribute("aria-busy", String(value));
}

function showAccess() {
  csrf = "";
  byId("workspace").hidden = true;
  byId("access").hidden = false;
  byId("logout").hidden = true;
  byId("result").replaceChildren();
}

function history() {
  const target = byId("history-list");
  target.replaceChildren();
  for (const operation of [...operations].reverse()) {
    const label = { trip: "Itinerario", draft: "Borrador", send: "Envío" }[operation.kind];
    const state = { pending: "en curso", unknown: "por comprobar", completed: "completado", error: "con error" }[operation.state];
    const button = element("button", `${label}: ${state}`);
    button.type = "button";
    button.addEventListener("click", () => render(operation));
    target.append(button);
  }
  if (!operations.length) target.append(element("p", "Aquí podrá volver a sus últimas solicitudes.", "muted"));
}

function remember(operation) {
  const index = operations.findIndex((item) => item.id === operation.id);
  if (index >= 0) operations[index] = operation;
  else operations.push(operation);
  history();
}

function render(operation) {
  selected = operation;
  remember(operation);
  byId("result-heading").textContent = { trip: "Su itinerario", draft: "Su borrador", send: "Resultado del envío" }[operation.kind];
  byId("result-state").textContent = { pending: "En curso", unknown: "Por comprobar", completed: "Completado", error: "Con error" }[operation.state];
  status(operation.message, operation.state === "error");
  byId("recover").hidden = !["pending", "unknown"].includes(operation.state);
  const target = byId("result");
  target.replaceChildren();
  if (operation.itinerary) {
    const trip = operation.itinerary;
    target.append(element("p", trip.summary, "trip-summary"));
    for (const day of trip.days || []) {
      const section = element("section", undefined, "trip-day");
      section.append(element("h3", `Día ${day.day}`));
      const list = element("ul");
      for (const activity of day.activities || []) list.append(element("li", activity));
      section.append(list);
      target.append(section);
    }
    const warnings = element("div", undefined, "warnings");
    for (const warning of trip.warnings || []) warnings.append(element("p", warning));
    target.append(warnings);
  }
  if (operation.draft) {
    const draft = operation.draft;
    const section = element("section", undefined, "mail-preview");
    section.append(element("h3", draft.subject));
    const metadata = element("div", undefined, "mail-meta");
    metadata.append(element("p", `Para: ${draft.recipient}`), element("p", `De: ${draft.sender}`));
    section.append(metadata, element("p", draft.text, "mail-body"));
    const send = element("button", "Enviar este correo", "button primary");
    send.id = "send";
    send.type = "button";
    const expired = Number(draft.expires_at) * 1000 <= Date.now();
    const locked = Boolean(draft.send_operation) || expired;
    send.dataset.locked = String(locked);
    send.disabled = busy || locked;
    send.addEventListener("click", () => submit(`/api/drafts/${encodeURIComponent(draft.id)}/send`, {}, true));
    section.append(send);
    section.append(element("p", draft.send_operation ? "Este borrador ya tiene una operación de envío. No se volverá a enviar." : expired ? "Este borrador venció. Prepare uno nuevo." : "El botón envía exactamente este contenido. Si desea cambios, prepare un nuevo borrador. Vigencia: cinco minutos.", "expiry"));
    target.append(section);
  }
  if (operation.receipt) {
    const receipt = element("div", undefined, "receipt");
    const accepted = operation.receipt.status === "sent";
    receipt.append(element("h3", accepted ? "El proveedor aceptó el correo" : "El correo no se completó"));
    receipt.append(element("p", accepted ? "La aceptación no garantiza que llegue al buzón. Revise también la carpeta de spam." : "No se repetirá este envío automáticamente. Compruebe el resultado antes de crear otro."));
    target.append(receipt);
  }
}

async function refreshSession() {
  const session = await api("/api/session");
  csrf = session.csrf;
  operations = session.operations;
  byId("account").textContent = session.email;
  byId("access").hidden = true;
  byId("workspace").hidden = false;
  byId("logout").hidden = false;
  history();
  if (operations.length) render(operations[operations.length - 1]);
}

async function submit(path, data, sending = false) {
  if (busy) return;
  setBusy(true);
  status(sending ? "Enviando este borrador una sola vez. Espere el recibo." : "Preparando su solicitud. No se enviará ningún correo.");
  byId("result").replaceChildren(element("p", "La respuesta del agente aparecerá aquí.", "loading-placeholder"));
  try {
    render(await api(path, data));
    byId("result-heading").focus();
  } catch (error) {
    if (sending) {
      try { await refreshSession(); } catch { /* Keep the uncertainty visible. */ }
      status("No se pudo confirmar el resultado. No vuelva a enviar. Compruebe la operación pendiente o vuelva a iniciar sesión.", true);
    } else status(error.message, true);
  } finally { setBusy(false); }
}

byId("login").addEventListener("click", (event) => {
  if (byId("login").getAttribute("aria-disabled") === "true") event.preventDefault();
});
byId("logout").addEventListener("click", async () => {
  try { await api("/auth/logout", {}); showAccess(); }
  catch (error) { status(error.message, true); }
});
for (const mode of ["trip", "mail"]) {
  byId(`${mode}-mode`).addEventListener("click", () => {
    for (const other of ["trip", "mail"]) {
      byId(`${other}-form`).hidden = other !== mode;
      byId(`${other}-mode`).setAttribute("aria-pressed", String(other === mode));
    }
  });
}
byId("prepare-email").addEventListener("change", (event) => {
  byId("trip-recipient-row").hidden = !event.target.checked;
  byId("trip-recipient").required = event.target.checked;
});
byId("trip-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const prepare = byId("prepare-email").checked;
  submit("/api/itineraries", {
    city: byId("city").value, days: Number(byId("days").value),
    interests: [...document.querySelectorAll('input[name="interest"]:checked')].map((input) => input.value),
    prepare_email: prepare, ...(prepare ? { recipient: byId("trip-recipient").value } : {}),
  });
});
byId("mail-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submit("/api/drafts", { recipient: byId("recipient").value, request: byId("mail-request").value });
});
byId("recover").addEventListener("click", async () => {
  if (!selected || busy) return;
  setBusy(true);
  try { render(await api(`/api/operations/${encodeURIComponent(selected.id)}`)); }
  catch (error) { status(error.message, true); }
  finally { setBusy(false); }
});

try {
  const config = await api("/api/config");
  if (!config.configured) {
    byId("access-note").textContent = "Acceso en preparación. Falta configurar Google y la persistencia de esta aplicación. No se pueden ejecutar solicitudes todavía.";
  } else {
    byId("login").setAttribute("aria-disabled", "false");
    byId("access-note").textContent = "Acceso privado con una cuenta de Google autorizada. No solicita permisos de Gmail para iniciar sesión.";
    try { await refreshSession(); } catch { showAccess(); }
  }
} catch {
  byId("access-note").textContent = "No se pudo comprobar el acceso. Recargue la página o vuelva a Agentic.";
}
