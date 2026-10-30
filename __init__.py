"""VorpalFusion inside ComfyUI: this folder is a custom-node pack as well as the standalone app.

Cloned (or installed by ComfyUI Manager or `comfy node install`) into `custom_nodes/`, it serves the live page on
ComfyUI's own server at `/vorpalfusion/`, and adds a VorpalFusion tab to ComfyUI's sidebar and a button to its top
bar that open it. Nothing to configure and no second service: the endpoint is the ComfyUI it runs in, Keep saves
into ComfyUI's output folder (`output/vorpalfusion/`), and the templates are ComfyUI's own.

- The page and the sidecar (`live.py`, unchanged) run on ComfyUI's event loop: one aiohttp route under the prefix
  hands each request to the sidecar's ASGI app, so the page is reached exactly as ComfyUI is, remotely included
  (it uses relative URLs only).
- The v2 API that comfy-sdk speaks is comfy-api-proxy's aiohttp app, mounted in-process under the same prefix
  (`/vorpalfusion/api/v2/…`) with its preview throttle off, against this ComfyUI over loopback.
- The node pack (`nodes/`) registers as before: `GVLatentStore` / `GVLatentRecall` and the kept step previewer.
- ComfyUI's Apps panel lists VorpalFusion: on load the pack writes app workflows (`*.app.json`, App Mode, marked
  `extra.vorpalfusion`) into each ComfyUI user's `workflows/` folder, once each and never over a file that is there;
  the web extension opens VorpalFusion on the app's template when one is opened.

If comfy-sdk or comfy-api-proxy is missing (`pip install -r requirements.txt` in ComfyUI's Python), the nodes still
load and the log says what to install.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

WEB_DIRECTORY = "./web"
PREFIX = "/vorpalfusion"
HERE = Path(__file__).resolve().parent
log = logging.getLogger("vorpalfusion")


def _load(name: str, path: Path | None = None):
    """A file of this folder as module `vorpalfusion_<name>`, by path: no relative import, so this file also loads on
    its own (pytest imports it as a plain module when it collects `checks/`)."""
    spec = importlib.util.spec_from_file_location(f"vorpalfusion_{name}", path or HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_nodes = _load("nodes", HERE / "nodes" / "__init__.py")
NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS = _nodes.NODE_CLASS_MAPPINGS, _nodes.NODE_DISPLAY_NAME_MAPPINGS


def app_modules():
    """`live` and the modules beside it, which import each other by bare name (`profile`, `vf_templates`). They load
    under their own names (`vorpalfusion_live`, …); the bare names exist only while they load, so the standard
    library's `profile` stays what the rest of ComfyUI imports."""
    bare = ("profile", "vf_templates")
    held = {name: sys.modules.pop(name) for name in bare if name in sys.modules}
    try:
        sys.modules["vf_templates"] = templates = _load("vf_templates")
        sys.modules["profile"] = _load("profile")
        live = _load("live")
    finally:
        for name in bare:
            sys.modules.pop(name, None)
        sys.modules.update(held)
    if shutil.which(templates.COMFY_CLI) is None:  # comfy-cli installed into ComfyUI's Python, which need not be on PATH
        for name in ("comfy", "comfy.exe"):
            if (found := Path(sys.executable).parent / name).is_file():
                templates.COMFY_CLI = str(found)
    return live


def endpoint() -> str:
    """This ComfyUI, as a loopback URL from inside its own process."""
    from comfy.cli_args import args

    host = (args.listen or "127.0.0.1").split(",")[0].strip()
    host = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    # ponytail: plain http; a ComfyUI started with --tls-keyfile needs a certificate httpx trusts for this loopback URL
    scheme = "https" if args.tls_keyfile and args.tls_certfile else "http"
    return f"{scheme}://{f'[{host}]' if ':' in host else host}:{args.port}"


