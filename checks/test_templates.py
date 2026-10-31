"""The template source without a GPU: every fixture record reads into a live profile through the vendored slot
readers, the tuned list names the measured families, and a template qualifies by what its record and the endpoint
have."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live  # noqa: E402  (beside this folder)
from profile import Profile, thinned  # noqa: E402
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
    file, and a file the endpoint has in another precision (a swap); a picture loader's file is the creator's, and a
    choice that is not a model file (a crop position) is the node's own to check."""
    graph = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2_turbo_bf16.safetensors"}},
             "2": {"class_type": "VAELoader", "inputs": {"vae_name": "absent.safetensors"}},
             "3": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
             "4": {"class_type": "CustomNode", "inputs": {}}, "5": {"class_type": "PartnerImageNode", "inputs": {}},
             "6": {"class_type": "KSampler", "inputs": {"sampler_name": "euler", "model": ["1", 0]}},
             "7": {"class_type": "ImageCrop", "inputs": {"position": "center"}}}
    info = {"UNETLoader": {"input": {"required": {"unet_name": [["krea2_turbo_fp8_scaled.safetensors"]]}}},
            "VAELoader": {"input": {"required": {"vae_name": [["ae.safetensors"]]}}},
            "LoadImage": {"input": {"required": {"image": [["other.png"]]}}}, "PartnerImageNode": {"api_node": True},
            "KSampler": {"input": {"required": {"sampler_name": ["COMBO", {"options": ["euler"]}]}}},
            "ImageCrop": {"input": {"required": {"position": ["COMBO", {"options": ["top", "bottom"]}]}}}}
    missing, swaps = gaps(graph, info)
    assert missing == ["CustomNode", "PartnerImageNode (a partner API node)", "absent.safetensors"], missing
    assert swaps == {"krea2_turbo_bf16.safetensors": "krea2_turbo_fp8_scaled.safetensors"}, swaps


def test_a_clip_attends_with_int8_unless_its_template_picks_its_own():
    """Draft and settled clips get the attention backend in front of every guider; Final and a template with its own
    attention node (FastH3) keep theirs. A hand-written schedule thins for a draft only."""
    state = {"prompt": "a fox", "seed": 1, "sources": []}
    h3 = Profile(FIXTURES["records"]["video_minimax_h3_t2v"])
    h3.attention = True
    for settle in (False, True):
        graph, _ = h3.build(state, settle=settle)
        guiders = [n for n in graph.values() if "Guider" in n["class_type"]]
        assert guiders and all(graph[g["inputs"]["model"][0]]["class_type"] == "ModelAttentionBackend" for g in guiders)
    assert not any(n["class_type"] == "ModelAttentionBackend" for n in h3.build(state, settle=True, rung="final")[0].values())
    fast = Profile(FIXTURES["records"]["video_fastvideo_fasth3_t2v"])
    fast.attention = True
    assert sum(n["class_type"] == "ModelAttentionBackend" for n in fast.build(state, settle=False)[0].values()) == 1
    assert thinned("1.0, 0.972973, 0.923077, 0.8, 0.571429, 0.333333, 0.0") == "1.0, 0.972973, 0.8, 0.333333, 0.0"
    assert thinned("1.0, 0.5, 0.0") == "1.0, 0.5, 0.0"
    ltx = Profile(FIXTURES["records"]["video_ltx2_5_t2v"])  # not in the manifest's thin_drafts: its own knots
    knots = [n["inputs"]["sigmas"] for n in ltx.wf.values() if n["class_type"] == "ManualSigmas"]
    assert knots and [n["inputs"]["sigmas"] for n in ltx.build(state, settle=False)[0].values() if n["class_type"] == "ManualSigmas"] == knots
    ltx.thin = True
    assert [n["inputs"]["sigmas"] for n in ltx.build(state, settle=False)[0].values() if n["class_type"] == "ManualSigmas"] == [thinned(k) for k in knots]
