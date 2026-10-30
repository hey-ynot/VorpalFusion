// VorpalFusion's entries in ComfyUI: a sidebar tab with the live page in it, a top-bar button and a menu command
// that open the page in its own browser tab. The page is served by this ComfyUI under /vorpalfusion/.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const page = () => api.fileURL("/vorpalfusion/");
const open = () => window.open(page(), "_blank", "noopener");

app.registerExtension({
  name: "VorpalFusion",
  commands: [{ id: "VorpalFusion.Open", label: "Open VorpalFusion", icon: "pi pi-sparkles", function: open }],
  menuCommands: [{ path: ["Extensions"], commands: ["VorpalFusion.Open"] }],
  actionBarButtons: [{ icon: "pi pi-sparkles", label: "VorpalFusion", tooltip: "Open VorpalFusion in its own tab", onClick: open }],
  setup() {
    app.extensionManager.registerSidebarTab({
      id: "vorpalfusion",
      icon: "pi pi-sparkles",
      title: "VorpalFusion",
      tooltip: "VorpalFusion: the picture develops while you type",
      type: "custom",
      render(el) {
        el.style.cssText = "display:flex;flex-direction:column;height:100%";
        const bar = document.createElement("a");
        bar.href = page(); bar.target = "_blank"; bar.rel = "noopener";
        bar.textContent = "Open in its own tab ↗";
        bar.style.cssText = "padding:6px 10px;font-size:12px;opacity:.8;color:inherit;text-decoration:none";
        const frame = document.createElement("iframe");
        frame.src = page(); frame.title = "VorpalFusion"; frame.allow = "clipboard-write";
        frame.style.cssText = "flex:1;width:100%;border:0;min-height:480px";
        el.replaceChildren(bar, frame);
      },
    });
  },
});
