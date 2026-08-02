const loginForm = document.getElementById("login-form");
const loginError = document.getElementById("login-error");
const loginSubmit = document.getElementById("login-submit");
const loginCard = document.getElementById("login-card");
const loginBlade = document.getElementById("login-blade");

// A quick orange sweep around the card border on a failed login - the
// card's rendered size varies with viewport/content, so the path is
// built from its actual measured box each time rather than a fixed one.
function cardBorderPath() {
  const w = loginCard.offsetWidth;
  const h = loginCard.offsetHeight;
  const r = 14; // matches .login-card's border-radius
  return `M ${r + 1},1 L ${w - r - 1},1 A ${r},${r} 0 0 1 ${w - 1},${r + 1} L ${w - 1},${h - r - 1} A ${r},${r} 0 0 1 ${w - r - 1},${h - 1} L ${r + 1},${h - 1} A ${r},${r} 0 0 1 1,${h - r - 1} L 1,${r + 1} A ${r},${r} 0 0 1 ${r + 1},1 Z`;
}

function playLoginErrorGlow() {
  loginBlade.style.offsetPath = `path('${cardBorderPath()}')`;
  loginBlade.style.transition = "none";
  loginBlade.style.offsetDistance = "0%";
  loginBlade.style.opacity = "0";
  loginBlade.getBoundingClientRect();
  loginBlade.style.transition = "opacity 0.08s linear";
  loginBlade.style.opacity = "1";
  requestAnimationFrame(() => {
    loginBlade.style.transition = "offset-distance 1.3s cubic-bezier(0.4,0,0.2,1)";
    loginBlade.style.offsetDistance = "96%";
  });
  setTimeout(() => {
    loginBlade.style.transition = "opacity 0.35s ease-out";
    loginBlade.style.opacity = "0";
  }, 1250);
}

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
      playLoginErrorGlow();
      return;
    }
    window.location.href = "/";
  } catch (err) {
    loginError.textContent = window.i18n.t("msg.connectionErrorDetail", { detail: err.message });
    playLoginErrorGlow();
  } finally {
    loginSubmit.disabled = false;
  }
});
