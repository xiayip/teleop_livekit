#!/usr/bin/env python3
"""
视频流发布模块，处理机器人图像数据发送到VR端
"""

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from livekit import rtc


class VideoStreamPublisher(Node):
    def __init__(self, source: rtc.VideoSource, remote_counter: dict):
        super().__init__('video_stream_publisher')
        self.source = source
        self.remote_counter = remote_counter
        
        # Create subscription for image messages
        self.subscription = self.create_subscription(
            Image, '/image_raw', self.image_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=1)
        )

    def image_callback(self, msg: Image):
        """Handle incoming image messages and forward them to LiveKit."""
        # Only upload stream if there are participants connected
        if self.remote_counter.get("count", 0) == 0:
            self.get_logger().debug("No participant connected, skipping frame upload")
            return

        if msg.encoding != 'rgb8':
            self.get_logger().warn(f'Unsupported encoding {msg.encoding}, skipping frame')
            return

        try:
            frame_bytes = bytearray(msg.data)
            frame = rtc.VideoFrame(
                width=msg.width,
                height=msg.height,
                type=rtc.VideoBufferType.RGB24,
                data=frame_bytes
            )
            self.source.capture_frame(frame)
        except Exception as e:
            self.get_logger().error(f"Error capturing frame: {e}")
