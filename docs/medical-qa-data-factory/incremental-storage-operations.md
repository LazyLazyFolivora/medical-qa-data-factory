# 增量运行存储使用说明

## 统一存储

Web、持续生产和 CLI 的运行状态统一写入 PostgreSQL，较大的产物内容写入文件。没有旧 JSON 存储后端、自动回退或历史导入工具。

每个 run 的 `objective_runtime/storage.json` 记录 run ID、数据库 schema 和正文目录位置，不保存连接密码。数据库保存当前状态和增量事件。不要手工删除该标识。

旧 run 不再支持加载、历史查看或断点恢复；原文件不会自动删除。切换时停止旧后端，使用新工作区开始新批次，避免尝试恢复旧的生产批次。

## 初始化

在项目根目录、原有 Python 环境中配置 `MEDICAL_QA_PRODUCTION_DATABASE_URL`，然后为新工作区初始化表：

```bash
python -m medical_qa_data_factory web migrate-production \
  --workspace /data2/medical-qa/production-v2
```

Web 启动时使用相同工作区。独立 CLI 也需要先对其 `--workspace` 执行上述数据库初始化。

SQL 迁移创建数据库表，不迁移旧任务文件。

## 文件位置

Web 新建任务的正文默认存于工作区下的 `runtime_content`，可在启动进程前指定：

```bash
export MEDICAL_QA_RUNTIME_CONTENT_ROOT=/data2/medical-qa/runtime_content
```

该变量不自动从项目 `.env` 加载，应配置在进程或服务环境中。既有任务继续使用自身标识记录的位置，修改变量不会搬迁文件。

此目录只覆盖运行产物正文。调试日志、原始材料缓存、全文下载和发布导出仍在各自目录。PostgreSQL 的数据盘也需单独配置。

数据库与正文文件必须一起备份。当前不启用自动清理，不应手动删除仍被运行事件引用的正文。

## 测试

运行存储相关测试需要设置 `MEDICAL_QA_TEST_POSTGRES_URL`。测试使用临时 schema，结束后删除；没有配置时明确跳过数据库测试，不使用旧文件存储替代。

```bash
python -m pytest tests/test_incremental_runtime_storage.py \
  tests/test_generation_runtime_safety.py \
  tests/test_objective_web.py -q
```
