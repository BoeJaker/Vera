from vera.catalog.specialist_core import nlp_node_row


def test_specialist_node_row_preserves_portable_package_identity():
    row = nlp_node_row({
        "node_id": "cpu-a",
        "component": {"version": "1", "intact": True},
        "tasks": {"ner": {
            "model": "owner/model",
            "present": True,
            "loaded": True,
            "model_package": {
                "package_id": "mpkg_verified",
                "version": "content-abc123",
                "format": "onnx",
            },
        }},
    }, {"version": "1"}, lambda expected, actual: {
        "state": "current", "changed": [],
    })

    assert row["tasks"]["ner"] == {
        "model": "owner/model",
        "present": True,
        "loaded": True,
        "package": "content-abc123",
        "package_id": "mpkg_verified",
        "format": "onnx",
    }
