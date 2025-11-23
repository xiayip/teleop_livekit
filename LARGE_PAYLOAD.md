# 大数据包分片传输功能

## 概述

LiveKit 的 data channel 对单个数据包有大小限制（通常 ~16KB）。对于大数据（如点云、高分辨率图像），直接发送会失败。

基础设施层 (`livekit_ros2_bridge_base.py`) 提供了**自动分片传输**能力，将大数据包拆分为小块发送，接收端可以重组。

## 功能特性

### ✅ 自动分片
- 自动将大数据拆分为可配置大小的块（默认 50KB）
- 每个块独立发送到 LiveKit data channel

### ✅ 元数据同步
- 每个块发送前先发送元数据（message ID、块索引、总块数）
- 接收端可以追踪和重组数据

### ✅ 线程安全
- 可以从 ROS 回调（任意线程）安全调用
- 自动提交到 asyncio 事件循环执行

### ✅ 异常处理
- 支持完成回调，可以捕获发送错误
- 提供同步和异步两种 API

### ✅ 可配置
- 块大小可配置
- 支持自定义 message ID
- 支持自定义 topic 名称

## API 文档

### 方法 1: `publish_large_payload` (推荐)

**用于 ROS 回调等同步上下文**

```python
def publish_large_payload(
    self,
    payload: bytes,
    topic: str,
    message_id: Optional[int] = None,
    chunk_size: int = 50000,
    on_done: Optional[Callable[[Optional[Exception]], None]] = None
)
```

**参数**：
- `payload`: 要发送的二进制数据
- `topic`: LiveKit data channel 主题名称
- `message_id`: 可选的消息 ID（默认使用微秒时间戳）
- `chunk_size`: 每个块的大小（字节），默认 50000 (50KB)
- `on_done`: 可选的完成回调 `callback(exception)`
  - `exception=None` 表示成功
  - `exception!=None` 表示失败

**特点**：
- 非阻塞，立即返回
- 后台异步发送
- 通过回调通知完成

**示例**：
```python
def my_callback(self, msg):
    payload = bytes(msg.data)
    
    self.publish_large_payload(
        payload=payload,
        topic="my_data",
        message_id=123,
        chunk_size=50000,
        on_done=self._on_send_done
    )

def _on_send_done(self, exc):
    if exc:
        self.get_logger().error(f"Send failed: {exc}")
    else:
        self.get_logger().info("Send succeeded")
```

### 方法 2: `publish_large_payload_async`

**用于 async/await 上下文**

```python
async def publish_large_payload_async(
    self,
    payload: bytes,
    topic: str,
    message_id: Optional[int] = None,
    chunk_size: int = 50000
)
```

**参数**：同上（无回调参数）

**特点**：
- 可以 `await` 等待完成
- 发送失败会抛出异常
- 适合在 async 函数中使用

**示例**：
```python
async def send_data_async(self):
    payload = b'large data...'
    
    try:
        await self.publish_large_payload_async(
            payload=payload,
            topic="my_data",
            chunk_size=100000
        )
        print("Success!")
    except Exception as e:
        print(f"Failed: {e}")
```

## 传输协议

### 发送端

对于每个数据块：
1. 发送元数据到 `<topic>:meta`
2. 发送数据块到 `<topic>`

**元数据格式** (JSON):
```json
{
  "id": 12345,         // 消息 ID
  "chunk": 0,          // 当前块索引（从 0 开始）
  "total": 10,         // 总块数
  "size": 50000        // 当前块大小（字节）
}
```

### 接收端

接收端需要：
1. 订阅 `<topic>:meta` 获取元数据
2. 订阅 `<topic>` 获取数据块
3. 根据 `id` 和 `chunk` 索引重组数据
4. 当收到所有块（`chunk == total - 1`）后完成重组

## 使用示例

### 示例 1: 点云数据传输（TeleopBridge）

```python
class TeleopBridge(LiveKitROS2BridgeBase):
    def compressed_pointcloud_callback(self, msg):
        payload = bytes(msg.data)
        
        # 使用基类的大数据包发送
        self.publish_large_payload(
            payload=payload,
            topic="pointcloud",
            message_id=self._seq,
            chunk_size=50000,
            on_done=self._on_send_done
        )
    
    def _on_send_done(self, exc):
        self._sending = False
        if exc:
            self.get_logger().error(f"Failed: {exc}")
```

### 示例 2: 图像数据传输

```python
class ImageBridge(LiveKitROS2BridgeBase):
    def image_callback(self, msg):
        # 压缩图像可能有几百 KB
        payload = bytes(msg.data)
        
        self.publish_large_payload(
            payload=payload,
            topic="camera_image",
            chunk_size=50000,  # 50KB 块
            on_done=lambda exc: self.handle_result(exc)
        )
```

### 示例 3: 定期发送大数据（async）

