"""Runtime guards for exact-match projection reuse and its invalidation."""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from unittest.mock import call, patch

from . import mdcg, readcache
from .mdcos import MdCGOS


def frozen_exact(q, content, **kw):
    """Pre-optimization predicate, independent of all new projection helpers."""
    query = re.sub(r"\s+", "", q or "")
    return int(len(query) >= 2 and query in re.sub(r"\s+", "", content or ""))


class ExactTextCache(unittest.TestCase):
    def setUp(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("MDCG_")}
        clean.update(MDCG_HOTCACHE="0", MDCG_READ_CACHE="1", MDCG_FRESHNESS="0",
                     MDCG_UNIFY_QUERY="0", MDCG_EN_ATOMS="0")
        self.env = patch.dict(os.environ, clean, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def make_store(self, bodies=None):
        temp = tempfile.TemporaryDirectory(prefix="exact_projection_")
        self.addCleanup(temp.cleanup)
        cg = MdCGOS(temp.name)
        self.addCleanup(cg.close)
        for nid, body in (bodies or {"a": "AB 与 BC 分开", "z": "A B\u2003C", "case": "abc"}).items():
            cg.add(nid, body, layer="knowledge", importance=0.5)
        cg.flush()
        return cg

    def docs(self, cg):
        return [(entry, *cg._read(entry)) for entry in cg.index["nodes"].values()]

    def score(self, cg, query):
        return cg._score(self.docs(cg), query, mdcg.bigrams(query))

    def test_raw_and_prepared_predicates_preserve_whitespace_and_case(self):
        cases = [("ABC", "A B\u2003C", 1), ("A\tB C", "ABC", 1),
                 ("ABC", "AB 与 BC 分开", 0), ("ABC", "abc", 0),
                 ("甲\n乙", "甲\u00a0乙", 1), ("甲乙", "甲\u3000乙", 1),
                 ("", "ABC", 0), (None, None, 0), ("A", "A", 0)]
        for query, body, expected in cases:
            with self.subTest(query=query, body=body):
                prepared = (mdcg.compact_exact_text(query), mdcg.compact_exact_text(body))
                self.assertEqual(mdcg.exact_hit_of(query, body), expected)
                self.assertEqual(mdcg.exact_hit_of(query, body, _prepared=prepared), expected)

    def test_production_results_and_scores_equal_frozen_predicate(self):
        cg = self.make_store()
        for mode in ("legacy", "jaccard"):
            with patch.object(mdcg, "SCORE_MODE", mode):
                for query in ("ABC", "A B C", "甲乙", "unmatched", "", "abc"):
                    with self.subTest(mode=mode, query=query):
                        cached_scores = self.score(cg, query)
                        new_search, _ = cg.search(query, k=3, judge=False, record=False)
                        new_rrf, _ = cg.search_rrf(query, k=3, paths=("lexical",), judge=False, record=False)
                        with patch.object(mdcg, "exact_hit_of", frozen_exact):
                            self.assertEqual(cached_scores, self.score(cg, query))
                            self.assertEqual(new_search, cg.search(query, k=3, judge=False, record=False)[0])
                            self.assertEqual(new_rrf, cg.search_rrf(query, k=3, paths=("lexical",), judge=False, record=False)[0])

    def test_warm_scoring_normalizes_only_query_once(self):
        cg = self.make_store()
        self.score(cg, "ABC")
        with patch.object(mdcg, "compact_exact_text", wraps=mdcg.compact_exact_text) as normalize:
            self.score(cg, "A B C")
        self.assertEqual(normalize.call_args_list, [call("A B C")])

    def test_one_write_invalidates_only_changed_document(self):
        cg = self.make_store()
        self.score(cg, "ABC")
        before = {path: record[1] for path, record in cg._exact_text_cache.items()}
        cg.add("a", "A\tB\nC 新内容", layer="knowledge", importance=0.5)
        cg.flush()
        with patch.object(mdcg, "compact_exact_text", wraps=mdcg.compact_exact_text) as normalize:
            rows = self.score(cg, "ABC")
        self.assertEqual(normalize.call_count, 2)  # one query, one changed document
        a = next(node for node, _ in rows if node["id"] == "a")
        self.assertEqual(a[mdcg.EXACT_HIT_KEY], 1)
        for path, record in cg._exact_text_cache.items():
            if path != cg.index["nodes"]["a"]["path"]:
                self.assertIs(record[1], before[path])

    def test_clear_removes_projection_without_changing_legacy_count(self):
        cg = self.make_store()
        cg.search_rrf("ABC", paths=("lexical",), judge=False, record=False)
        expected = sum(len(getattr(cg, name, {})) for name in
                       ("_read_cache", "_norm_bigrams_cache", "_positive_body_cache"))
        self.assertTrue(cg._exact_text_cache)
        self.assertEqual(readcache.clear(cg), expected)
        self.assertFalse(cg._exact_text_cache)
        self.assertEqual(readcache.clear(cg), 0)

    def test_short_query_does_not_normalize_documents(self):
        cg = self.make_store()
        with patch.object(cg, "_doc_exact_text", side_effect=AssertionError("unnecessary projection")):
            rows = self.score(cg, "A")
        self.assertTrue(all(node[mdcg.EXACT_HIT_KEY] == 0 for node, _ in rows))

    def test_opt_out_uses_uncached_projection(self):
        with patch.dict(os.environ, {"MDCG_READ_CACHE": "0"}):
            cg = self.make_store()
        self.assertFalse(hasattr(cg, "_exact_text_cache"))
        rows = self.score(cg, "ABC")
        self.assertEqual({node["id"]: node[mdcg.EXACT_HIT_KEY] for node, _ in rows},
                         {"a": 0, "z": 1, "case": 0})

    def test_instances_with_same_relative_path_are_isolated(self):
        first = self.make_store({"same": "A B C"})
        second = self.make_store({"same": "D E F"})
        self.assertEqual(self.score(first, "ABC")[0][0][mdcg.EXACT_HIT_KEY], 1)
        self.assertEqual(self.score(second, "ABC")[0][0][mdcg.EXACT_HIT_KEY], 0)
        self.assertIsNot(first._exact_text_cache, second._exact_text_cache)

    def test_reinstall_does_not_keep_hidden_projection_cache(self):
        cg = self.make_store()
        entry = next(iter(cg.index["nodes"].values()))
        cg._doc_exact_text(entry, "A B C")
        readcache.install(cg)
        readcache.clear(cg)
        self.assertEqual(cg._doc_exact_text(entry, "D E F"), "DEF")


if __name__ == "__main__":
    unittest.main()
