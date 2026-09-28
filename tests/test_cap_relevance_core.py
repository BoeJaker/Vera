"""The planner's capability catalogue (user 2026-09-27: "give the planners a
better set of caps and enhance the delivery of relevant caps to the planners").

Measured on prod the same day: 0 of 2,554 caps embedded (the startup pass was
off and returned before even loading the cached vectors), so search was purely
lexical, matching query words as SUBSTRINGS of name parts. Census catalogues
carried markets.custom.create for "Create clock.html", ide.vscode.password.
reveal and netscan.target.traceroute for "explain a race condition"; 29-38
caps offered per run, 1-8 used.
"""

import inspect
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import cap_relevance_core as C  # noqa: E402

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
    from Vera.vera.dag import dag_store as S
except Exception:                                    # pragma: no cover
    M = S = None

needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")


def test_generic_and_function_words_never_score_by_name():
    toks = C.query_tokens("Create clock.html - a self-contained HTML page showing a live clock")
    assert "create" not in toks and "html" not in toks and "page" not in toks
    assert "clock" in toks
    assert C.score("markets.custom.create", query="Create clock.html") == 0.0


def test_a_name_matches_whole_words_only():
    q = "Write a 200-word explainer of what a race condition is"
    assert C.score("netscan.target.traceroute", query=q) == 0.0     # "race" is not "trace"
    assert C.score("pxstore.store.writer.provision", query=q) == 0.0
    assert C.score("browser.screenshot", query="take a browser screenshot") > 0


def test_meaning_outweighs_a_single_name_word():
    q = "look up the latest news"
    near = C.score("web.research", query=q, q_emb=[1.0, 0.0], embedding=[0.9, 0.1])
    word = C.score("news.feed.delete", query=q, q_emb=[1.0, 0.0], embedding=[0.0, 1.0])
    assert near > word


def test_the_tail_is_short_and_earned():
    ranked = ["dream.trigger.toggle", "cal.todo.toggle", "render.html", "agentbridge.check_updates",
              "markets.evolve.start", "evolve.schedule.upsert"]
    tail = C.select_tail(ranked, "Create clock.html, a live digital clock")
    assert tail == ranked[:C.ALWAYS_TOP]                            # generic goal: 3, not 20
    mk = ["markets.backtest.run", "markets.backtest.batch", "markets.backtest.sweep",
          "markets.backtest.analyze", "cal.todo.toggle", "markets.backtest.signals"]
    tail = C.select_tail(mk, "Backtest a moving-average crossover on BTC with the markets backtester")
    assert "markets.backtest.signals" in tail and "cal.todo.toggle" not in tail
    many = ["browser.%d" % i for i in range(20)]
    assert len(C.select_tail(many, "use the browser")) == C.MAX_TAIL
    assert C.select_tail(["a.b", "c.d"], "x", exclude={"a.b"}) == ["c.d"]


def test_catalogue_lines_keep_whole_sentences_and_the_when_to_use():
    d = ("Unified fast web research - search the web BROADLY and PULL the useful content from "
         "the top results in ONE call. Results are cached per session and deduplicated across "
         "engines so repeated queries stay cheap and consistent. WHEN TO USE: the default way to "
         "look something up.")
    line = C.brief_line("web.research", d)
    assert line.startswith("web.research — Unified") and "WHEN TO USE" in line
    assert len(line) - len("web.research — ") <= C.BRIEF_MAX
    assert "ONE call." in line                                  # never cut mid-sentence
    assert C.brief_line("x.y", "") == "x.y"


@needs_app
def test_secret_and_provisioning_caps_are_never_discovered_into_a_catalogue():
    for n in ("ide.vscode.password.reveal", "ide.vscode.password.set", "platform.secrets.set",
              "pxstore.store.writer.provision", "sandbox.config.set", "vault.token.issue"):
        assert M._planner_catalog_sensitive(n), n
    for n in ("web.research", "code.author", "exec.bash.run", "operator.run", "tokenize.text"):
        assert not M._planner_catalog_sensitive(n), n
    src = inspect.getsource(M._workshop_build_toolkit)
    assert "explicit or not _planner_catalog_sensitive(name)" in src    # base toolkit still wins
    assert "add(c, explicit=True)" in src
    assert "_cap_relevance.select_tail(" in src


@needs_app
def test_cached_vectors_load_even_when_startup_embedding_is_off():
    src = inspect.getsource(S.CapabilityIndex.start_embedding)
    # the early return used to come BEFORE the cache load
    assert src.index('hgetall("vera:cap_embeddings")') < src.index("if not do_embed:")
    assert "embed_missing=True" in inspect.getsource(S.caps_embed_run)
    assert "_cap_rel.score(" in inspect.getsource(S.CapabilityIndex.relevance_search)
