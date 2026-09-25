"""Owner's Office 流水线。

硬规则：模型调用只能放在 pipeline/llm.py（CI 检查 C-LLM-ENTRY）。
本包的其他模块需要模型时，调用 pipeline.llm.complete()，不要自己导入模型 SDK。

- llm.py：唯一的模型调用入口。按 agents/*.yml 与提示词 v3 的 front matter 组装请求、裁剪输入、
  预算守卫、记录日志，校验输出并在不合格时重试一次。
- outputs.py：<output> 封装的解析与校验、generated_by、按 00 §F2 放置（不调用模型）。
- isolation.py：从成品里删去审计类输入不该看到的章节（00 §G6，不调用模型）。
"""
