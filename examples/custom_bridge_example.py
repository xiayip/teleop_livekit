#!/usr/bin/env python3
"""
Example: Custom Bridge - 展示如何基于 LiveKitROS2BridgeBase 创建自定义桥接

这个示例演示了如何继承基类来实现一个简单的日志转发桥接，
将 ROS2 的日志消息转发到 LiveKit，用于远程监控。
"""

import json
from livekit import rtc
from rcl_interfaces.msg import Log
import rclpy.qos

from teleop_livekit.livekit_ros2_bridge_base import LiveKitROS2BridgeBase


class LogForwardingBridge(LiveKitROS2BridgeBase):
    """
    自定义桥接示例：将 ROS2 日志转发到 LiveKit
    
    用途：远程监控机器人的日志输出
    """
    
    def __init__(self, room: rtc.Room, node_name: str = 'log_forwarding_bridge'):
        super().__init__(room, node_name)
        
        # 配置
        self.log_level_filter = Log.INFO  # 只转发 INFO 及以上级别
        self.max_logs_per_second = 10
        self._log_count = 0
        self._last_reset_time = 0.0
        
        # 设置订阅
        self.setup_subscriptions()
        
        self.get_logger().info("Log forwarding bridge initialized")
    
    def setup_subscriptions(self):
        """订阅 ROS2 日志话题"""
        self.log_subscriber = self.create_subscription(
            Log,
            '/rosout',  # ROS2 标准日志话题
            self.log_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=100)
        )
    
    def log_callback(self, msg: Log):
        """
        处理日志消息
        
        业务逻辑：
        1. 过滤日志级别
        2. 限制发送频率
        3. 转发到 LiveKit
        """
        import time
        
        # 级别过滤
        if msg.level < self.log_level_filter:
            return
        
        # 频率限制
        now = time.time()
        if now - self._last_reset_time > 1.0:
            self._log_count = 0
            self._last_reset_time = now
        
        if self._log_count >= self.max_logs_per_second:
            return
        
        self._log_count += 1
        
        # 格式化日志
        level_names = {
            Log.DEBUG: 'DEBUG',
            Log.INFO: 'INFO',
            Log.WARN: 'WARN',
            Log.ERROR: 'ERROR',
            Log.FATAL: 'FATAL'
        }
        
        log_data = {
            'packetType': 'ros2_log',
            'level': level_names.get(msg.level, 'UNKNOWN'),
            'name': msg.name,
            'msg': msg.msg,
            'file': msg.file,
            'function': msg.function,
            'line': msg.line,
            'timestamp': {
                'sec': msg.stamp.sec,
                'nanosec': msg.stamp.nanosec
            }
        }
        
        # 发送到 LiveKit
        self.publish_to_livekit(log_data)


class SensorDataBridge(LiveKitROS2BridgeBase):
    """
    自定义桥接示例：多传感器数据聚合
    
    用途：将多个传感器数据聚合后定期发送
    """
    
    def __init__(self, room: rtc.Room, node_name: str = 'sensor_data_bridge'):
        super().__init__(room, node_name)
        
        # 传感器数据缓存
        self.latest_temperature = None
        self.latest_humidity = None
        self.latest_battery = None
        
        # 定时器：每秒聚合发送一次
        self.timer = self.create_timer(1.0, self.aggregate_and_send)
        
        # 设置订阅
        self.setup_subscriptions()
        
        self.get_logger().info("Sensor data bridge initialized")
    
    def setup_subscriptions(self):
        """订阅多个传感器话题"""
        from std_msgs.msg import Float32
        
        self.temp_sub = self.create_subscription(
            Float32, '/sensors/temperature', self.temperature_callback, 10
        )
        self.humidity_sub = self.create_subscription(
            Float32, '/sensors/humidity', self.humidity_callback, 10
        )
        self.battery_sub = self.create_subscription(
            Float32, '/sensors/battery', self.battery_callback, 10
        )
    
    def temperature_callback(self, msg):
        """温度传感器回调"""
        from std_msgs.msg import Float32
        self.latest_temperature = msg.data
    
    def humidity_callback(self, msg):
        """湿度传感器回调"""
        from std_msgs.msg import Float32
        self.latest_humidity = msg.data
    
    def battery_callback(self, msg):
        """电池传感器回调"""
        from std_msgs.msg import Float32
        self.latest_battery = msg.data
    
    def aggregate_and_send(self):
        """
        聚合所有传感器数据并发送
        
        业务逻辑：定时聚合，只在有数据时发送
        """
        if all([
            self.latest_temperature is not None,
            self.latest_humidity is not None,
            self.latest_battery is not None
        ]):
            sensor_data = {
                'packetType': 'sensor_aggregate',
                'temperature': self.latest_temperature,
                'humidity': self.latest_humidity,
                'battery': self.latest_battery,
                'timestamp': self.get_clock().now().to_msg().sec
            }
            
            self.publish_to_livekit(sensor_data)


