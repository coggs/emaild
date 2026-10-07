import base64
import os

import pytest

from emaild import crypto
from emaild.config import Settings, sql_identifier
from emaild.db import split_script
from emaild.llm.base import LLMResult, PolicyError
from emaild.llm.router import Router
from emaild.search import rrf, text_query

KEY = base64.urlsafe_b64encode(os.urandom(32)).decode()


def test_text_query_escapes_and_drops_stopwords():
    assert text_query("What did the accountant say about the BAS?") == "{accountant} ACCUM {bas}"
    assert text_query("the a of") is None
    q = text_query("near} OR {x} jane@acct.example")
    assert "}" not in q.replace("}", "", q.count("{"))  # every term closed exactly once
    assert "{jane@acct.example}" in q


def test_rrf_prefers_items_in_both_lists():
    s = rrf([[1, 2, 3], [3, 4]])
    assert max(s, key=s.get) == 3


def test_per_user_keys_isolated():
    token = crypto.encrypt(KEY, 1, 1, b"secret")
    assert crypto.decrypt(KEY, 1, 1, token) == b"secret"
    with pytest.raises(Exception):
        crypto.decrypt(KEY, 1, 2, token)


def test_sql_identifier():
    assert sql_identifier("all_minilm_l12_v2") == "ALL_MINILM_L12_V2"
    with pytest.raises(ValueError):
        sql_identifier("x; drop table items")


def test_split_script():
    text = "-- c\nCREATE TABLE t (a NUMBER)\n/\nBEGIN\n  NULL;\nEND;\n/\n"
    assert split_script(text) == ["CREATE TABLE t (a NUMBER)", "BEGIN\n  NULL;\nEND;"]


def test_all_sql_files_split(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "db"
    for f in list((root / "migrations").glob("*.sql")) + list((root / "bootstrap").glob("*.sql")):
        stmts = split_script(f.read_text())
        assert stmts, f
        for s in stmts:
            assert not s.rstrip().endswith("/"), f


class _Remote:
    name, is_local, model = "remote", False, "m"

    def chat(self, messages, schema=None, temperature=0.1):
        return LLMResult("ok", "m", "remote", False)


def test_router_blocks_cloud_for_local_only(monkeypatch):
    monkeypatch.setenv("EMAILD_LLM_PROVIDER", "remote")
    r = Router(Settings(), providers={"remote": _Remote()})
    with pytest.raises(PolicyError):
        r.chat("ask", [], policy="local_only")
    assert r.chat("ask", [], policy="cloud_allowed").text == "ok"
