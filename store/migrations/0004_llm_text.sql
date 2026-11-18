-- 0004_llm_text.sql —— 模型调用的题面与输出全文。
--
-- 0001 只留了 prompt_ref / response_ref（内容寻址指针），但没留内联列：
-- 一次 ping（几十字）或一条短 JSON 的题面不足 4KB，按 blobs.INLINE_LIMIT
-- 的规则应当内联进库，结果指针为空、内容无处可放，正好丢掉最短也最有用的那批。
-- 两列与 source_document.text_inline 同义：ref 有值读文件，ref 为空读这里。

ALTER TABLE llm_call ADD COLUMN prompt_inline TEXT;
ALTER TABLE llm_call ADD COLUMN response_inline TEXT;