class SelectiveCommandBridge(LiveKitROS2BridgeBase):
    """
    自定义桥接示例：命令过滤和验证
    
    用途：从 LiveKit 接收命令，验证后转发到机器人
    """
    
    def __init__(self, room: rtc.Room, node_name: str = 'selective_command_bridge'):
        super().__init__(room, node_name)
        
        # 白名单：允许的话题和消息类型
        self.allowed_topics = {
            '/cmd_vel': 'geometry_msgs/msg/Twist',
            '/arm/joint_command': 'sensor_msgs/msg/JointState',
        }
        
        # 速度限制
        self.max_linear_velocity = 1.0  # m/s
        self.max_angular_velocity = 1.0  # rad/s
        
        self.get_logger().info("Selective command bridge initialized")
    
    def handle_ros2_message(self, packet):
        """
        覆盖基类方法，添加业务验证逻辑
        
        业务逻辑：
        1. 检查话题白名单
        2. 验证消息类型
        3. 应用安全限制
        """
        try:
            topic_name = packet['topicName']
            message_type = packet['messageType']
            
            # 白名单检查
            if topic_name not in self.allowed_topics:
                self.get_logger().warn(f"Topic {topic_name} not in whitelist, rejected")
                return
            
            if self.allowed_topics[topic_name] != message_type:
                self.get_logger().warn(f"Message type mismatch for {topic_name}, rejected")
                return
            
            # 对 cmd_vel 应用速度限制
            if topic_name == '/cmd_vel':
                data = packet['data']
                
                # 限制线速度
                linear = data.get('linear', {})
                linear['x'] = max(-self.max_linear_velocity, min(self.max_linear_velocity, linear.get('x', 0.0)))
                linear['y'] = max(-self.max_linear_velocity, min(self.max_linear_velocity, linear.get('y', 0.0)))
                linear['z'] = max(-self.max_linear_velocity, min(self.max_linear_velocity, linear.get('z', 0.0)))
                
                # 限制角速度
                angular = data.get('angular', {})
                angular['x'] = max(-self.max_angular_velocity, min(self.max_angular_velocity, angular.get('x', 0.0)))
                angular['y'] = max(-self.max_angular_velocity, min(self.max_angular_velocity, angular.get('y', 0.0)))
                angular['z'] = max(-self.max_angular_velocity, min(self.max_angular_velocity, angular.get('z', 0.0)))
            
            # 调用基类方法发布
            super().handle_ros2_message(packet)
            
        except Exception as e:
            self.get_logger().error(f"Failed to validate and handle message: {e}")


# ========== 使用示例 ==========