def asgi_handler(asgi, state, prefix: str = PREFIX):
    """An aiohttp handler that runs one ASGI app (the sidecar's), with `scope["app_state"] = state()` and the path
    below `prefix`. A reply that streams (the frame stream) streams; any other reply is sent whole. The sidecar's
    CORS headers are dropped: it is same-origin here, and ComfyUI's own middleware decides who may call it."""
    from aiohttp import web

    async def handle(request: web.Request) -> web.StreamResponse:
        body = await request.read()
        scope = {"type": "http", "method": request.method, "path": request.path[len(prefix):] or "/",
                 "query_string": request.query_string.encode(), "app_state": state(),
                 "headers": [(k.lower().encode(), v.encode()) for k, v in request.headers.items()]}
        sent: dict = {"chunks": []}

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def stream() -> web.StreamResponse:
            if "stream" not in sent:
                sent["stream"] = web.StreamResponse(status=sent["status"], headers=sent["headers"])
                await sent["stream"].prepare(request)
            return sent["stream"]

        async def send(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]
                sent["headers"] = [(k.decode(), v.decode()) for k, v in message.get("headers", [])
                                   if not k.lower().startswith(b"access-control-")]
                if (b"content-type", b"text/event-stream") in message.get("headers", []):
                    await stream()  # the page's EventSource opens on the headers, before the first event
            elif "stream" in sent or message.get("more_body"):
                await (await stream()).write(message.get("body", b""))
            else:
                sent["chunks"].append(message.get("body", b""))

        try:
            await asgi(scope, receive, send)
        except ConnectionResetError:  # the page went away mid-stream
            if "stream" not in sent:
                raise
        if "stream" in sent:
            return sent["stream"]
        return web.Response(status=sent.get("status", 500), headers=sent.get("headers", []), body=b"".join(sent["chunks"]))

    return handle


def app_workflow(template: str | None, title: str) -> dict:
    """A minimal App Mode workflow (`extra.linearMode`) that ComfyUI's Apps panel lists by its `.app.json` name; the
    web extension opens VorpalFusion on `extra.vorpalfusion.template` (none: the page's own first choice). Its one
    note says what it is, for a ComfyUI without the pack."""
    note = (f"# {'VorpalFusion · ' + title if template else 'VorpalFusion'}\n\nOpening this app opens VorpalFusion"
            f"{f' on `{template}`' if template else ''}: the picture develops while you type. If nothing opens, "
            "the VorpalFusion custom-node pack is not loaded in this ComfyUI.")
    return {"id": str(uuid.uuid4()), "revision": 0, "last_node_id": 1, "last_link_id": 0,
            "nodes": [{"id": 1, "type": "MarkdownNote", "pos": [0, 0], "size": [480, 180], "flags": {}, "order": 0,
                       "mode": 0, "inputs": [], "outputs": [], "properties": {}, "widgets_values": [note]}],
            "links": [], "groups": [], "config": {}, "version": 0.4,
            "extra": {"linearMode": True, "vorpalfusion": {"template": template}}}


def ready_apps(tuned: re.Pattern, same_model) -> dict[str, tuple[str | None, str]]:
    """The apps to offer, `{file name: (template, title)}`: `VorpalFusion.app.json`, and one per tuned family (image
    and clip apart) whose template this ComfyUI can run with the model files it has: a template of ComfyUI's own
    index that needs no picture (no `io.inputs`), every model file its graph lists present (another precision counts)."""
    import folder_paths
    from app.frontend_management import FrontendManager

    apps: dict[str, tuple[str | None, str]] = {"VorpalFusion.app.json": (None, "VorpalFusion")}
    assets = FrontendManager.template_asset_map() or {}
    if "index.json" not in assets:
        return apps

    def models(node):  # the {name, directory} entries a template's nodes list, subgraphs included
        if isinstance(node, dict):
            if isinstance(node.get("models"), list):
                yield from (m for m in node["models"] if isinstance(m, dict))
            for value in node.values():
                yield from models(value)
        elif isinstance(node, list):
            for value in node:
                yield from models(value)

    def ready(name: str) -> bool:
        listed = list(models(json.loads(Path(assets[f"{name}.json"]).read_text())))
        for model in listed:
            have = folder_paths.get_filename_list(model.get("directory", ""))
            if model.get("name") not in have and not same_model(model.get("name", ""), have):
                return False
        return bool(listed)

    families: dict[tuple, list[dict]] = {}
    for entry in (t for category in json.loads(Path(assets["index.json"]).read_text()) for t in category.get("templates") or []):
        if (found := tuned.match(entry.get("name", ""))) and not (entry.get("io") or {}).get("inputs") and f"{entry['name']}.json" in assets:
            families.setdefault(found.groups(), []).append(entry)
    for entries in families.values():
        # ponytail: a family's distilled or turbo template first (the fast one live wants), else ComfyUI's order
        entries.sort(key=lambda entry: not re.search(r"distilled|turbo", entry["name"]))
        if entry := next((e for e in entries if ready(e["name"])), None):
            title = entry.get("title") or entry["name"]
            # the family's name ("Z-Image-Turbo: Text to Image" → "Z-Image-Turbo"), short enough for the Apps panel
            short = title.split(":")[0].strip() + (" clips" if entry.get("mediaType") == "video" else "")
            safe = re.sub(r'\s*[<>:"/\\|?*]+\s*', " - ", short).strip(" -")  # a file name on every OS
            apps[f"VorpalFusion - {safe}.app.json"] = (entry["name"], title)
    return apps


