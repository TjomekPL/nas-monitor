const loginForm = document.getElementById("login-form");
const loginError = document.getElementById("login-error");
const loginSubmit = document.getElementById("login-submit");

loginForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  loginError.textContent = "";

  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value;

  loginSubmit.disabled = true;
  try {
    const res = await fetch("/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      loginError.textContent = data.error_code
        ? window.i18n.errorText(data.error_code, data.error_context)
        : window.i18n.t("msg.httpError", { status: res.status });
      return;
    }
    window.location.href = "/";
  } catch (err) {
    loginError.textContent = window.i18n.t("msg.connectionErrorDetail", { detail: err.message });
  } finally {
    loginSubmit.disabled = false;
  }
});
