"""The ComfyUI extension (`__init__.py`) outside ComfyUI: the app's modules load without taking the standard library's
`profile`, and the aiohttp bridge serves the sidecar's ASGI app under the prefix (a page whole, without CORS headers;
the frame stream open before its first event). Needs aiohttp, which ComfyUI ships; skipped without it.

    uv run --with pytest --with aiohttp pytest -q checks/test_extension.py
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def pack():
    spec = importlib.util.spec_from_file_location("vorpalfusion_pack", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_modules_load_beside_the_standard_profile():
    before = sys.modules.get("profile"), sys.modules.get("vf_templates")
    live = pack().app_modules()
    assert (sys.modules.get("profile"), sys.modules.get("vf_templates")) == before  # whatever ComfyUI had stays
    assert live.Profile.__module__ == "vorpalfusion_profile" and callable(live.app)


def test_bridge_serves_the_sidecar_under_the_prefix():
    extension = pack()
    live = extension.app_modules()
    state = SimpleNamespace(listeners=set(), halt=lambda: None)

    async def run():
        app = web.Application()
        app.router.add_route("*", "/vorpalfusion/{tail:.*}", extension.asgi_handler(live.app, lambda: state))
        async with TestClient(TestServer(app)) as client:
            page = await client.get("/vorpalfusion/")
            assert page.status == 200 and "text/html" in page.headers["content-type"] and b"VorpalFusion" in await page.read()
            assert not [h for h in page.headers if h.lower().startswith("access-control-")]
            assert (await client.post("/vorpalfusion/cancel", data=b"{}")).status == 204
            assert (await client.get("/vorpalfusion/nothing-here")).status == 404
            stream = await asyncio.wait_for(client.get("/vorpalfusion/frames"), 5)  # headers before any event
            assert stream.status == 200 and stream.headers["content-type"] == "text/event-stream"
            next(iter(state.listeners)).put_nowait({"kind": "phase", "phase": "idle"})
            line = await asyncio.wait_for(stream.content.readline(), 5)
            assert json.loads(line.decode()[len("data: "):]) == {"kind": "phase", "phase": "idle"}
            stream.close()

    asyncio.run(run())
