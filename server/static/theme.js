/*
 * theme.js — the light/dark/system switcher, shared by every page (main
 * dashboard, admin table browser, register/confirm/missed forms) so it
 * behaves identically everywhere instead of being rebuilt per page.
 *
 * How it works: each page has a tiny inline script in <head>, before this
 * file loads, that reads localStorage and sets data-theme on <html> right
 * away (has to happen before first paint or you get a flash of the wrong
 * theme). This file is the rest of it — building the actual toggle button,
 * wiring clicks, and keeping "System" live if the OS theme changes while
 * the page is open. Drop `<div data-theme-toggle></div>` anywhere you want
 * the switch to appear; add `data-theme-toggle="inline"` if it's sitting
 * inside a topbar/sidebar (no floating position), otherwise it floats in
 * the corner — handy for the plain form pages that don't have a header.
 */

const THEME_KEY = '_sk_theme'; // 'light' | 'dark' | 'system' (default when unset)

function _sk_getStoredTheme(){
  const v = localStorage.getItem(THEME_KEY);
  // Default is 'light', not 'system' — a first-time visitor whose OS
  // happens to be in dark mode used to see the dark theme with zero
  // explicit choice on their part, before they'd ever touched the
  // toggle. 'system' is still available as an explicit pick, just not
  // silently what everyone starts on.
  if(v === 'light' || v === 'dark' || v === 'system') return v;
  return 'light';
}

function _sk_applyTheme(pref){
  const root = document.documentElement;
  if(pref === 'light' || pref === 'dark') root.setAttribute('data-theme', pref);
  else root.removeAttribute('data-theme'); // 'system' — let @media (prefers-color-scheme) decide
}

function _sk_setTheme(pref){
  localStorage.setItem(THEME_KEY, pref);
  _sk_applyTheme(pref);
  _sk_renderToggles();
}

// Keep "System" live — if someone switches their OS theme while this tab
// is open and they're on System mode, follow it without needing a reload.
if(window.matchMedia){
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(){
    if(_sk_getStoredTheme() === 'system') _sk_applyTheme('system');
  });
}

function _sk_themeToggleHtml(){
  const cur = _sk_getStoredTheme();
  const opt = (val, icon, label) =>
    '<button type="button" class="theme-opt' + (cur===val?' active':'') + '" ' +
    'onclick="_sk_setTheme(\'' + val + '\')" title="' + label + '" aria-label="' + label + ' theme">' +
    '<i data-lucide="' + icon + '"></i></button>';
  return '<div class="theme-switch">' + opt('light','sun','Light') + opt('dark','moon','Dark') + opt('system','monitor','System') + '</div>';
}

function _sk_renderToggles(){
  document.querySelectorAll('[data-theme-toggle]').forEach(function(el){
    el.innerHTML = _sk_themeToggleHtml();
  });
  if(typeof lucide !== 'undefined') lucide.createIcons();
}

// One shared stylesheet for the switch itself, injected once, so it
// doesn't need copy-pasting into three separate CSS files. Uses the same
// --bg2/--tx2/--tx/--acc/--b0 variable names every page already defines,
// so it automatically matches whichever page it's dropped into.
(function(){
  const css = '.theme-switch{display:inline-flex;align-items:center;gap:2px;background:var(--bg2);' +
    'border:1px solid var(--b0);border-radius:8px;padding:2px}' +
    '[data-theme-toggle]:not([data-theme-toggle="inline"]){position:fixed;top:16px;right:16px;z-index:200}' +
    '.theme-opt{display:flex;align-items:center;justify-content:center;width:26px;height:26px;' +
    'border:none;border-radius:6px;background:transparent;color:var(--tx3);cursor:pointer;' +
    'transition:background 0.12s,color 0.12s}' +
    '.theme-opt svg{width:13px;height:13px;stroke-width:2}' +
    '.theme-opt:hover{color:var(--tx2)}' +
    '.theme-opt.active{background:var(--bg1);color:var(--acc)}';
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);
})();

document.addEventListener('DOMContentLoaded', _sk_renderToggles);
