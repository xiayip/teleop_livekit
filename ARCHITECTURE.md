# LiveKit-ROS2 Bridge Architecture

## 架构概述

代码已重构为分层架构，将**通用基础设施**与**业务逻辑**解耦：

```
┌─────────────────────────────────────────┐
│      teleop_manager.py                  │  ← 生命周期管理
│  (Lifecycle management)                 │
└──────────────┬──────────────────────────┘
               │
               ↓
┌─────────────────────────────────────────┐
│       teleop_bridge.py                  │  ← 业务逻辑层
│  (Business logic: specific topics,     │     - 特定话题订阅
│   filtering, throttling, change detect) │     - 过滤/节流/变化检测
└──────────────┬──────────────────────────┘     - 远程操控逻辑
               │
               ↓ (inherits)
┌─────────────────────────────────────────┐
│   livekit_ros2_bridge_base.py          │  ← 通用基础设施层
│  (Generic bidirectional bridge)        │     - ROS2 ↔ LiveKit 转换
│   - Message serialization              │     - 动态 publisher/subscriber
│   - Dynamic pub/sub creation           │     - Service/Action 客户端
│   - Service/Action clients             │     - 协议层抽象
└─────────────────────────────────────────┘
```

## 模块说明

### 1. `livekit_ros2_bridge_base.py` - 通用基础设施

**职责**：提供 ROS2 与 LiveKit 之间的通用双向通信能力

**核心功能**：
- `ROS2MessageFactory`: 消息类型动态加载、JSON 序列化/反序列化
- `LiveKitROS2BridgeBase`: 基类提供
  - LiveKit 数据包接收和路由
  - 动态创建 ROS2 publisher/subscriber
  - Service 和 Action 客户端管理
  - 异步消息发送到 LiveKit
  - **大数据包分片传输**：`publish_large_payload` / `publish_large_payload_async`
    - 自动将大数据拆分为小块（默认 50KB）
    - 元数据同步用于重组
    - 线程安全，支持回调

**特点**：
- 完全通用，不包含任何业务逻辑
- 可被任何需要 LiveKit-ROS2 桥接的应用继承
- 协议无关，支持任意 ROS2 消息类型
- 内置大数据传输能力，突破 LiveKit 16KB 限制

### 2. `teleop_bridge.py` - 远程操控业务逻辑

**职责**：实现特定的远程操控功能

**核心功能**：
- **视频流**：`image_callback` - 图像转 LiveKit 视频轨道
- **点云流**：`compressed_pointcloud_callback` - 使用基类大数据包分片发送
- **机器人状态反馈**：
  - `odometry_callback` - 位姿反馈（带变化检测）
  - `joint_state_callback` - 关节状态反馈（带过滤和变化检测）

**优化策略**：
- **时间节流**：最大 5 Hz 更新频率
- **变化检测**：只在数据变化超过阈值时发送
  - 位姿：位置 1mm，姿态 0.01
  - 关节：0.001 rad (~0.057°)
- **数据过滤**：关节状态只发送 joint1-6
- **大数据处理**：点云数据使用 50KB 块分片传输

**特点**：
- 继承自 `LiveKitROS2BridgeBase`
- 业务逻辑集中，易于维护和扩展
- 独立的阈值和策略配置
- 利用基类大数据包能力处理点云

### 3. `teleop_manager.py` - 生命周期管理

**职责**：管理整个系统的启动/停止流程

**核心功能**：
- LiveKit 房间连接
- ROS2 初始化
- 创建 `TeleopBridge` 实例
- 管理 ROS2 spinning 线程
- 优雅关闭

**特点**：
- 清晰的启动/停止流程
- 线程安全的资源管理
- 提供 main 入口点

## 使用方式

### 作为独立节点运行

```bash
ros2 run teleop_livekit teleop_bridge <livekit_url> <livekit_token>
```

### 在代码中使用

