import pytest

from vera.fabric.dataset_provider import CancellationSignal, QueryCancelled
from vera.fabric.huggingface_dataset_adapter import (
    HuggingFaceAdapterError, HuggingFaceDatasetAdapter, HuggingFaceSource,
)


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"
REVISION = "a" * 40


class Features(dict):
    def to_dict(self):
        return dict(self)


class FakeDataset:
    features = Features({"id": {"dtype": "int64"}})
    _fingerprint = "fixture-fingerprint"

    def __init__(self, rows):
        self.rows = list(rows)

    def __len__(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)


class FakeStream(FakeDataset):
    def __init__(self, rows):
        super().__init__(rows)
        self.offset = 0

    def __iter__(self):
        while self.offset < len(self.rows):
            value = self.rows[self.offset]
            self.offset += 1
            yield value

    def state_dict(self):
        return {"offset": self.offset}

    def load_state_dict(self, state):
        self.offset = state["offset"]


class Loader:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        cls = FakeStream if kwargs["streaming"] else FakeDataset
        return cls(self.rows)


def source(**values):
    return HuggingFaceSource(repo_id="owner/data", revision=REVISION,
                             split="train", config="english", **values)


def test_source_requires_pinned_hub_identity_and_has_stable_id():
    first = source()
    assert first.source_id == source().source_id
    assert first.dataset_id == "hf.owner_data.english.train"
    with pytest.raises(ValueError, match="pinned"):
        HuggingFaceSource(repo_id="owner/data", revision="main", split="train")
    with pytest.raises(ValueError, match="owner/name"):
        HuggingFaceSource(repo_id="./local", revision=REVISION, split="train")


def test_materialize_maps_schema_provenance_and_disables_implicit_token():
    loader = Loader([{"id": 1}, {"id": 2}])
    adapter = HuggingFaceDatasetAdapter(loader, package_version="4.8.4")
    snapshot, records = adapter.materialize(source(), created_at=NOW)
    assert records == ({"id": 1}, {"id": 2})
    assert snapshot.schema == {"id": {"dtype": "int64"}}
    assert snapshot.provenance["source"]["revision"] == REVISION
    assert loader.calls == [{"path": "owner/data", "split": "train",
                             "revision": REVISION, "streaming": False,
                             "token": False, "name": "english"}]


def test_materialize_refuses_truncation_and_non_json_rows():
    adapter = HuggingFaceDatasetAdapter(Loader([{"id": 1}, {"id": 2}]),
                                        package_version="4.8.4")
    with pytest.raises(HuggingFaceAdapterError, match="above materialization"):
        adapter.materialize(source(), created_at=NOW, max_records=1)
    bad = HuggingFaceDatasetAdapter(Loader([{"value": float("nan")}]),
                                    package_version="4.8.4")
    with pytest.raises(ValueError, match="canonical JSON"):
        bad.materialize(source(), created_at=NOW)


def test_stream_pages_resume_from_provider_checkpoint_without_duplicates():
    loader = Loader([{"id": 1}, {"id": 2}, {"id": 3}])
    adapter = HuggingFaceDatasetAdapter(loader, package_version="4.8.4")
    first = adapter.stream_page(source(), limit=2)
    second = adapter.stream_page(source(), limit=2, cursor=first.next_cursor)
    assert [row["id"] for row in first.records + second.records] == [1, 2, 3]
    assert not first.exhausted and second.exhausted and second.next_cursor == ""
    assert all(call["streaming"] for call in loader.calls)


def test_stream_cursor_is_bound_to_exact_source():
    adapter = HuggingFaceDatasetAdapter(Loader([{"id": 1}, {"id": 2}]),
                                        package_version="4.8.4")
    page = adapter.stream_page(source(), limit=1)
    other = HuggingFaceSource(repo_id="owner/data", revision="b" * 40,
                              split="train", config="english")
    with pytest.raises(ValueError, match="mismatched"):
        adapter.stream_page(other, limit=1, cursor=page.next_cursor)
    with pytest.raises(ValueError, match="size limit"):
        adapter.stream_page(source(), limit=1, cursor="x" * 16_385)


def test_stream_requires_checkpoint_api_and_propagates_cancellation():
    adapter = HuggingFaceDatasetAdapter(
        lambda **kwargs: iter([{"id": 1}]), package_version="4.8.4")
    with pytest.raises(HuggingFaceAdapterError, match="checkpoint state"):
        adapter.stream_page(source())
    signal = CancellationSignal()
    signal.cancel()
    loader = Loader([{"id": 1}])
    with pytest.raises(QueryCancelled):
        HuggingFaceDatasetAdapter(loader, package_version="4.8.4").stream_page(
            source(), cancellation=signal)
    assert loader.calls == []


def test_loader_failure_is_bounded_and_chained():
    def fail(**kwargs):
        raise OSError("secret endpoint detail")
    adapter = HuggingFaceDatasetAdapter(fail, package_version="4.8.4")
    with pytest.raises(HuggingFaceAdapterError, match="load failed") as caught:
        adapter.materialize(source(), created_at=NOW)
    assert isinstance(caught.value.__cause__, OSError)


def test_package_version_is_exactly_pinned_and_iteration_errors_are_bounded():
    with pytest.raises(ValueError, match="pinned to 4.8.4"):
        HuggingFaceDatasetAdapter(Loader([]), package_version="4.8.3")
    class Broken(FakeDataset):
        def __iter__(self):
            raise OSError("backend detail")
    adapter = HuggingFaceDatasetAdapter(
        lambda **kwargs: Broken([{"id": 1}]), package_version="4.8.4")
    with pytest.raises(HuggingFaceAdapterError, match="iteration failed") as caught:
        adapter.materialize(source(), created_at=NOW)
    assert isinstance(caught.value.__cause__, OSError)
