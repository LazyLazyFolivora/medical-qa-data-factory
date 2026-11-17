# DeepSeek 对照试验（2026-11-12）

同一 TP53 已读段落、同一 select→write 提示，3组共6次调用。模型使用现有 deepseek-official 配置：deepseek-flash、reasoning_effort=none、context_window_tokens=1000000、输出4096。官方当前接口名参见 https://api-docs.deepseek.com/en/ 。旧 V4 Flash 名称不应被当作固定版本保证。

输入每次约411—1664 tokens，故仅确认使用1M容量配置，没有测试百万token输入能力。context_window_tokens是本地预算配置，原始API调用不发送虚构的context_window参数。实验替换模型，没有切换全局默认Qwen。

运行：项目根目录 python design/retrieval-coverage-mvp/locked_need_probe.py --deepseek 。输出目录 data/experiments/tp53-locked-need-deepseek-20261112，保留请求、响应、解析结果与窗口元数据；不保存认证头。密钥读取现有.env.deepseek，兼容变量赋值及纯密钥格式，不改写密钥文件。

三组都选p53结构域组成：第一题限定四聚体单体，第二题仅问单体结构域，第三题同时问作用形式和五个结构域。第二题最聚焦：

> p53蛋白单体由哪些结构域组成？

答案列TAD、PRD、DBD、OD、RD，依据E1原文列举。本轮未观察到Qwen之前的基因/蛋白历史替换或跨到治疗建议的情况，但第三题仍比单一列举多一个求知点。

三次是近重复，不能报成三个独立样例；模型与tokenizer、请求参数不同，样本量小，不能断言全面优于Qwen。当前只支持“同材料下输出更聚焦”的局部判断。尚未测试DeepSeek从实体自动检索到出题的完整流程，也未发布或作为选择器训练轨迹。
