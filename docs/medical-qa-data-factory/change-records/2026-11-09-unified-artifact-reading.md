# 统一材料读取接口

单步调试第二步中，模型将材料 ID 放进 parameters，并在 source_view=true 时选择三篇材料。旧实现普通读取允许八份，source_view 只允许一份，导致读取未执行。

现在 read_artifacts 的唯一公开材料读取约定是：顶层 input_artifact_ids 列表（最多八个），无需模式开关。材料统一返回 source_views 列表；短材料给完整原文，长材料给段落索引。需要选段时用 parameters.source_sections={材料ID:[段落ID]}，每份一至四段，合计原文最多16000字符。混合读取普通产物时，普通记录仍在 content 中按 JSON 分页。

入口兼容旧模型返回：parameters.input_artifact_ids 归一化至顶层；两处值冲突则报明确错误。旧 source_view 不再决定读取模式，原有单篇 paragraph_ids 兼容调用仍可读取；新工具说明仅介绍 source_sections。单篇与多篇返回结构一致。主 agent skill 和相关错误说明同步更新。

验证：41项读取/主循环/研究工具相关测试通过，17项skill测试通过。重放真实第二步保存的调用，返回 material.pubmed.24463227（1216字符）、material.pubmed.27461822（1979字符）、material.pubmed.39062682（913字符），均为完整的已保存摘要，不代表论文全文。

重放结果保存于 data/runs/egfr-single-step/single_steps/002/read-fix-replay.json。这次只验证工具修复，没有调用主模型、提交运行状态或推进第三步；原第二步错误记录仍保留。

审查补充：缺失或无法解码的原文按材料返回 read_error，不中断整个运行；新增缺失文件回归测试，总计58项通过。
