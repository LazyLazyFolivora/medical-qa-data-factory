# 生产工作台视觉方向

采用用户确认的 Apple 风格视觉稿，参考 awesome-design-md 的 apple.md；针对数据工作台调整，不照搬营销页。

- 白色批次概览与 `#f5f5f7` 工作区分层，取消统计卡片外框与垂直分隔线。
- `#1d1d1f` 正文，`#6e6e73` 次要信息，`#0066cc` 操作色。
- 系统字体，中文使用 PingFang SC / Microsoft YaHei。正文与标题默认 400 字重，通过字号、留白和对齐表达层级。
- 分组设置及执行表格使用 18px 圆角，无装饰阴影；主要按钮使用胶囊形。
- 表格保留语义表头、状态文字、实体链接、错误信息及键盘焦点。窄屏在表格内部横向滚动。
- `desktop.png`、`mobile.png` 为实际 Vue 页面截图，使用模拟批次数据，不代表真实生产状态。

实现：`frontend/src/ProductionApp.vue` 与 `frontend/src/production.css`。样式限制在生产页面，不影响单实体研究工作台。
