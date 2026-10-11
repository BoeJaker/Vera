from pathlib import Path

import pytest

from vera.inventory.compatibility_consumer_inventory import (
    collect_compatibility_consumer_inventory,
)
from vera.inventory.compatibility_path_review import (
    attach_declared_consumer_inventory,
    build_compatibility_path_review_set,
)


pytestmark = pytest.mark.critical
SYMBOLS = (
    "memory.patch_capability_for_memory",
    "memory.patch_new_cap",
    "memory.record_cap_interaction",
)


def test_inventory_is_bounded_content_free_and_deterministic(tmp_path: Path):
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "adapter.py").write_text(
        "from vera.fabric.memory_hooks import patch_new_cap\npatch_new_cap('x')\n",
        encoding="utf-8",
    )
    (consumer / "unrelated.py").write_text("patch_new_capability = 1\n", encoding="utf-8")

    first = collect_compatibility_consumer_inventory({"consumer-a": consumer}, SYMBOLS)
    second = collect_compatibility_consumer_inventory({"consumer-a": consumer}, reversed(SYMBOLS))

    assert first == second
    root = first["declared_roots"][0]
    hits = root["matches"]["memory.patch_new_cap"]
    assert hits == [{
        "relative_path": "adapter.py",
        "line_numbers": [1, 2],
        "content_digest": hits[0]["content_digest"],
    }]
    assert "patch_new_cap('x')" not in str(first)
    assert first["global_external_coverage"] is False
    assert first["removal_authority"] is False
    assert first["executes"] is False
    assert first["imports"] is False
    assert first["mutates"] is False


def test_inventory_keeps_each_root_and_symbol_independent(tmp_path: Path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "config.toml").write_text(
        'hook = "memory.record_cap_interaction"\n', encoding="utf-8")
    (right / "notes.md").write_text(
        "Use patch_capability_for_memory during startup.\n", encoding="utf-8")

    result = collect_compatibility_consumer_inventory(
        {"right": right, "left": left}, SYMBOLS)

    roots = {item["label"]: item for item in result["declared_roots"]}
    assert roots["left"]["matches"]["memory.record_cap_interaction"][0][
        "relative_path"] == "config.toml"
    assert roots["right"]["matches"]["memory.patch_capability_for_memory"][0][
        "relative_path"] == "notes.md"
    assert roots["left"]["matches"]["memory.patch_capability_for_memory"] == []


def test_inventory_fails_closed_on_missing_roots_and_bounds(tmp_path: Path):
    with pytest.raises(ValueError, match="at least one"):
        collect_compatibility_consumer_inventory({}, SYMBOLS)
    with pytest.raises(ValueError, match="unavailable"):
        collect_compatibility_consumer_inventory({"missing": tmp_path / "missing"}, SYMBOLS)
    root = tmp_path / "root"
    root.mkdir()
    (root / "one.py").write_text("patch_new_cap()", encoding="utf-8")
    (root / "two.py").write_text("patch_new_cap()", encoding="utf-8")
    with pytest.raises(ValueError, match="bounds exceeded"):
        collect_compatibility_consumer_inventory({"root": root}, SYMBOLS, max_files=1)


def test_inventory_does_not_follow_file_symlinks_outside_declared_root(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("patch_new_cap()", encoding="utf-8")
    link = root / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks unavailable on this platform")

    result = collect_compatibility_consumer_inventory({"root": root}, SYMBOLS)

    scanned = result["declared_roots"][0]
    assert scanned["skipped_symlinks"] == 1
    assert scanned["matches"]["memory.patch_new_cap"] == []


def test_declared_inventory_enriches_reviews_without_authorising_removal(tmp_path: Path):
    root = tmp_path / "external"
    root.mkdir()
    (root / "consumer.py").write_text(
        "from vera.fabric.memory_hooks import record_cap_interaction\n",
        encoding="utf-8",
    )
    inventory = collect_compatibility_consumer_inventory({"external-a": root}, SYMBOLS)

    result = attach_declared_consumer_inventory(
        build_compatibility_path_review_set(Path(__file__).resolve().parents[1]),
        inventory,
    )

    by_name = {item["name"]: item for item in result["reviews"]}
    evidence = by_name["memory.record_cap_interaction"]["external_consumer_evidence"]
    assert evidence["match_count"] == 1
    assert evidence["matches"][0]["root"] == "external-a"
    assert evidence["global_external_coverage"] is False
    assert by_name["memory.patch_new_cap"]["coverage"]["external_consumers"] == (
        "declared_roots_examined")
    assert result["removal_candidates"] == []
    assert result["bulk_removal_authority"] is False
