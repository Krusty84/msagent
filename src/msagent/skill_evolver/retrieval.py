#!/usr/bin/python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This file is part of the MindStudio project.
#
# MindStudio is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#
#    http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -------------------------------------------------------------------------

"""Minimal BM25 retrieval over skill descriptions (stdlib only).

Used by :mod:`msagent.skill_evolver.features` to find a library skill whose
description resembles what a session did without consulting any skill. Plain
BM25 over a few dozen short descriptions needs no embeddings, and the crude
tokenizer (no stemming) keeps every match explainable.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

# Function words that carry no topical signal; deliberately short.
_STOPWORD_TEXT = (
    "a an the and or of to in on for with is are be it this that as by at from "
    "use when you your not if then into how what which can will do does have has "
    "и в на с по для не что это как из к у о от а но или за то же бы он она они "
    "мы вы я ты так все всё его её их нет да ли при без до про уже"
)
STOPWORDS: frozenset[str] = frozenset(_STOPWORD_TEXT.split())

# Runs of letters/digits; "_" and "-" split tokens (read_file -> read, file).
_TOKEN_RE = re.compile(r"[^\W_]+")
_MIN_TOKEN_CHARS = 2


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens of ``text`` without stopwords and 1-char tokens."""
    tokens = _TOKEN_RE.findall(text.casefold())
    return [t for t in tokens if len(t) >= _MIN_TOKEN_CHARS and t not in STOPWORDS]


@dataclass(frozen=True, slots=True)
class SkillDoc:
    """A skill as seen by retrieval: its display name and description."""

    name: str
    description: str


@dataclass(frozen=True, slots=True)
class Hit:
    """One search result: the document, its BM25 score and the matched terms."""

    doc: SkillDoc
    score: float
    # Distinct query tokens found in the document, sorted.
    matched: tuple[str, ...]


class BM25Index:
    """BM25 (Lucene idf variant) over ``name + description`` of each document."""

    def __init__(
        self,
        docs: Sequence[SkillDoc],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self._docs = list(docs)
        self._k1 = k1
        self._b = b
        texts = [f"{doc.name} {doc.description}" for doc in self._docs]
        self._term_counts = [Counter(tokenize(text)) for text in texts]
        self._lengths = [sum(counts.values()) for counts in self._term_counts]
        total = sum(self._lengths)
        self._avg_length = total / len(self._docs) if self._docs else 0.0
        self._doc_freq: Counter[str] = Counter()
        for counts in self._term_counts:
            self._doc_freq.update(counts.keys())

    def __len__(self) -> int:
        return len(self._docs)

    def _idf(self, term: str) -> float:
        df = self._doc_freq.get(term, 0)
        return math.log(1.0 + (len(self._docs) - df + 0.5) / (df + 0.5))

    def search(self, query: str, *, top_k: int = 5) -> list[Hit]:
        """Return the best ``top_k`` documents sharing a term with ``query``."""
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1, got {top_k}")
        terms = sorted(set(tokenize(query)))
        hits: list[Hit] = []
        for doc, counts, length in zip(self._docs, self._term_counts, self._lengths):
            matched = [term for term in terms if term in counts]
            if not matched:
                continue
            norm = self._k1 * (1.0 - self._b + self._b * length / self._avg_length)
            score = 0.0
            for term in matched:
                tf = counts[term]
                score += self._idf(term) * tf * (self._k1 + 1.0) / (tf + norm)
            hits.append(Hit(doc=doc, score=score, matched=tuple(matched)))
        hits.sort(key=lambda hit: (-hit.score, hit.doc.name))
        return hits[:top_k]
