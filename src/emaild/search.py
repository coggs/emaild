"""Hybrid search: Oracle Text (keywords) + in-DB vector similarity, fused with reciprocal rank fusion."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import oracledb

from . import store

_WORD = re.compile(r"[\w'@.\-]+", re.UNICODE)
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "was", "what", "did", "my", "me", "i",
         "about", "with", "from", "that", "this", "it", "be", "are", "do", "does", "any", "have", "has", "when",
         "who", "how", "which", "say", "said", "tell", "show", "find", "email", "emails"}


def text_query(q: str) -> str | None:
    """Turn free text into a safe Oracle Text query: each term escaped with {} and combined with ACCUM."""
    terms = []
    for t in _WORD.findall(q.lower()):
        t = t.strip(".-'")
        if len(t) < 2 or t in _STOP:
            continue
        terms.append("{" + t.replace("}", "") + "}")
    if not terms:
        return None
    return " ACCUM ".join(dict.fromkeys(terms))


def rrf(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


# Spam, trash, emails flagged as suspicious, and one-time codes / sign-in links never feed search, ask or briefs
# (keeps phishing text away from the model as "sources", and credentials out of answers).
EXCLUDE_UNSAFE = """NVL(JSON_SERIALIZE(i.labels), '[]') NOT LIKE '%"SPAM"%'
    AND NVL(JSON_SERIALIZE(i.labels), '[]') NOT LIKE '%"TRASH"%'
    AND NOT EXISTS (SELECT 1 FROM decisions dx WHERE dx.item_id = i.id
                    AND (NVL(JSON_VALUE(dx.corrected, '$.category'), dx.category) = 'one_time'
                         OR (NVL(JSON_VALUE(dx.corrected, '$.category'), dx.category) IN ('spam', 'suspicious')
                             AND NVL(JSON_VALUE(dx.corrected, '$.action'), dx.action) = 'archive')))"""
# One-time codes are always excluded, whatever action an earlier review left on them: while live they have their
# own countdown panel, and once expired they're noise (then scrubbed).


@dataclass
class Filters:
    sender: str | None = None
    after: date | None = None
    before: date | None = None
    account: str | None = None
    include_spam: bool = False
    # exact sender addresses (any of), e.g. from query.resolve_sender; combined with `sender` if both are set
    sender_addrs: list[str] | None = None
    # whole organisations: anyone @domain or @any.subdomain (e.g. "the club committee" -> everyone at the club's domain)
    sender_domains: list[str] | None = None
    # Date boundaries. None (the default, used by the MCP search/ask tools) keeps the original behaviour:
    # `after`/`before` mean UTC midnight. With an IANA zone (query.run passes EMAILD_TZ) they mean local midnight
    # in that zone, converted to a naive UTC datetime; the session TIME_ZONE is +00:00, so Oracle compares it
    # with received_at (TIMESTAMP WITH TIME ZONE) as UTC, the same convention brief.py uses.
    tz: str | None = None

    def boundary(self, d: date) -> datetime:
        if not self.tz:
            return datetime.combine(d, datetime.min.time())
        local = datetime.combine(d, datetime.min.time(), tzinfo=ZoneInfo(self.tz))
        return local.astimezone(timezone.utc).replace(tzinfo=None)

    def sql(self, binds: dict) -> str:
        parts = [] if self.include_spam else [EXCLUDE_UNSAFE]
        if self.sender:
            parts.append("(LOWER(i.sender_addr) LIKE :f_sender OR LOWER(i.sender_name) LIKE :f_sender)")
            binds["f_sender"] = f"%{self.sender.lower()}%"
        if self.sender_addrs:
            names = []
            for n, a in enumerate(self.sender_addrs[:50]):
                binds[f"f_sa{n}"] = a.lower()
                names.append(f":f_sa{n}")
            parts.append(f"LOWER(i.sender_addr) IN ({', '.join(names)})")
        if self.sender_domains:
            ors = []
            for n, d in enumerate(self.sender_domains[:10]):
                binds[f"f_sd{n}"] = f"%@{d.lower()}"
                binds[f"f_ss{n}"] = f"%.{d.lower()}"
                ors += [f"LOWER(i.sender_addr) LIKE :f_sd{n}", f"LOWER(i.sender_addr) LIKE :f_ss{n}"]
            parts.append("(" + " OR ".join(ors) + ")")
        if self.after:
            parts.append("i.received_at >= :f_after")
            binds["f_after"] = self.boundary(self.after)
        if self.before:
            parts.append("i.received_at < :f_before")
            binds["f_before"] = self.boundary(self.before)
        if self.account:
            parts.append("i.account_id IN (SELECT id FROM accounts WHERE LOWER(address) = :f_account)")
            binds["f_account"] = self.account.lower()
        return (" AND " + " AND ".join(parts)) if parts else ""


@dataclass
class Hit:
    item_id: int
    score: float
    received_at: str
    sender: str
    subject: str
    snippet: str
    thread_id: int | None


def _keyword(cur, q: str, f: Filters, k: int) -> tuple[list[int], dict[int, str]]:
    tq = text_query(q)
    if not tq:
        return [], {}
    binds: dict = {"q": tq, "k": k * 3}
    cur.execute(f"""SELECT c.item_id, c.content FROM chunks c JOIN items i ON i.id = c.item_id
                    WHERE CONTAINS(c.content, :q, 1) > 0 {f.sql(binds)}
                    ORDER BY SCORE(1) DESC FETCH FIRST :k ROWS ONLY""", binds)
    return _dedupe(cur.fetchall())


def _semantic(cur, model_id: int, model: str, q: str, f: Filters, k: int) -> tuple[list[int], dict[int, str]]:
    # only compare against vectors from the active model (model_id is set together with the embedding)
    binds: dict = {"q": q, "k": k * 3, "mid": model_id}
    cur.execute(f"""WITH qv AS (SELECT VECTOR_EMBEDDING({model} USING :q AS data) v FROM dual)
                    SELECT c.item_id, c.content FROM chunks c JOIN items i ON i.id = c.item_id CROSS JOIN qv
                    WHERE c.model_id = :mid {f.sql(binds)}
                    ORDER BY VECTOR_DISTANCE(c.embedding, qv.v, COSINE) FETCH FIRST :k ROWS ONLY""", binds)
    return _dedupe(cur.fetchall())


def _dedupe(rows) -> tuple[list[int], dict[int, str]]:
    order, best = [], {}
    for item_id, content in rows:
        if item_id not in best:
            best[item_id] = content
            order.append(item_id)
    return order, best


def _recent(cur, f: Filters, k: int) -> list[int]:
    binds: dict = {"k": k}
    cur.execute(f"""SELECT i.id FROM items i WHERE 1=1 {f.sql(binds)}
                    ORDER BY i.received_at DESC FETCH FIRST :k ROWS ONLY""", binds)
    return [r[0] for r in cur]


def search(conn: oracledb.Connection, query: str = "", filters: Filters | None = None, limit: int = 10,
           newest: bool = False) -> list[Hit]:
    """Hybrid search. With `newest`, a query takes the best few matches (3x the limit) and returns the newest of
    those, newest first; an empty query is always newest first."""
    f = filters or Filters()
    cur = conn.cursor()
    passages: dict[int, str] = {}
    if query.strip():
        pool = min(limit * 3, 75) if newest else limit
        kw_ids, kw_pass = _keyword(cur, query, f, pool)
        model = store.active_model(conn)
        sem_ids, sem_pass = _semantic(cur, model[0], model[1], query, f, pool) if model else ([], {})
        scores = rrf([kw_ids, sem_ids])
        passages = {**sem_pass, **kw_pass}
        ranked = sorted(scores, key=scores.get, reverse=True)[:pool]
    else:
        ranked = _recent(cur, f, limit)
        scores = {i: 0.0 for i in ranked}
    if not ranked:
        return []
    binds = {f"i{n}": v for n, v in enumerate(ranked)}
    cur.execute(f"""SELECT id, received_at, sender_name, sender_addr, subject, snippet, thread_id FROM items
                    WHERE id IN ({",".join(":" + b for b in binds)})""", binds)
    rows = {r[0]: r for r in cur}
    if newest and query.strip():
        _epoch = datetime.min.replace(tzinfo=timezone.utc)

        def _when(i):
            v = rows[i][1] if i in rows else None
            if isinstance(v, datetime):
                return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            return _epoch
        ranked = sorted(ranked, key=_when, reverse=True)[:limit]
    hits = []
    for item_id in ranked:
        r = rows.get(item_id)
        if not r:
            continue
        sender = f"{r[2]} <{r[3]}>" if r[2] else (r[3] or "")
        passage = passages.get(item_id) or r[5] or ""
        hits.append(Hit(item_id=item_id, score=round(scores[item_id], 4), received_at=str(r[1]) if r[1] else "",
                        sender=sender, subject=r[4] or "(no subject)", snippet=passage[:400], thread_id=r[6]))
    return hits
