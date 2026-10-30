"""The extension inside a ComfyUI checkout, on the CPU (no GPU, no models): ComfyUI loads this folder from a throwaway
`custom_nodes/`, and its nodes register, the web extension is served, the page, the template list, the frame stream
and the in-process v2 API answer under /vorpalfusion/, and the user's workflows hold the VorpalFusion app. Before
that, in this process: which apps are offered (a family's app only where its model files are, another precision
counting), and how they are written (every user, never over a file that is there, never again once written).

    <ComfyUI's python> checks/comfyui.py <ComfyUI folder> [--port 8874]

ComfyUI's python needs this folder's requirements.txt installed.
"""

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def get(url: str):
    with urllib.request.urlopen(url, timeout=30) as reply:  # follows the prefix's redirect
        kind = reply.headers.get("content-type", "")
        return reply.status, kind, reply.geturl(), (b"" if "event-stream" in kind else reply.read()), dict(reply.headers)


def check_apps(comfyui: str, home: Path) -> str:
    sys.path.insert(0, comfyui)
    sys.argv = ["main.py", "--cpu"]
    import comfy.options

    comfy.options.enable_args_parsing()
    import folder_paths

    folder_paths.set_user_directory(str(home / "user"))
    for folder, name in (("diffusion_models", "z_image_turbo_int8_convrot.safetensors"), ("text_encoders", "qwen_3_4b.safetensors"),
                         ("vae", "ae.safetensors")):  # Z-Image Turbo's files, its model in another precision; empty
        (home / "models" / folder).mkdir(parents=True, exist_ok=True)
        (home / "models" / folder / name).touch()
        folder_paths.add_model_folder_path(folder, str(home / "models" / folder), is_default=True)
    spec = importlib.util.spec_from_file_location("vorpalfusion_pack", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    pack = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pack)
    live = pack.app_modules()
    apps = pack.ready_apps(live.TUNED, sys.modules["vorpalfusion_vf_templates"].same_model)
    templates = {template for template, _ in apps.values()}
    assert templates == {None, "image_z_image_turbo"}, apps  # the plain app, and the one family whose files are here

    workflows = {user: home / "user" / user / "workflows" for user in ("default", "alice")}
    workflows["alice"].mkdir(parents=True)
    (workflows["alice"] / "VorpalFusion.app.json").write_text('{"mine": true}')  # a user's own file of that name
    written = pack.write_apps(["default", "alice", "__system"], apps)
    assert len(written) == 2 * len(apps) - 1, written  # every app for every user, alice's own file kept, no system user
    assert json.loads((workflows["alice"] / "VorpalFusion.app.json").read_text()) == {"mine": True}
    for path in workflows["default"].glob("*.app.json"):
        flow = json.loads(path.read_text())
        assert flow["extra"]["linearMode"] is True and flow["nodes"][0]["type"] == "MarkdownNote", path
        assert flow["extra"]["vorpalfusion"]["template"] in templates, path
    gone = next(workflows["default"].glob("VorpalFusion - Z-Image*.app.json"))
    gone.unlink()  # the user deletes an app: it stays deleted
    assert pack.write_apps(["default", "alice"], apps) == [] and not gone.exists()
    return f"apps {sorted(apps)} written for two users, a user's own file kept, a deleted app not written again"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("comfyui")
    parser.add_argument("--port", type=int, default=8874)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    with tempfile.TemporaryDirectory(prefix="vf-apps-") as scratch:
        print("ok:", check_apps(args.comfyui, Path(scratch)))
    with tempfile.TemporaryDirectory(prefix="vf-comfyui-") as home:
        (Path(home) / "custom_nodes").mkdir()
        (Path(home) / "custom_nodes" / "VorpalFusion").symlink_to(ROOT, target_is_directory=True)
        log = open(Path(home) / "comfyui.log", "w")
        server = subprocess.Popen([sys.executable, "main.py", "--cpu", "--disable-assets", "--base-directory", home,
                                   "--port", str(args.port)], cwd=args.comfyui, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(240):
                try:
                    get(base + "/")
                    break
                except OSError:
                    assert server.poll() is None, (Path(home) / "comfyui.log").read_text()[-3000:]
                    time.sleep(0.5)
            assert "GVLatentStore" in json.loads(get(base + "/object_info/GVLatentStore")[3]), "the nodes did not register"
            script = "/extensions/VorpalFusion/vorpalfusion.js"
            assert script in json.loads(get(base + "/extensions")[3]), "the web extension is not listed"
            assert b"registerSidebarTab" in get(base + script)[3]
            status, kind, url, body, _ = get(base + "/vorpalfusion")
            assert url.endswith("/vorpalfusion/") and "text/html" in kind and b"VorpalFusion" in body, (status, kind, url)
            status, kind, _, body, headers = get(base + "/vorpalfusion/templates")
            assert isinstance(json.loads(body), list) and not any(h.lower().startswith("access-control-") for h in headers)
            assert json.loads(get(base + "/vorpalfusion/api/v2/health")[3])["status"] == "healthy"
            assert get(base + "/vorpalfusion/frames")[1] == "text/event-stream"
            listed = json.loads(get(base + "/api/userdata?dir=workflows")[3])
            assert "VorpalFusion.app.json" in listed, listed  # what the Apps panel lists, through the user-data API
            print(f"ok: ComfyUI loaded the pack; nodes, web extension, page, {len(json.loads(body))} templates, "
                  f"frame stream and v2 API under /vorpalfusion/; the user's workflows: {listed}")
        finally:
            server.terminate()
            server.wait(20)


if __name__ == "__main__":
    main()
