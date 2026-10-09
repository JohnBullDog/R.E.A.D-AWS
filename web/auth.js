// Shared passcode for the hosted R.E.A.D. (D90): every /api request carries it in the
// X-Read-Passcode header; API Gateway's authorizer checks it. On 401/403 the page asks for it
// once and retries. The passcode is kept in this browser only (localStorage).
(function () {
  const KEY = "read-passcode";
  const HEADER = "X-Read-Passcode";
  const plain = window.fetch.bind(window);
  let asking = null;

  function saved() {
    try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; }
  }
  function save(v) {
    try { localStorage.setItem(KEY, v); } catch (e) { /* private window: ask again next time */ }
  }

  function ask(wrong) {
    if (asking) return asking;
    asking = new Promise((resolve) => {
      const shade = document.createElement("div");
      shade.style.cssText = "position:fixed;inset:0;background:rgba(15,20,26,.55);display:flex;" +
        "align-items:center;justify-content:center;z-index:1000;padding:16px";
      const box = document.createElement("form");
      box.className = "container";
      box.style.cssText = "max-width:380px;width:100%;background:var(--panel);padding:20px";
      const h = document.createElement("h2");
      h.textContent = "Enter the R.E.A.D. passcode";
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = wrong ? "That passcode wasn't accepted. Try again."
        : "This preview is shared with a passcode. Ask the project lead if you don't have it.";
      const input = document.createElement("input");
      input.type = "password";
      input.autocomplete = "current-password";
      input.style.width = "100%";
      input.setAttribute("aria-label", "Passcode");
      const go = document.createElement("button");
      go.type = "submit";
      go.textContent = "Continue";
      go.style.marginTop = "12px";
      box.append(h, p, input, go);
      shade.append(box);
      document.body.append(shade);
      input.focus();
      box.addEventListener("submit", (ev) => {
        ev.preventDefault();
        if (!input.value.trim()) return;
        save(input.value.trim());
        shade.remove();
        asking = null;
        resolve();
      });
    });
    return asking;
  }

  window.fetch = async function (url, opts) {
    // pages use relative addresses so the site also works under a prefix (bucklersoftware.com/READ/)
    const path = new URL(typeof url === "string" ? url : url.url, location.href).pathname;
    if (!/(^|\/)api\//.test(path)) return plain(url, opts);
    for (let attempt = 0; ; attempt++) {
      const o = Object.assign({}, opts || {});
      const headers = new Headers(o.headers || {});
      const code = saved();
      if (code) headers.set(HEADER, code);
      o.headers = headers;
      const r = await plain(url, o);
      if ((r.status !== 401 && r.status !== 403) || attempt >= 3) return r;
      await ask(Boolean(code));
    }
  };
})();
