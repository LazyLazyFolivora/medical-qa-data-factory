# CTD 正式接入实施状态

> 更新日期：2026-10-27  
> 状态：CTD-only Step 1-7 与 PrimeKG+CTD+PubMed 基础联合纵切已实现；
> 真实 crosswalk 和 Step 8 pilot 尚未通过。

## 1. 结论

当前不能写“CTD 正式接入已完成”，也不能把本文档当作产品验收通过证明。

已经完成：

- CTD 完整候选枚举和固定快照读取；
- PubMed 摘要、corpus manifest、字符 span、quote hash 和独立回读；
- Step 4 证据融合与生物医学语义门禁；
- 完整疾病竞争集评估和未知竞争者阻断；
- study-context qualifier 判别价值与 shortcut 检查；
- `ValidatedTask`、requirement matcher、promotion 和 evidence dependency digest；
- CTD service、`generate validated --profile ctd`、public/hidden release；
- PrimeKG canonical answer + 完整 PrimeKG 竞争集 + CTD/PubMed 联合服务；
- 版本化 PrimeKG/CTD chemical/disease identity snapshot 生成器；
- qualifier 逐项原文证据、public qualifier 反事实和题面硬门禁；
- Prompt Writer、独立语义 Verifier 和联合 public/hidden release；
- `generate primekg-ctd-pubmed` 单候选正式入口；
- 六类 intent 的确定性题面和语义回环；
- CTD 定向测试、默认回归和 PrimeKG 回归。

尚未完成：

- 真实疾病类别 taxonomy snapshot；
- 20 个真实 seed 的 PubMed fusion corpus；
- 至少 5 个真实可发布正例；
- 全部 pilot 题的人工盲审；
- 由真实运行生成的质量报告。

### 1.1 联合纵切当前边界

联合服务要求：

```text
PrimeKG source=CTD 候选边
  -> 枚举同一 public query 下全部 PrimeKG disease answers
  -> 每个 answer 解析 CTD disease crosswalk
  -> 每个 answer 读取 CTD direct records 和全部 PMID
  -> 每个 PMID 生成可回读 span/assessment
  -> 每个 public qualifier 指向 citation quote 内的逐字证据
  -> 完整 competition report
  -> 删除每个 public qualifier 后重算答案集
  -> ValidatedTask
  -> Prompt 生成自然题面
  -> 独立 verifier 回读题面语义
```

任一 candidate 出现 `ambiguous / unmapped / unreadable / provider_failed /
not_evaluated`，或出现两个 `matched` answer，都不会发布。正式答案始终保留
PrimeKG canonical ID，CTD disease ID 只进入 hidden lineage。

当前 `marker/mechanism` 对 disease answer 的机制题仍按 fail-closed 拒绝；第一版
只允许 therapeutic research disease 和 association。机制题后续必须把答案世界
扩展到 PrimeKG gene/pathway 等机制实体，不能把“有机制证据的疾病”当作机制答案。

### 1.2 Source-derived identity snapshot

当前已生成一个可从固定输入重建的身份快照：

```text
data/cache/primekg-ctd-identity.snapshot.json
data/cache/primekg-ctd-identity.snapshot.json.audit.jsonl
```

构建命令：

```powershell
python -m medical_qa_data_factory evidence ctd build-identity `
  --index data/cache/ctd-evidence.sqlite `
  --primekg-database E:/Cursor/assumption/datasets/PrimeKG/primekg.sqlite `
  --output data/cache/primekg-ctd-identity.snapshot.json