async def example_usage():
    """展示如何使用自定义桥接"""
    import asyncio
    from teleop_livekit.teleop_manager import TeleopManager
    
    # 连接参数
    livekit_url = "ws://localhost:7880"
    livekit_token = "your_token_here"
    
    # 方式 1: 使用内置的远程操控桥接
    teleop_manager = TeleopManager(livekit_url, livekit_token)
    await teleop_manager.start()
    
    # 方式 2: 创建自定义桥接
    # room = rtc.Room()
    # await room.connect(livekit_url, livekit_token)
    # 
    # # 选择一个自定义桥接
    # custom_bridge = LogForwardingBridge(room)
    # # 或
    # # custom_bridge = SensorDataBridge(room)
    # # 或
    # # custom_bridge = SelectiveCommandBridge(room)
    # 
    # # 启动 ROS2 spinning
    # import rclpy
    # rclpy.init()
    # while rclpy.ok():
    #     rclpy.spin_once(custom_bridge, timeout_sec=0.1)
    #     await asyncio.sleep(0.01)


class LargeDataBridge(LiveKitROS2BridgeBase):
    """
    自定义桥接示例：发送大数据包（如图像、点云）
    
    用途：演示如何使用基类的 publish_large_payload 方法
          自动分片发送大数据
    """
    
    def __init__(self, room: rtc.Room, node_name: str = 'large_data_bridge'):
        super().__init__(room, node_name)
        
        # 发送状态跟踪
        self._sending = False
        self._seq = 0
        
        # 设置订阅
        self.setup_subscriptions()
        
        self.get_logger().info("Large data bridge initialized")
    
    def setup_subscriptions(self):
        """订阅大数据话题"""
        from sensor_msgs.msg import CompressedImage
        
        # 示例：订阅压缩图像
        self.image_sub = self.create_subscription(
            CompressedImage,
            '/camera/image_raw/compressed',
            self.image_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=1)
        )
    
    def image_callback(self, msg):
        """
        处理压缩图像消息
        
        业务逻辑：
        1. 检查是否有发送正在进行
        2. 使用 publish_large_payload 自动分片
        3. 异步发送完成后回调
        """
        # 跳过如果正在发送
        if self._sending:
            return
        
        self._sending = True
        self._seq += 1
        
        # 图像数据可能很大（几百KB），使用大数据包发送
        payload = bytes(msg.data)
        
        self.get_logger().debug(
            f"Sending compressed image: {len(payload)} bytes, format: {msg.format}"
        )
        
        # 使用基类的大数据包发送能力
        # 自动分片为 50KB 的块
        self.publish_large_payload(
            payload=payload,
            topic="camera_image",
            message_id=self._seq,
            chunk_size=50000,  # 50KB chunks
            on_done=self._on_image_send_done
        )
    
    def _on_image_send_done(self, exc):
        """图像发送完成回调"""
        self._sending = False
        
        if exc:
            self.get_logger().error(f"Image send failed: {exc}")
        else:
            self.get_logger().debug("Image sent successfully")


# ========== 使用大数据包发送的另一种方式（async） ==========

class AsyncLargeDataBridge(LiveKitROS2BridgeBase):
    """
    演示使用 async 版本的大数据包发送
    
    适用于：在 async 上下文中需要等待发送完成的场景
    """
    
    def __init__(self, room: rtc.Room, node_name: str = 'async_large_data_bridge'):
        super().__init__(room, node_name)
        
        # 定时器：定期发送大数据
        self.timer = self.create_timer(1.0, self.periodic_send)
        
        self.get_logger().info("Async large data bridge initialized")
    
    def periodic_send(self):
        """定期发送大数据"""
        # 模拟大数据（1MB）
        large_data = b'x' * (1024 * 1024)
        
        # 提交到 asyncio loop 异步发送
        self._submit_to_loop(self._send_large_data_async(large_data))
    
    async def _send_large_data_async(self, data: bytes):
        """
        异步发送大数据
        
        使用 publish_large_payload_async 可以在 async 函数中
        直接 await 等待发送完成
        """
        try:
            await self.publish_large_payload_async(
                payload=data,
                topic="bulk_data",
                chunk_size=100000  # 100KB chunks
            )
            self.get_logger().info(f"Sent {len(data)} bytes successfully")
        except Exception as e:
            self.get_logger().error(f"Failed to send data: {e}")


if __name__ == '__main__':
    import asyncio
    asyncio.run(example_usage())
