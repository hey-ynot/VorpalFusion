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

If comfy-sdk or comfy-api-proxy is missing (`pip install -r requirements.txt` in ComfyUI's Python), the nodes still
load and the log says what to install.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import os
import shutil
import sys
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


def mount(server) -> None:
    """Mount the page, the sidecar and the v2 API under PREFIX on ComfyUI's server, the sidecar's loop started with it."""
    import comfy_api_proxy.app as proxy
    import comfy_api_proxy.realtime as realtime
    import folder_paths
    from aiohttp import web

    live = app_modules()
    base = endpoint()
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
