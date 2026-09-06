-- 0002_memory_fts: keyword/lexical search side of hybrid recall.
--
-- Standalone FTS5 table (not `content=`-linked) so the application controls every
-- write explicitly: one INSERT on `remember`, one DELETE on `forget`. That keeps the
-- "deletion actually deletes" invariant simple to verify — there is no external-content
-- trigger machinery that could silently leave a row behind.
--
-- `remove_diacritics 2` folds "ă/â/î/ș/ț" toward their base Latin letters, which lets a
-- Romanian query without diacritics (common when typing/ASR) still match text written
-- with them, and vice versa. This is the keyword half of cross-lingual recall; the
-- semantic half is the vector index.
--
-- classification: `text` mirrors memory_records.text — same sensitivity, same rule:
-- never logged.
CREATE VIRTUAL TABLE memory_fts USING fts5(
    text,
    tokenize = 'unicode61 remove_diacritics 2'
);
