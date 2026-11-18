-- 0003_evidence_seq.sql —— 证据在事件内的顺序。
--
-- 主键是 (event_id, document_id, role)，只保证不去重，不保证顺序。
-- 但顺序是有意义的：报告按它编号，详情页复核也只取前 N 条。
-- 没有这一列，13 份历史产物里有 8 份读回后证据顺序会乱。

ALTER TABLE event_evidence ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
