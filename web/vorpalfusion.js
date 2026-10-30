// VorpalFusion's entries in ComfyUI: a sidebar tab with the live page in it, a top-bar button and a menu command
// that open the page in its own browser tab, and the VorpalFusion apps in ComfyUI's Apps panel. The page is served by
// this ComfyUI under /vorpalfusion/.
//
// An app is a workflow marked `extra.vorpalfusion = {template}` (the pack writes them into the user's workflows
// folder). When one is the active workflow, App Mode's center panel shows the page on that template instead of
// App Mode's empty-app welcome. The official `afterLoadGraph` hook runs on every workflow load; a DOM observer
// follows App Mode's panel as it comes and goes (switching between App Mode and the graph does not load a graph).
// Where App Mode is absent, opening an app opens the sidebar tab on its template instead.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const page = (template) => api.fileURL("/vorpalfusion/") + (template ? `?template=${encodeURIComponent(template)}` : "");
const open = () => window.open(page(), "_blank", "noopener");
const marker = () => app.rootGraph?.extra?.vorpalfusion;  // the active workflow's, read live
const centerPanel = () => document.querySelector('[data-testid="linear-center-panel"]');
let sidebarTemplate = null;

function frame(template, css) {
  const el = document.createElement("iframe");
  el.src = page(template); el.title = "VorpalFusion"; el.allow = "clipboard-write"; el.dataset.template = template || "";
  el.style.cssText = `border:0;width:100%;${css}`;
  return el;
}

function link(template) {
  const a = document.createElement("a");
  a.href = page(template); a.target = "_blank"; a.rel = "noopener"; a.textContent = "Open in its own tab ↗";
  a.style.cssText = "padding:6px 10px;font-size:12px;opacity:.8;color:inherit;text-decoration:none";
  return a;
}

// App Mode's center panel holds the page while a VorpalFusion app is the active workflow; anything else, it is removed.
function place() {
  const panel = centerPanel(), mine = document.getElementById("vorpalfusion-app");
  const wanted = marker() ? marker().template || "" : null;
  if (mine && (wanted === null || mine.parentElement !== panel || mine.dataset.template !== wanted)) mine.remove();
  if (wanted === null || !panel || document.getElementById("vorpalfusion-app")) return;
  const view = document.createElement("div");
  view.id = "vorpalfusion-app"; view.dataset.template = wanted;
  // below App Mode's own top-left controls (App / graph, Build an app), so the way back stays in reach
  view.style.cssText = "position:absolute;inset:52px 0 0 0;z-index:5;display:flex;flex-direction:column;background:inherit";
  const bar = link(wanted); bar.style.cssText += ";position:absolute;top:-34px;right:8px";
  view.replaceChildren(bar, frame(wanted, "flex:1;height:100%"));
  panel.append(view);
}

function openSidebar(template) {
  sidebarTemplate = template;
  const tabs = app.extensionManager.sidebarTab;
  if (tabs.activeSidebarTabId !== "vorpalfusion") return tabs.toggleSidebarTab("vorpalfusion");
  const current = document.querySelector('iframe[title="VorpalFusion"][data-sidebar]');
  if (current && current.dataset.template !== (template || "")) {
    current.src = page(template); current.dataset.template = template || "";
  }
}

app.registerExtension({
  name: "VorpalFusion",
  commands: [{ id: "VorpalFusion.Open", label: "Open VorpalFusion", icon: "pi pi-sparkles", function: open }],
  menuCommands: [{ path: ["Extensions"], commands: ["VorpalFusion.Open"] }],
  actionBarButtons: [{ icon: "pi pi-sparkles", label: "VorpalFusion", tooltip: "Open VorpalFusion in its own tab", onClick: open }],
  async afterLoadGraph() {
    place();
    const opened = marker();
    // no App Mode to show it in (an older frontend, or the app saved in graph mode): the sidebar tab, on its template
    if (opened) setTimeout(() => { if (marker() === opened && !centerPanel()) openSidebar(opened.template || null); }, 600);
  },
  setup() {
    let queued = false;
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; place(); });
    }).observe(document.body, { childList: true, subtree: true });
    app.extensionManager.registerSidebarTab({
      id: "vorpalfusion",
      icon: "pi pi-sparkles",
      title: "VorpalFusion",
      tooltip: "VorpalFusion: the picture develops while you type",
      type: "custom",
      render(el) {
        el.style.cssText = "display:flex;flex-direction:column;height:100%";
        const view = frame(sidebarTemplate, "flex:1;min-height:480px");
        view.dataset.sidebar = "";
        el.replaceChildren(link(sidebarTemplate), view);
      },
    });
  },
});
