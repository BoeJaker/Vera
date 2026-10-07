"""The intent core orders the loop's catalogue; it never removes a cap."""

from vera.dag import intent_core_core as IC

CAT = ["exec.bash.run", "http.get", "caps.search", "memory.seek", "markets.strategy.list",
       "web.search", "web.fetch", "sandbox.session.fs.read", "render.html", "cal.todo.toggle",
       "dream.trigger.toggle", "agentbridge.check_updates", "memory.map", "memory.read",
       "exec.python.run", "code.edit", "operator.run", "prose.author", "code.author",
       "web.research"]


def test_research_core_reaches_the_fast_path_head():
    order, info = IC.apply(CAT, "research", "Write a short report on X with sources")
    assert order[:6] == ["web.research", "prose.author", "sandbox.session.fs.read",
                         "web.fetch", "exec.bash.run", "web.search"]
    assert info["after"]["prose.author"] < IC.FRONT <= info["before"]["prose.author"]
    assert "prose.author" in info["moved"]


def test_nothing_is_removed_and_the_rest_keeps_its_order():
    order, _ = IC.apply(CAT, "build")
    assert sorted(order) == sorted(CAT)
    rest = [c for c in order if c not in IC.INTENT_CORES["build"]]
    assert rest == [c for c in CAT if c not in IC.INTENT_CORES["build"]]


def test_caps_the_goal_names_stay_first():
    order, info = IC.apply(CAT, "action", "Run markets.strategy.list and report the count")
    assert order[0] == "markets.strategy.list"
    assert order[1] == "exec.bash.run"
    assert info["named"] == ["markets.strategy.list"]


def test_mixed_and_unknown_intents_leave_the_catalogue_alone():
    for intent in ("mixed", "", "chat", None):
        order, info = IC.apply(CAT, intent)
        assert order == CAT and info["moved"] == [] and info["added"] == []


def test_a_missing_core_cap_is_not_added_by_default():
    cat = [c for c in CAT if c != "operator.run"]
    order, info = IC.apply(cat, "build", known=set(CAT))
    assert "operator.run" not in order and info["added"] == []
    assert sorted(order) == sorted(cat)


def test_add_missing_adds_only_registered_unblocked_caps():
    cat = [c for c in CAT if c not in ("code.author", "operator.run", "prose.author")]
    known = set(CAT) - {"prose.author"}
    order, info = IC.apply(cat, "build", known=known, blocked={"operator.run"}, add_missing=True)
    assert "code.author" in order and info["added"] == ["code.author"]
    assert "operator.run" not in order        # blocked
    assert "prose.author" not in order        # not registered
    assert len(order) == len(cat) + 1


def test_idempotent():
    once, _ = IC.apply(CAT, "research", "report")
    twice, info = IC.apply(once, "research", "report")
    assert twice == once and info["moved"] == []


def test_duplicates_are_dropped_not_multiplied():
    order, _ = IC.apply(CAT + ["exec.bash.run"], "action")
    assert order.count("exec.bash.run") == 1


def test_resolve_mode():
    assert IC.resolve_mode("order") == "order"
    assert IC.resolve_mode(" ORDER ") == "order"
    for v in ("", None, "on", "trim", "off"):
        assert IC.resolve_mode(v) == "off"


def test_named_caps_are_whole_catalogue_names_only():
    assert IC.named_caps("use code.edit on index.html", CAT) == ["code.edit"]
    assert IC.named_caps("edit the code, then run it", CAT) == []
