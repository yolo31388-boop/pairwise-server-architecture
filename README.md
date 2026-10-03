# 服务器架构与负载均衡系统

## 项目描述

服务器架构与负载均衡系统的bug修复项目。核心模块 `network/server_architecture.py` 存在多个已知bug，需要修复后使测试全部通过。

## 已知问题

- 区域路由只查IP不查延迟和负载
- 负载均衡只查人数不查CPU内存带宽
- 会话迁移不迁移数据且无超时
- 故障转移无健康检查无降级方案
- 跨区对战无延迟补偿无数据同步

## 项目结构

```
├── network/server_architecture.py       # 核心模块（带bug）
├── tests/test_server_architecture.py    # 验收测试（红态）
├── pyproject.toml          # 项目配置
└── README.md               # 说明文档
```

## 运行测试

```bash
python -m pytest tests/test_server_architecture.py -q
```

修复目标：所有 16 个测试全部通过。

## 技术要求

- 纯Python标准库，不引入第三方依赖
- 不修改测试文件
- 保持现有API接口不变
- 修复所有已知bug
