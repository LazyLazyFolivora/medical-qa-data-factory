# 当前验证契约

1. blind_solve：公开问题与独立验证引文，不含候选答案和发现锚点。输出答案、uncertain，以及可选 evidence_need。
2. compare：比较独立结果与候选答案，传入完整公开问题范围。
3. counterexample_review：审查问题、候选答案、独立结果与验证引文；输出直接支持、矛盾、竞争答案、是否直接蕴涵答案，以及可选 evidence_need。
4. VERIFIED 冻结语义契约；REJECTED 终止候选。INDETERMINATE 有具体缺口且局部预算允许时，写入 verification_gap，定向补证后重新验证；否则终止候选。

仍有限制：来源独立性主要通过发现 anchor ID 排除；完整性依据由目标策略提供；竞争答案的单答案/举例/集合区别尚需进一步完善。不得把这些字段当成已经完成的语义证明。
