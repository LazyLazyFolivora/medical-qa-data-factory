# 全文下载器配置与使用

已接入 `computron/fulltext-article-downloader` 0.2.4，源码固定在 commit
`8e5251e82b6721840fa0dcb052b576406bd9be78`。

## 当前机器

- 源码：`data/experiments/fulltext-downloader-evaluation/repo`
- 隔离环境：`data/experiments/fulltext-downloader-evaluation/venv`
- 本地配置：`config/fulltext.json`（已启用，Git 忽略）
- 下载和日志：`data/cache/fulltext-downloads/`，每次调用独立目录。
- 不修改主环境依赖，不使用浏览器 cookies，不安装 TLS 模拟扩展。

修改配置后下一次正文调用会重新读取，无需重建环境。配置路径也可通过
`MEDICAL_QA_FULLTEXT_CONFIG` 指定；相对可执行文件和输出路径以项目根目录为基准。

## 填哪些配置

打开 `config/fulltext.json` 的 `credentials`，在双引号中填写；不要把密钥写进 example 文件。

| 字段 | 用途 | 获取方式 |
|---|---|---|
| UNPAYWALL_EMAIL | Unpaywall 开放副本检索、Crossref 联系邮箱 | 填自己的真实邮箱，无需购买 key |
| ELSEVIER_API_KEY | Elsevier 正文 API | https://dev.elsevier.com |
| SPRINGER_API_KEY | Springer 开放全文 XML | https://dev.springernature.com |
| WILEY_API_KEY | Wiley TDM | https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining |
| SEMANTIC_SCHOLAR_API_KEY | Semantic Scholar 查询额度及相关检索 | https://www.semanticscholar.org/product/api |

都可以留空，但相应渠道会不可用或受匿名额度限制。优先填邮箱，再按实际失败渠道申请 key。
出版社 key 不等于订阅访问权限。未填写的字段可由同名环境变量提供。
当前配置不包含 OpenAlex key：此版本下载器的这些配置项中没有它。

`executable` 指向 fulltext-download 可执行文件；`pdftotext` 指向 Poppler 的 pdftotext。
`timeout_seconds` 是每次下载子进程的总上限（默认180秒），不包括前面的 PMC 请求和后面的文本解析。
设 `enabled: false` 可恢复只用原有全文渠道。

## Agent 如何使用

主 agent 仍选择 `read_source_details`；流程为：

1. 原有 PubTator、PMC BioC、Europe PMC 正文获取。
2. 若 Europe PMC 获取失败且配置启用，按 PMID 查询 DOI。
3. 调用隔离环境的下载器，由其按出版社和来源自动回退。
4. PDF 用 pdftotext 解析；JATS XML 用原有章节解析器处理。
5. 保存新的正文材料和引用，供 `read_artifacts` 选段阅读。

失败会带渠道错误及 trace 目录返回；成功的 provider_result.usage.fulltext_downloads
保留 DOI、文件、来源、版本说明和 trace 目录。下载器的身份检查不是完整性保证，
扫描PDF没有文本层时明确报错；尚不支持OCR和所有出版社XML方言。XML不符合现有JATS解析要求时不会假装成功。
当前不做跨调用缓存复用，以免并行任务写坏同一文件；每次产物保留供审计。

## 单独测试

在项目根目录 PowerShell：

```powershell
$env:PYTHONPATH='src'
$env:PYTHONUTF8='1'
python -c "from medical_qa_data_factory.generation.fulltext_download import download_article; r=download_article('10.1371/journal.pone.0000308'); print(r['download_provenance']); print(len(r['text']))"
```

此方式读取项目的 fulltext.json。直接使用第三方 CLI **不会**读取这个项目配置；其原生配置为环境变量或 `~/.fulltext_keys`。

新机器需克隆同一 commit、创建独立 venv、`python -m pip install <源码目录>`，
安装 Poppler 后从 fulltext.example.json 复制本地配置并修改路径。不要提交凭据、venv和下载论文。

## 本次验证

23个适配器及既有正文相关测试通过。真实调用项目适配器下载 PLOS 对照成功，
解析出52787字符、9个索引片段；未推进原单步调试任务。
