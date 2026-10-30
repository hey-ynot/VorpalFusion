"""The template source without a GPU: every fixture record reads into a live profile through the vendored slot
readers, and the built-in list keeps the measured families only."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live  # noqa: E402  (beside this folder)
from profile import Profile  # noqa: E402

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


def test_families():
    assert live.FAMILIES.match("image_flux3_klein_distilled_t2i") and live.FAMILIES.match("video_ltx2_5_flf2v")
    assert not live.FAMILIES.match("api_bfl_flux3_t2i") and not live.FAMILIES.match("image_z_image_turbo_fun_union_controlnet")
    assert not live.FAMILIES.match("image_qwen_image_2_1_image_edit")