```python
from teleop_livekit.teleop_manager import TeleopManager

async def my_app():
    manager = TeleopManager(url, token)
    await manager.start()
    # ... your code ...
    await manager.stop()
```

### 扩展自定义业务逻辑

```python
from teleop_livekit.livekit_ros2_bridge_base import LiveKitROS2BridgeBase

class MyCustomBridge(LiveKitROS2BridgeBase):
    def setup_subscriptions(self):
        # 订阅你需要的话题
        self.my_sub = self.create_subscription(
            MyMsg, '/my_topic', self.my_callback, 10
        )
    
    def my_callback(self, msg):
        # 你的业务逻辑
        self.publish_to_livekit({
            'packetType': 'my_custom_packet',
            'data': ...
        })
```

## 架构优势

### 1. **关注点分离**
- 基础设施层不关心具体业务
- 业务层专注于远程操控逻辑
- 管理层处理生命周期

### 2. **可扩展性**
- 新的业务场景：继承 `LiveKitROS2BridgeBase`
- 新的数据源：在子类中添加订阅
- 新的优化策略：在子类中实现

### 3. **可测试性**
- 基础层可独立测试序列化/反序列化
- 业务层可 mock 基类测试逻辑
- 清晰的边界便于单元测试

### 4. **可维护性**
- 每个模块职责明确
- 修改业务逻辑不影响基础设施
- 代码组织清晰，易于理解

## 迁移指南

### 从旧代码迁移

**旧代码**（`livekit_ros2_bridge.py`）：
- 所有逻辑混在 `LiveKitROS2Bridge` 类中

**新代码**：
1. 通用功能 → `livekit_ros2_bridge_base.py`
2. 远程操控逻辑 → `teleop_bridge.py`
3. 启动管理 → `teleop_manager.py`

**主要变化**：
- `LiveKitROS2Bridge` → `TeleopBridge`
- `LiveKitROS2BridgeManager` → `TeleopManager`
- 基类功能抽取到 `LiveKitROS2BridgeBase`

## 大数据包传输能力

LiveKit data channel 对单个数据包有大小限制（~16KB）。基础层提供了**自动分片传输**功能：

### 核心 API

```python
# 方式 1: 从 ROS 回调调用（推荐）
self.publish_large_payload(
    payload=bytes_data,
    topic="my_topic",
    message_id=123,
    chunk_size=50000,  # 50KB chunks
    on_done=callback    # 完成回调
)

# 方式 2: 在 async 函数中使用
await self.publish_large_payload_async(
    payload=bytes_data,
    topic="my_topic",
    chunk_size=50000
)
```

### 使用场景

| 数据类型 | 典型大小 | 是否需要分片 | 推荐块大小 |
|---------|---------|-------------|-----------|
| 小消息 (JSON) | <10KB | ❌ 用 `send_feedback` | N/A |
| 压缩图像 | 100-500KB | ✅ | 50KB |
| 压缩点云 | 100KB-1MB | ✅ | 50KB |
| 大批量数据 | >1MB | ✅ | 100KB |

### 传输协议

- 元数据发送到 `<topic>:meta`：`{id, chunk, total, size}`
- 数据块发送到 `<topic>`
- 接收端根据元数据重组

详细文档：参见 [LARGE_PAYLOAD.md](LARGE_PAYLOAD.md)

## 文件对应关系

| 旧文件 | 新文件 | 说明 |
|-------|--------|------|
| `livekit_ros2_bridge.py` | `livekit_ros2_bridge_base.py` | 通用基础设施（含大数据包） |
| (同上) | `teleop_bridge.py` | 业务逻辑 |
| (同上) | `teleop_manager.py` | 生命周期管理 |

## 后续优化建议

1. **配置化**：将阈值、频率等参数移到配置文件或 ROS2 参数
2. **插件化**：将不同的数据流（视频、点云、状态）抽象为插件
3. **监控增强**：添加更详细的统计和性能监控
4. **错误处理**：增强异常处理和恢复机制