```

方法版本为 `primekg_ctd_source_exact_alias_v1`。它只接受同一冻结
PrimeKG CTD source edge 与 CTD record 的 exact normalized name 匹配；同名对应
多个 CTD ID 时标为 ambiguous 并排除。它不是外部 ontology gold，也不能用模糊名称
扩充映射。

本次真实快照：

```text
PrimeKG snapshot: bd22ae20ed3037e2d4bb518904406143
CTD snapshot:     ctd:95fdff4ae428f880
snapshot SHA256:  aef560df6e252aa0edf62a50a50f660054dde5136feda43a98d946edbd404352
chemical maps:    459
disease maps:     108
CTD edges:        837 mapped_exact, 1467 unmapped
fully mapped chemicals for all PrimeKG exposure answers: 76
```

“fully mapped”只说明 identity closure 成立，不说明这些 chemical 的 CTD/PubMed
证据能够形成可发布题。每道题仍需经过 direct evidence、全部 PMID、qualifier
counterfactual 和题面语义门禁。

## 2. 自动化事实

实现基线：

```text
b1d0892 feat: add PrimeKG CTD PubMed evidence foundation
+ 当前 PrimeKG/CTD/PubMed 联合 task service、release 与 CLI
```

最终验证：

```text
980 passed, 13 deselected
13 passed, 980 deselected    # primekg_integration
```

默认测试环境未安装 `pytest-cov`，本文不声明当前分支的精确行覆盖率。联合服务的
正例、双答案、mapping unknown、模型/provider failure、PubMed snapshot mismatch、
答案泄漏、PrimeKG source 伪造和 public/hidden 发布均有定向测试。

## 3. 真实 CTD 快照

本地固定索引：

```text
data/cache/ctd-evidence.sqlite
```

manifest：

```json
{
  "schema_version": "ctd-evidence-index-v2",
  "snapshot_id": "ctd:95fdff4ae428f880",
  "source_sha256": "95fdff4ae428f8802903d9c9fc16e97a01548c0ba2dd84d710cefdb2b3621dfe",
  "source_size": 163603578,
  "rows_processed": 9903450
}
```

## 4. 20-seed preflight

以下 seeds 来自真实 CTD 索引，按 disease record 数降序、chemical ID 确定性排序。
这里的计数只说明可枚举规模和 direct/inference-only 分布，不表示这些问题已经通过验收。

| Chemical ID | Name | Disease records | Direct rows | Inference-only rows |
|---|---|---:|---:|---:|
| C006780 | bisphenol A | 5824 | 259 | 33000 |
| D001564 | Benzo(a)pyrene | 5559 | 114 | 31639 |
| D013749 | Tetrachlorodibenzodioxin | 5480 | 166 | 31150 |
| C017947 | sodium arsenite | 5394 | 182 | 31035 |
| D014635 | Valproic Acid | 5383 | 386 | 29564 |
| D000082 | Acetaminophen | 5026 | 198 | 28514 |
| D019256 | Cadmium Chloride | 4891 | 128 | 27973 |
| C543008 | bisphenol S | 4799 | 48 | 26401 |
| D004958 | Estradiol | 4788 | 183 | 27629 |
| D000393 | Air Pollutants | 4716 | 153 | 26439 |
| D052638 | Particulate Matter | 4708 | 244 | 27302 |
| D016604 | Aflatoxin B1 | 4665 | 27 | 26102 |
| C009495 | titanium dioxide | 4632 | 48 | 25861 |
| D014028 | Tobacco Smoke Pollution | 4480 | 154 | 25332 |
| D000431 | Ethanol | 4323 | 290 | 24477 |
| C076994 | perfluorooctane sulfonic acid | 4310 | 79 | 24180 |
| D004997 | Ethinyl Estradiol | 4303 | 135 | 22986 |
| D014212 | Tretinoin | 4251 | 239 | 23845 |
| D013853 | Thioacetamide | 4192 | 55 | 22573 |
| D001151 | Arsenic | 4154 | 193 | 23177 |

可复现查询：

```sql
SELECT chemical_id,
       MIN(chemical_name) AS chemical_name,
       COUNT(DISTINCT COALESCE(disease_id, normalized_disease_name))
         AS disease_records,
       SUM(CASE WHEN direct_evidence <> '' THEN 1 ELSE 0 END)
         AS direct_rows,
       SUM(CASE WHEN direct_evidence = '' THEN 1 ELSE 0 END)
         AS inference_only_rows
FROM ctd_records
GROUP BY chemical_id
HAVING direct_rows > 0
ORDER BY disease_records DESC, chemical_id
LIMIT 20;
```

```powershell
sqlite3 -readonly -header -column data/cache/ctd-evidence.sqlite "<上面的 SQL>"
```

## 5. Step 8 阻塞项

### 5.1 Disease identity residual risk

source-derived exact-alias snapshot 已解决 108 个 disease 的稳定绑定，并在
2304 条 source edges 上留下完整 audit。剩余问题不是名称匹配算法，而是：

- 1467 条仍 unmapped，相关 chemical 不会进入发布；
- 同一 PrimeKG disease 跨 edge 若出现冲突映射，当前规则会排除；
- 该快照需要产品/数据负责人确认其 identity policy 是否满足正式 gold 要求；
- 疾病类别仍缺独立 taxonomy，不能从模型自由生成。

### 5.2 Disease category

CTD 当前文件没有直接提供本项目所需的稳定疾病分类。不能根据疾病名称字符串猜测类别。

### 5.3 PubMed fusion corpus

真实 pilot 需要为全部竞争项生成并持久化：

- PubMed 固定 corpus；
- 每个 PMID 的独立 read-back span；
- 支持、反对、context-only、uncertain 状态；
- Step 4 eligible/rejected claim。

当前仓库没有这 20 个 seed 的真实 fusion run spec。

### 5.4 人工盲审

人工盲审需要产品/医学审阅者完成，不能由自动化测试替代。

### 5.5 真实 smoke 结果

已用 Dicamba `D003996` → Parkinson disease 运行一次真实 PubMed + 结构化模型
smoke。PubMed 成功读取；isolated target assessment 被
`pubmed_quote_mismatch` 拒绝，完整竞争运行也没有发布题目，并保留了
`provider_failed / unknown_competitor` rejection。

这次结果证明链路不会把网络成功或模型返回结构合法误当成 gold。它是失败 smoke，
不是 pilot 通过，也不能作为真实正例。

## 6. 恢复 Step 8 的必要输入

运行真实 pilot 前必须具备：

1. 已生成 identity snapshot，待负责人确认 policy；
2. 版本化 disease category taxonomy；
3. 授权的模型配置和 PubMed provider；
4. 20 个 seed 的 pinned PubMed corpus；
5. 每个 seed 的 `CtdTaskRunSpec`；
6. 医学审阅者和盲审记录。

输入齐备后按以下顺序执行：

```powershell
# 1. 为每个 seed 生成并持久化 Step 4 fusion
# 2. 写出 CtdTaskRunSpec

python -m medical_qa_data_factory generate primekg-ctd-pubmed ...

# 3. 汇总候选漏斗、拒绝码、intent/polarity/qualifier 分布和模板占比
# 4. 对全部发布题执行医学盲审
# 5. 只有 P0 全部通过后才修改本状态为“已完成”
```

