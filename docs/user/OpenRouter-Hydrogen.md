# Gemini 4 Hydrogen 正文候选

DeepSeek V4 Pro 是新装与旧配置的默认正文模型。在「模型连接 → 主回复模型」保存 OpenRouter API Key 并启用候选。密钥通过 Windows DPAPI 保存，设置页显示录入状态。

先核对原已审正文装配的版本、角色范围及发给 OpenRouter 的材料。作者确认后，维护者可在已登记的隔离实例执行 `python -m persona_runtime.main_reply --home <实例目录> --reviewed-bundle <已审装配的完整SHA-256>`。受保护角色还须核准该角色的正文用途，可用同一命令的 `--authorize-persona <角色ID>` 登记。密钥可在本机 PowerShell 用 `--key` 隐藏输入。

装配就绪后，显式将「新回合正文」切到 Gemini 4 Hydrogen，并确认目标会话的全流程和 OpenRouter 正文授权。新回合按受理时绑定的装配、凭据和预算执行。已受理回合沿原绑定继续，凭据或授权变化时按原回合记录停靠。重开后可在同一设置查看实际选中的模型。选回 DeepSeek V4 Pro 后，新回合走原官方正文路线。

Hydrogen 使用 OpenRouter 上的 `google/gemini-4-hydrogen`，正文请求固定为 `low` 并排除推理内容。查前预告由 Flash 负责，检索和记忆沿现有接收方。模型供应商实际处理范围以会话授权和上游回执为准。

已用作者获准的单句完成两种 API 响应模式的真实探针。角色实窗、长文、查后材料及作者盲评沿隔离灰度安排推进。
