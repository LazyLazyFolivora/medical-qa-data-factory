# 完整出题实验 CLI

现在可以输入一个实体，连续运行检索、机会选择、定向补查、事实选择和写题。
当前输入范围是**人类基因符号**，如 PNMA1；每次尝试产出一道题。
该入口用于方法测试，不自动发布或写入训练数据集。

## 运行

在 PowerShell 中：

```powershell
cd E:\Cursor\assumption\projects\medical-qa-data-factory
$env:PYTHONPATH = "src"
$env:PYTHONIOENCODING = "utf-8"

D:\Anaconda\envs\qa_env\python.exe -m medical_qa_data_factory.question_pipeline_pilot `
  --entity PNMA1 `
  --output data/experiments/my-pnma1-run-01 `
  --provider qwen-self-hosted
```

使用本地 PrimeKG 图谱时，增加：

```powershell
  --graph E:\Cursor\assumption\datasets\PrimeKG\primekg.sqlite
```

例如完整命令：

```powershell
D:\Anaconda\envs\qa_env\python.exe -m medical_qa_data_factory.question_pipeline_pilot --entity PNMA1 --graph E:\Cursor\assumption\datasets\PrimeKG\primekg.sqlite --output data/experiments/my-pnma1-graph-run-01 --provider qwen-self-hosted
```

将 `--entity` 换成另一个人类基因符号即可探索其他实体。`--output` 必须是新目录，
重复测试时换目录名；默认从项目 `config/models.json` 读取模型配置，可用 `--config` 指定其他配置。
需要能访问配置的模型服务、UniProt 和 PubMed。传入图谱参数才会读取图谱；图谱边只是检索线索。

## 实际流程与冻结范围

1. 查询种子实体的 reviewed human UniProt 记录；指定图谱时读取邻域。
2. 使用现有 `pilot-entity-exploration` 提出最多两项检索，读取摘要；无摘要时退回实体关键词查询。
3. 使用现有 `pilot-focused-opportunity` 选择一个机会和补查对象，执行一次 UniProt 定向补查。
4. **从冻结目录读取** `pilot-fact-pair`，选择观察与新增事实。
5. pair 使用**冻结的** `pilot-focused-composition` 写题；single 使用新增的 `pilot-single-composition`，
   只问一项事实；insufficient 或缺少必要资料时输出缺口。

基线目录：`docs/medical-qa-data-factory/baselines/fact-pair-v1/`。
启动时核对两份冻结 skill 的 SHA-256，文本发生变化则报错。不会自动改回工作目录中的提示。
**冻结的是事实选择与关系题写作，不是所有前置检索和机会选择，也不包括新加的 single 分支。**
每步实际 skill 全文、版本和输入保存在 request 文件中。

选题解释 information_gain/reader_need 不作为事实依据传入写作者。
物种等元信息按来源分别提供，不因补查为人类条目就断言其他文献也研究人类。
此处是数据交接约定，不代表模型必然遵循这些边界。

## 查看结果

- `review.md`：题目、参考答案、依据和缺口。
- `result.json`：结构化最终结果。status 为 `draft`、`needs_material` 或 `error`。
- `source.json`、`exploration-materials.json`、`exploration.search-*.json`、`articles.json`：种子资料及检索记录。
- `selection.result.json`、`lookup.raw.json`：机会与补查资料。
- `fact-pair.result.json`、`writing-materials.json`：实际选择的事实及写作交接。
- 各阶段 `*.request.json`、`*.result.json`：完整模型调用记录。
- `run.json`：实体、图谱路径及使用的基线位置。

`draft` 仅表示生成了文本，不表示通过事实或难度验证。
`needs_material` 是未能形成题目的正常实验结果，原因会保留。
运行异常会尽量保存 `error` 结果，再以非零退出码退出；参数或配置错误可能发生在创建输出目录之前。

## 当前验证

已完成带 PrimeKG 的 PNMA1 实跑，保存于 `data/experiments/pnma1-cli-pipeline-01/`。
完整流程能生成草稿，但这次机会选择退回 PNMA1 自身；输出仍擅加人类条件并将推测机制确定化。
这份结果不能直接作为合格题收入数据集，也未通过修改冻结提示掩盖问题。

无图谱的完整运行也已完成，保存于 `data/experiments/pnma1-cli-pipeline-02/`。
该次生成了生殖功能相关题，但把 PNMA1 与 PNMA4 各自自组装的描述写成两者共同组装，
并进一步暗示该组装就是维持生殖寿命的确定机制；来源还只标为 abstract。
因此两次运行只证明串联可执行，不证明出题质量。最终仍需阅读原始资料和中间结果。
两次实际请求中的核心 skill 文本均与冻结副本一致（请求契约会去除首尾空白）。

另有无网络的流程测试覆盖冻结文件读取、pair/single/insufficient 分支、事实交接、
从种子到写作的串联以及不将选题解释传给写作者。真实质量需结合保存的中间结果继续研究。
本轮共 8 项相关测试通过，单事实 skill 格式检查通过。
