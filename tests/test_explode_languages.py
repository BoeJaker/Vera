# -*- coding: utf-8 -*-
"""Go, Rust and Java (EXPLODE.md §9, S7): the point of explode is any code a reader has in front of them -- an
LLM's reply, a cloned repo, a page's source -- not only the estate's own Python and JS. These three carry most of
what gets cloned and share a shape: a package line, imports, and named things whose bodies are braces. This is
the same TOLERANT reading the JS path uses and signs its cards the same way, so a reader can see what read it.
"""
import asyncio

import pytest

from vera.research import code_explode_core as C
from vera.research import explode_capabilities as X

GO = '''package store

import (
    "fmt"
    "net/http"
    x "os/exec"
)

type Store struct {
    db *DB
}

type Reader interface {
    Read(p []byte) (int, error)
}

func (s *Store) Load(id string) (*Row, error) {
    row, err := s.db.Query(id)   // TODO: cache this
    if err != nil {
        return nil, err
    }
    return parse(row), nil
}

func parse(r *Row) *Row {
    fmt.Println(r)
    return r
}
'''

RS = '''use std::collections::HashMap;
use crate::db::Row;

pub struct Store { rows: HashMap<String, Row> }

pub trait Load { fn load(&self, id: &str) -> Option<Row>; }

impl Load for Store {
    fn load(&self, id: &str) -> Option<Row> {
        let row = self.rows.get(id).unwrap();
        Some(parse(row))
    }
}

pub fn parse(r: &Row) -> Row { r.clone() }
'''

JAVA = '''package app.store;

import java.util.Map;

public class Store extends Base implements Load {
    private Map<String, Row> rows;

    public Row load(String id) {
        Row row = rows.get(id);
        return parse(row);
    }

    private Row parse(Row r) {
        System.out.println(r);   // FIXME: logging
        return r;
    }
}
'''


def syms(p, kind=None):
    return {s["qual"]: s for s in p["symbols"] if s["kind"] != "module" and (kind is None or s["kind"] == kind)}


def test_a_file_is_known_by_its_extension_and_by_its_own_first_lines():
    assert C.detect_lang("store.go") == "go" and C.detect_lang("s.rs") == "rust" and C.detect_lang("S.java") == "java"
    # Go's `import (` reads as Python's `import`, and Rust's `pub struct S { .. }` as a CSS rule: the specific
    # sniffs have to win
    assert C.detect_lang("", GO) == "go"
    assert C.detect_lang("", RS) == "rust"
    assert C.detect_lang("", JAVA) == "java"
    assert C.detect_lang("x.py", "def f(): pass") == "python"        # and nothing else was broken doing it


def test_go_reads_its_imports_its_types_and_the_receiver_a_method_hangs_on():
    p = C.parse_brace_patterns("store.go", GO, "go")
    assert [i["module"] for i in p["imports"]] == ["fmt", "net/http", "os/exec"]
    assert p["aliases"]["x"] == "os/exec"                              # an aliased import keeps its alias
    S = syms(p)
    assert S["Store"]["kind"] == "class" and S["Reader"]["kind"] == "class"
    assert S["Store.Load"]["kind"] == "method" and S["Store.Load"]["own_class"] == "Store"
    assert S["parse"]["kind"] == "function"
    assert [c["name"] for c in S["Store.Load"]["calls"]] == ["Query", "parse"]
    assert S["Store.Load"]["line0"] == 17 and S["Store.Load"]["line1"] == 23


def test_rust_hangs_an_impls_methods_on_the_type_it_is_FOR_not_the_trait():
    p = C.parse_brace_patterns("store.rs", RS, "rust")
    S = syms(p)
    assert "Store.load" in S and "Load.load" not in S                  # `impl Load for Store` is Store's method
    assert S["Store"]["kind"] == "class" and S["Load"]["kind"] == "class"
    assert [c["name"] for c in S["Store.load"]["calls"]] == ["get", "unwrap", "Some", "parse"]
    assert [i["module"] for i in p["imports"]] == ["std::collections::HashMap", "crate::db::Row"]
    assert [c["name"] for c in S["parse"]["calls"]] == ["clone"]       # a one-line body is still a body


def test_java_reads_a_class_what_it_extends_and_implements_and_its_methods():
    p = C.parse_brace_patterns("Store.java", JAVA, "java")
    S = syms(p)
    assert S["Store"]["bases"] == ["Base", "Load"]
    assert S["Store.load"]["own_class"] == "Store" and S["Store.parse"]["own_class"] == "Store"
    assert [c["name"] for c in S["Store.load"]["calls"]] == ["get", "parse"]
    assert not S["Store"]["calls"]                                     # a type's calls belong to its methods


def test_no_symbol_calls_itself_and_no_keyword_is_a_call():
    for src, lang, path in ((GO, "go", "a.go"), (RS, "rust", "a.rs"), (JAVA, "java", "A.java")):
        for s in C.parse_brace_patterns(path, src, lang)["symbols"]:
            names = [c["name"] for c in s["calls"]]
            assert s["name"] not in names, (lang, s["qual"], names)
            assert not ({"if", "for", "while", "switch", "func", "fn", "match", "return"} & set(names)), (lang, names)


def test_the_lint_is_what_is_true_of_all_three_and_says_so():
    go = {(f["line"], f["code"]) for f in C._brace_lint("a.go", GO)}
    assert (18, "T000") in go and (26, "B101") in go                   # a TODO, and a Println left in
    rs = {f["code"] for f in C._brace_lint("a.rs", RS)}
    assert "B100" in rs                                                # unwrap() panics rather than returning
    jv = {f["code"] for f in C._brace_lint("A.java", JAVA)}
    assert "T000" in jv and "B101" in jv
    assert not C._brace_lint("a.go", 'x := "TODO: not this one, it is a string"\n')


def test_the_whole_contract_comes_out_of_a_mixed_set_and_every_card_says_what_read_it():
    doc = C.explode_sources([{"path": "store.go", "text": GO, "lang": "go"},
                             {"path": "store.rs", "text": RS, "lang": "rust"},
                             {"path": "Store.java", "text": JAVA, "lang": "java"}])
    titles = {c["title"] for c in doc["cards"]}
    assert {"Store", "parse", "Load", "load"} & titles
    assert all(c.get("by") for c in doc["cards"])
    assert {c["by"] for c in doc["cards"] if c["kind"] != "external"} == {"patterns"}
    assert doc["counts"]["files"] == 3 and not doc["source"]["partial"]
    calls = [e for e in doc["edges"] if e["kind"] == "CALLS"]
    assert calls and all(e["resolution"] in ("exact", "heuristic", "external") for e in calls)
    # a call to something in the same file resolves exactly
    ids = {c["id"]: c for c in doc["cards"]}
    assert any(e["resolution"] == "exact" and ids[e["to"]]["title"] == "parse" for e in calls)


def test_the_capability_and_the_picker_both_know_these_files():
    assert X._CODE_EXT.count(".go") and X._CODE_EXT.count(".rs") and X._CODE_EXT.count(".java")
    d = asyncio.get_event_loop().run_until_complete(
        X.explode_code(text=GO, lang="go", path="store.go"))
    assert d["ok"] and d["source"]["engines"] == ["patterns"]
    assert any(c["title"] == "Store.Load" or c["title"] == "Load" for c in d["cards"])
