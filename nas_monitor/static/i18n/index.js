/*
 * nas_monitor i18n loader - no build step, plain <script> tags (see
 * dashboard.html: pl.js and en.js each populate window.NAS_I18N[lang]
 * and must load before this file; this file must load before
 * dashboard.js).
 *
 * Backend error/warning codes and operations-log entries are never
 * translated in Python - see nas_monitor/errors.py and nas_monitor/
 * oplog.py. This is the ONLY place user-facing text is resolved, via
 * t("err.<code>", context) / t("warn.<code>", context) /
 * t(`log.${category}.${action}.${status}`, params). Adding a language
 * means adding one more file here (pl.js/en.js as a template) - nothing
 * in Python or the rest of the frontend needs to change.
 */

(function () {
  const STORAGE_KEY = "nas-monitor-lang";
  const FALLBACK_LANG = "en";
  const DEFAULT_LANG = "pl";

  function availableLanguages() {
    return Object.keys(window.NAS_I18N || {});
  }

  function detectLanguage() {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && window.NAS_I18N[saved]) return saved;
    const browserLang = (navigator.language || "").slice(0, 2).toLowerCase();
    if (window.NAS_I18N[browserLang]) return browserLang;
    return window.NAS_I18N[DEFAULT_LANG] ? DEFAULT_LANG : availableLanguages()[0];
  }

  let currentLang = detectLanguage();
  const listeners = [];

  function get(dict, key) {
    return key
      .split(".")
      .reduce((obj, part) => (obj && typeof obj === "object" ? obj[part] : undefined), dict);
  }

  function interpolate(str, params) {
    if (!params) return str;
    return str.replace(/\{(\w+)\}/g, (match, name) => (params[name] !== undefined && params[name] !== null ? params[name] : match));
  }

  function t(key, params) {
    const dict = window.NAS_I18N[currentLang] || {};
    let str = get(dict, key);
    if (str === undefined) {
      const fallbackDict = window.NAS_I18N[FALLBACK_LANG] || {};
      str = get(fallbackDict, key);
    }
    if (str === undefined) {
      console.warn(`[i18n] missing key: ${key}`);
      return key;
    }
    return interpolate(str, params);
  }

  // Resolve a backend error_code/error_context pair into display text.
  // Falls back to a generic "unknown error" message (still translated)
  // if a code has no matching entry yet, rather than showing the raw
  // code to the user.
  function errorText(errorCode, errorContext) {
    if (!errorCode) return t("err._unknown");
    const key = `err.${errorCode}`;
    const dict = window.NAS_I18N[currentLang] || {};
    const fallbackDict = window.NAS_I18N[FALLBACK_LANG] || {};
    if (get(dict, key) === undefined && get(fallbackDict, key) === undefined) {
      console.warn(`[i18n] missing error code translation: ${errorCode}`);
      return t("err._unknown");
    }
    return t(key, errorContext);
  }

  function warningText(code, context) {
    return t(`warn.${code}`, context);
  }

  function noteText(code, context) {
    return t(`note.${code}`, context);
  }

  function logSummary(category, action, status, params) {
    return t(`log.${category}.${action}.${status}`, params);
  }

  function applyTranslations(root) {
    const scope = root || document;
    scope.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    scope.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
    });
    scope.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
    });
  }

  function setLanguage(lang) {
    if (!window.NAS_I18N[lang] || lang === currentLang) return;
    currentLang = lang;
    localStorage.setItem(STORAGE_KEY, lang);
    document.documentElement.setAttribute("lang", lang);
    applyTranslations(document);
    listeners.forEach((fn) => fn(lang));
  }

  function onLanguageChange(fn) {
    listeners.push(fn);
  }

  function currentLanguage() {
    return currentLang;
  }

  document.documentElement.setAttribute("lang", currentLang);

  window.i18n = {
    t,
    errorText,
    warningText,
    noteText,
    logSummary,
    applyTranslations,
    setLanguage,
    onLanguageChange,
    currentLanguage,
    availableLanguages,
  };

  // Static markup (data-i18n attributes already in the HTML, which ships
  // with Polish fallback text) needs translating immediately on load -
  // not just on a later language switch, since the detected language
  // (saved preference or browser language) may not be Polish.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => applyTranslations(document));
  } else {
    applyTranslations(document);
  }
})();
