# 正文获取失败默认复用记录

G6PD 固定版本运行 `g6pd-20261115-043248` 对 PMID27081765 发起六次全文获取（一次与另一篇批量，五次单独）。失败诊断包含解析、授权和访问限制；研究模型仍重复调用，未形成新证据。

read_source_details 现在在全部请求来源均有既往 not_acquired 记录、且尚无保存全文时，默认返回 prior_failure_ids，并明确 cached_failure=true、network_attempted=false、provider_calls=0。主任务继续；模型可以查看诊断、换论文/来源，或填写 retry_reason 再联网尝试。没有将失败推断为全文永久不存在。新来源、混合批次、已有全文等其他路径不变。

Details 参数 schema 与工具说明同步。代码审查通过。用实际 G6PD 保存状态调用工具，提供空 provider 映射验证不进入网络分支，返回了五条既往失败引用；结果保存于 `fulltext-failure-cache-20261115/actual-state-replay.json`。此检查不使用模型，不能证明模型会自主改变研究方向。

当前 G6PD 活跃进程使用修改前的代码，不打断运行。待终止后再验证新行为。这里只解决重复联网成本，不把缓存命中当成问答质量改善或目标达成。
