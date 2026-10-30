"""The extension inside a ComfyUI checkout, on the CPU (no GPU, no models): ComfyUI loads this folder from a throwaway
`custom_nodes/`, and its nodes register, the web extension is served, and the page, the template list, the frame
stream and the in-process v2 API answer under /vorpalfusion/.

    <ComfyUI's python> checks/comfyui.py <ComfyUI folder> [--port 8874]

ComfyUI's python needs this folder's requirements.txt installed.
"""

import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("comfyui")
    parser.add_argument("--port", type=int, default=8874)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
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
            print(f"ok: ComfyUI loaded the pack; nodes, web extension, page, {len(json.loads(body))} templates, "
                  f"frame stream and v2 API under /vorpalfusion/")
        finally:
            server.terminate()
            server.wait(20)


if __name__ == "__main__":
    main()
