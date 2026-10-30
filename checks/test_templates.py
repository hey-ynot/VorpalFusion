"""The template source without a GPU: every fixture record reads into a live profile through the vendored slot
readers, the tuned list names the measured families, and a template qualifies by what its record and the endpoint
have."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live  # noqa: E402  (beside this folder)
from profile import Profile  # noqa: E402
from vf_templates import gaps  # noqa: E402

FIXTURES = json.loads((Path(__file__).parent / "templates.json").read_text())


def test_every_creation_template_has_a_live_profile():
    for row in FIXTURES["templates"]:
        if row["role"] != "creation":
            continue
        profile = Profile(FIXTURES["records"][row["name"]])
        assert profile.prompt and profile.seed, row["name"]
        assert profile.kind == row["kind"], row["name"]
        assert profile.media, row["name"]


def test_listing_groups_and_filters():
    rows = live.listing(FIXTURES["templates"])
    assert {row["group"] for row in rows} == {"Image", "Edit", "Clip"}
    assert all(not row["name"].startswith("utility_") for row in rows)


def test_tuned():
    assert live.TUNED.match("image_flux3_klein_distilled_t2i") and live.TUNED.match("video_ltx2_5_flf2v")
    assert not live.TUNED.match("api_bfl_flux3_t2i") and not live.TUNED.match("image_z_image_turbo_fun_union_controlnet")
    assert not live.TUNED.match("image_qwen_image_2_1_image_edit")


def test_every_creation_template_starts_its_sampler_from_nothing_or_a_picture():
    """A text-to-X template's sampler starts from a made latent, an image-to-video one from its picture; an edit one
    from either (a style reference rides the conditioning)."""
    for row in FIXTURES["templates"]:
        start = Profile(FIXTURES["records"][row["name"]]).start()
        if row["role"] == "creation":
            assert start == {"make": "empty", "animate": "source"}.get(row["job"], start) and start, row["name"]
    assert Profile(FIXTURES["records"]["utility_video_frame_interpolation"]).start() is None


def test_gaps():
    """Every node class and choice the graph names, against object_info: a lacking class, a partner node, a missing
    file, and a file the endpoint has in another precision (a swap); a picture loader's file is the creator's."""
    graph = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2_turbo_bf16.safetensors"}},
             "2": {"class_type": "VAELoader", "inputs": {"vae_name": "absent.safetensors"}},
             "3": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
             "4": {"class_type": "CustomNode", "inputs": {}}, "5": {"class_type": "PartnerImageNode", "inputs": {}},
             "6": {"class_type": "KSampler", "inputs": {"sampler_name": "euler", "model": ["1", 0]}}}
    info = {"UNETLoader": {"input": {"required": {"unet_name": [["krea2_turbo_fp8_scaled.safetensors"]]}}},
            "VAELoader": {"input": {"required": {"vae_name": [["ae.safetensors"]]}}},
            "LoadImage": {"input": {"required": {"image": [["other.png"]]}}}, "PartnerImageNode": {"api_node": True},
            "KSampler": {"input": {"required": {"sampler_name": ["COMBO", {"options": ["euler"]}]}}}}
    missing, swaps = gaps(graph, info)
    assert missing == ["CustomNode", "PartnerImageNode (a partner API node)", "absent.safetensors"], missing
    assert swaps == {"krea2_turbo_bf16.safetensors": "krea2_turbo_fp8_scaled.safetensors"}, swaps