def write_apps(users, apps: dict[str, tuple[str | None, str]]) -> list[str]:
    """Each app into each user's `workflows/` folder (`user/<user>/workflows/`, the folder the Apps panel lists),
    once: a name written before (kept in `user/<user>/vorpalfusion/apps.json`) is not written again, so an app the
    user deleted stays deleted, and a file already there is never replaced. Returns the paths written."""
    import folder_paths

    written_now = []
    for user in users:
        root = folder_paths.get_public_user_directory(user)
        if root is None:
            continue
        ledger = Path(root) / "vorpalfusion" / "apps.json"
        written = set(json.loads(ledger.read_text())) if ledger.is_file() else set()
        for name, (template, title) in apps.items():
            path = Path(root) / "workflows" / name
            if name not in written and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(app_workflow(template, title), indent=1))
                written_now.append(str(path))
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(sorted(written | set(apps)), indent=1))
    return written_now


def mount(server) -> None:
    """Mount the page, the sidecar and the v2 API under PREFIX on ComfyUI's server, the sidecar's loop started with it."""
    import comfy_api_proxy.app as proxy
    import comfy_api_proxy.realtime as realtime
    import folder_paths
    from aiohttp import web

    live = app_modules()
    base = endpoint()
    try:  # every ComfyUI user (one, `default`, unless --multi-user) gets the apps it has not had
        apps = ready_apps(live.TUNED, sys.modules["vorpalfusion_vf_templates"].same_model)
        if written := write_apps(server.user_manager.users, apps):
            log.info("VorpalFusion: %d app(s) for ComfyUI's Apps panel: %s", len(written), ", ".join(written))
    except Exception as error:  # noqa: BLE001  (the apps are a convenience: the page works without them)
        log.warning("VorpalFusion: could not write its apps for ComfyUI's Apps panel: %s", error)
    realtime._THROTTLE_SECONDS = 0  # every step preview reaches the page (the proxy sends at most two a second otherwise)
    proxy._external_base = lambda request: f"{request.scheme}://{request.host}{PREFIX}"  # its job and asset links, under the mount
    sub = proxy.make_app(base)
    held: dict = {}
    # httpx logs every request at INFO, and ComfyUI's console shows INFO: the loop's own loopback calls (several per
    # frame) stay out of it.
    logging.getLogger("httpx").addFilter(lambda record: f"{base}/" not in record.getMessage())

    async def start(_app) -> None:
        from comfy_sdk import AsyncComfy

        prior = os.environ.get("COMFY_BASE_URL")
        os.environ["COMFY_BASE_URL"] = base + PREFIX  # comfy-sdk reads its endpoint when a client is made
        try:
            client = AsyncComfy(client_info="vorpalfusion-comfyui")
        finally:
            if prior is None:
                os.environ.pop("COMFY_BASE_URL", None)
            else:
                os.environ["COMFY_BASE_URL"] = prior
        held["live"] = live.Live(client, base, None, keep=os.path.join(folder_paths.get_output_directory(), "vorpalfusion"))
        held["task"] = asyncio.create_task(held["live"].run())
        log.info("VorpalFusion: open %s/ on this ComfyUI (or its sidebar tab)", PREFIX)

    async def stop(_app) -> None:
        if task := held.get("task"):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    sub.on_startup.append(start)
    sub.on_cleanup.append(stop)
    sub.router.add_route("*", "/{tail:.*}", asgi_handler(live.app, lambda: held["live"]))

    async def slash(_request):
        raise web.HTTPFound(PREFIX + "/")  # the page's relative URLs resolve under the prefix

    server.app.router.add_get(PREFIX, slash)
    server.app.add_subapp(PREFIX, sub)


try:
    from server import PromptServer  # ComfyUI's; absent when this file is imported outside ComfyUI
except ImportError:
    PromptServer = None
if PromptServer is not None and getattr(PromptServer, "instance", None) is not None:
    try:
        mount(PromptServer.instance)
    except ImportError as error:
        log.warning("VorpalFusion: the page is off, %s is missing. In ComfyUI's Python: pip install -r %s",
                    error.name, HERE / "requirements.txt")
