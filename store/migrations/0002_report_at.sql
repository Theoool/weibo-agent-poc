-- 0002_report_at.sql —— 补记报告自身的生成时刻。
--
-- 0001 里只有 run.started_at / finished_at，两者都不是报告头的「生成时间」：
-- 实时运行时它是 investigator.run() 返回的时刻（比 started_at 晚几分钟），
-- 导入历史产物时它应当等于原文件的 generated_at。少了这一列，
-- 报告从库里重渲染后时间会漂移，而时间恰恰是判断"这份报告有多旧"的依据。

ALTER TABLE workflow_run ADD COLUMN report_at TEXT;
