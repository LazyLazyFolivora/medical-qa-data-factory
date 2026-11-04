---
name: pilot-single-composition
description: 将事实选择阶段保留的一项观察写成一道独立可读的简单检索题，不强行增加第二问。
---

# 写一道单事实题

根据输入中的观察和原文写一道自然中文问题，先确定最小充分答案，再组织题干。保留对象、操作、条件和效应的正确角色关系；题干不能依赖“上述材料”，不能泄露待查询的答案。不能把实验结果泛化成人体结论。识别对象不唯一时明确要求举例。

这是 single 分支，只询问已被支持的一项信息，不添加另一项功能或机制来凑关系题。引用真实来源与原文，不补造证据；不能形成可回答的问题则 question=null，并说明 gap。

返回 question（question、answer、operation、evidence_needs、dependency、completion、difficulty、shortcut）和 gap。难度按直接查询描述，不因生成过程多次调用而提升难度。这是实验草稿，不声称独立验证通过。