```python
class BulkDataBridge(LiveKitROS2BridgeBase):
    def __init__(self, room):
        super().__init__(room)
        self.create_timer(1.0, self.periodic_send)
    
    def periodic_send(self):
        large_data = self.generate_data()  # 1MB
        
        # 提交到 asyncio loop
        self._submit_to_loop(
            self._send_async(large_data)
        )
    
    async def _send_async(self, data):
        try:
            await self.publish_large_payload_async(
                payload=data,
                topic="bulk_data",
                chunk_size=100000  # 100KB 块
            )
            self.get_logger().info("Sent successfully")
        except Exception as e:
            self.get_logger().error(f"Failed: {e}")
```

### 示例 4: 带重试的发送

```python
class RobustBridge(LiveKitROS2BridgeBase):
    def send_with_retry(self, payload, max_retries=3):
        self.retry_count = 0
        self.max_retries = max_retries
        self.payload = payload
        
        self._try_send()
    
    def _try_send(self):
        self.publish_large_payload(
            payload=self.payload,
            topic="reliable_data",
            on_done=self._on_send_result
        )
    
    def _on_send_result(self, exc):
        if exc:
            self.retry_count += 1
            if self.retry_count < self.max_retries:
                self.get_logger().warn(
                    f"Send failed, retrying ({self.retry_count}/{self.max_retries})"
                )
                self._try_send()
            else:
                self.get_logger().error("Send failed after all retries")
        else:
            self.get_logger().info("Send succeeded")
```

## 性能考虑

### 块大小选择

| 数据类型 | 典型大小 | 推荐块大小 | 原因 |
|---------|---------|-----------|------|
| 压缩点云 | 100KB-1MB | 50KB | 平衡延迟和开销 |
| 高清图像 | 100KB-500KB | 50KB | 适中的传输粒度 |
| 小图像 | <50KB | 不需要分片 | 直接发送更快 |
| 大批量数据 | >1MB | 100KB | 减少块数量 |

### 优化建议

1. **选择合适的块大小**
   - 太小：开销大（每块都有元数据）
   - 太大：超过 LiveKit 限制会失败
   - 推荐：30KB - 100KB

2. **避免并发发送**
   ```python
   # 使用标志位避免重叠
   if self._sending:
       return
   self._sending = True
   ```

3. **检查接收端是否存在**
   ```python
   if len(self.room.remote_participants) == 0:
       return  # 没人在看，不发送
   ```

4. **使用合适的 QoS**
   ```python
   # 对实时数据使用 depth=1
   qos_profile=rclpy.qos.QoSProfile(depth=1)
   ```

5. **记录性能指标**
   ```python
   start = time.time()
   self.publish_large_payload(...)
   # 在 on_done 中计算耗时
   ```

## 故障排查

### 问题 1: "No asyncio event loop available"

**原因**：基类初始化时没有运行中的 asyncio loop

**解决**：确保在 asyncio 上下文中创建 bridge
```python
async def main():
    room = rtc.Room()
    await room.connect(url, token)
    bridge = MyBridge(room)  # 此时有 loop
```

### 问题 2: 发送失败但没有错误日志

**原因**：没有提供 `on_done` 回调

**解决**：添加回调以捕获错误
```python
self.publish_large_payload(
    ...,
    on_done=lambda exc: print(f"Error: {exc}") if exc else None
)
```

### 问题 3: 接收端数据乱序

**原因**：网络延迟导致数据包乱序到达

**解决**：接收端必须使用 `chunk` 索引重组，不能依赖接收顺序

### 问题 4: 内存占用高

**原因**：同时发送多个大数据包

**解决**：
1. 使用发送标志位限制并发
2. 降低发送频率
3. 减小块大小

## 与原版的对比

### 原版 (`livekit_ros2_bridge.py`)

```python
def _publish_large_payload(self, payload, *, topic, chunk_size=50000, ...):
    # 混在业务逻辑中
    ...
```

**问题**：
- 功能和业务逻辑混在一起
- 不能被其他桥接复用
- 难以测试

### 新版（基础层）

```python
# 在 LiveKitROS2BridgeBase 中
def publish_large_payload(self, payload, topic, ...):
    # 通用基础设施
    ...
```

**优势**：
- ✅ 通用能力，任何子类都能用
- ✅ 职责清晰，易于测试
- ✅ 文档完善，易于理解
- ✅ 同时提供同步和异步 API

## 最佳实践总结

1. **使用场景判断**
   - 数据 < 16KB：直接用 `send_feedback`
   - 数据 >= 16KB：使用 `publish_large_payload`

2. **错误处理**
   - 始终提供 `on_done` 回调
   - 记录失败情况
   - 考虑重试机制

3. **性能优化**
   - 控制发送频率
   - 避免并发发送
   - 检查接收端存在性

4. **可维护性**
   - 使用有意义的 topic 名称
   - 添加日志记录
   - 监控传输统计

## 参考示例

完整示例见：
- `teleop_livekit/teleop_bridge.py` - 点云传输实现
- `examples/custom_bridge_example.py` - 图像和大数据传输示例
