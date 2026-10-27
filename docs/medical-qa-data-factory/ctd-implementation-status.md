# CTD 正式接入实施状态

> 更新日期：2026-10-27  
> 状态：Step 1-7 已实现并通过自动化回归；Step 8 真实 pilot 尚未通过。

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
- 六类 intent 的确定性题面和语义回环；
- CTD 定向测试、默认回归和 PrimeKG 回归。

尚未完成：

- 真实 CTD disease identity crosswalk；
- 真实疾病类别 taxonomy snapshot；
- 20 个真实 seed 的 PubMed fusion corpus；
- 至少 5 个真实可发布正例；
- 全部 pilot 题的人工盲审；
- 由真实运行生成的质量报告。

## 2. 自动化事实

实现提交截至：

```text
eac5a5a feat: realize and verify CTD-backed questions
```

最终验证：

```text
939 passed, 13 deselected
13 passed, 939 deselected    # primekg_integration
```

核心新增模块覆盖率：

```text
ctd_world_evaluation.py   81%
task_candidates.py        90%
task_compiler.py          88%
task_validation.py        88%
task_service.py           92%
task_release.py           96%
task_realization.py       87%
```

覆盖率使用标准库 `trace` 对相关单元、集成和回归测试统计；默认测试环境未安装
`pytest-cov`。

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

### 5.1 Disease identity

当前 CTD disease ID/name 尚未绑定到权威 canonical disease identity。

没有 crosswalk 时：

- alias 合并无法证明；
- 同名异病无法安全区分；
- candidate answer 无法与下游 canonical ID 对齐；
- 完整竞争集不能进入正式发布。

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

## 6. 恢复 Step 8 的必要输入

运行真实 pilot 前必须具备：

1. 版本化 CTD disease crosswalk；
2. 版本化 disease category taxonomy；
3. 授权的模型配置和 PubMed provider；
4. 20 个 seed 的 pinned PubMed corpus；
5. 每个 seed 的 `CtdTaskRunSpec`；
6. 医学审阅者和盲审记录。

输入齐备后按以下顺序执行：

```powershell
# 1. 为每个 seed 生成并持久化 Step 4 fusion
# 2. 写出 CtdTaskRunSpec

python -m medical_qa_data_factory generate validated `
  --profile ctd `
  --ctd-index data/cache/ctd-evidence.sqlite `
  --ctd-run-spec data/runs/ctd-pilot/<seed>/run-spec.json `
  --output data/runs/ctd-pilot/<seed>/release

# 3. 汇总候选漏斗、拒绝码、intent/polarity/qualifier 分布和模板占比
# 4. 对全部发布题执行医学盲审
# 5. 只有 P0 全部通过后才修改本状态为“已完成”
```

