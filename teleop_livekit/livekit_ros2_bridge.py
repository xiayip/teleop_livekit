#!/usr/bin/env python3
"""
LiveKit ROS2 Bridge - Python Side
Receives LiveKit data packets and forwards them to ROS2 topics
"""

import asyncio
import json
import time
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass
import threading
import importlib

# LiveKit imports
from livekit import rtc

# ROS2 imports
import rclpy
from rclpy.node import Node
from rclpy.publisher import Publisher
from rclpy.client import Client
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

# Common ROS2 message types
from std_msgs.msg import String, Float64, Float32, Int16, Int32, UInt16, UInt8, Int8, UInt64, Int64
from geometry_msgs.msg import Twist, PoseStamped, Pose, Point, Quaternion, Vector3
from sensor_msgs.msg import Image, CompressedImage
from nav_msgs.msg import Odometry
from builtin_interfaces.msg import Time
from std_msgs.msg import Header


class ROS2MessageFactory:
    """ROS2 message factory class"""
    
    # Message type mapping
    MESSAGE_TYPE_MAP = {
        'std_msgs/msg/String': String,
        'std_msgs/msg/Int8': Int8,
        'std_msgs/msg/UInt8': UInt8,
        'std_msgs/msg/Int16': Int16,
        'std_msgs/msg/UInt16': UInt16,
        'std_msgs/msg/Int32': Int32,
        'std_msgs/msg/Int64': Int64,
        'std_msgs/msg/UInt64': UInt64,
        'std_msgs/msg/Float32': Float32,
        'std_msgs/msg/Float64': Float64,
        
        'geometry_msgs/msg/Twist': Twist,
        'geometry_msgs/msg/PoseStamped': PoseStamped,
        'geometry_msgs/msg/Pose': Pose,
        'geometry_msgs/msg/Point': Point,
        'geometry_msgs/msg/Quaternion': Quaternion,
        'geometry_msgs/msg/Vector3': Vector3,
        'sensor_msgs/msg/Image': Image,
        'sensor_msgs/msg/CompressedImage': CompressedImage,
        'nav_msgs/msg/Odometry': Odometry,
    }
    
    @classmethod
    def get_message_class(cls, message_type: str):
        """Get message class based on message type string"""
        if message_type in cls.MESSAGE_TYPE_MAP:
            return cls.MESSAGE_TYPE_MAP[message_type]
        
        # Try dynamic import
        try:
            # Parse message type e.g.: "my_package/msg/CustomMsg"
            parts = message_type.split('/')
            if len(parts) == 3:
                package_name, msg_dir, msg_name = parts
                module_name = f"{package_name}.{msg_dir}"
                module = importlib.import_module(module_name)
                return getattr(module, msg_name)
        except (ImportError, AttributeError) as e:
            print(f"Unable to import message type {message_type}: {e}")
            return None
        
        return None
    
    @classmethod
    def create_message(cls, message_type: str, data: Dict[str, Any]):
        """Create ROS2 message instance"""
        msg_class = cls.get_message_class(message_type)
        if msg_class is None:
            raise ValueError(f"Unsupported message type: {message_type}")
        
        msg = msg_class()
        cls._populate_message(msg, data)
        return msg
    
    @classmethod
    def _populate_message(cls, msg, data: Dict[str, Any]):
        """Populate message data"""
        for key, value in data.items():
            if hasattr(msg, key):
                attr = getattr(msg, key)
                
                if isinstance(value, dict):
                    # Nested message
                    cls._populate_message(attr, value)
                elif isinstance(value, list):
                    # Array field
                    if hasattr(attr, 'clear'):
                        attr.clear()
                    if hasattr(attr, 'extend'):
                        attr.extend(value)
                    else:
                        setattr(msg, key, value)
                else:
                    # Basic type
                    if key == 'stamp' and isinstance(value, (int, float)):
                        # Special timestamp handling
                        stamp = Time()
                        stamp.sec = int(value)
                        stamp.nanosec = int((value - int(value)) * 1e9)
                        setattr(msg, key, stamp)
                    else:
                        # Type conversion handling
                        attr_type = type(getattr(msg, key))
                        if attr_type == float and isinstance(value, (int, str)):
                            # Convert integer or string to float
                            setattr(msg, key, float(value))
                        elif attr_type == int and isinstance(value, (float, str)):
                            # Convert float or string to integer
                            setattr(msg, key, int(value))
                        elif attr_type == bool and isinstance(value, (int, str)):
                            # Convert integer or string to boolean
                            setattr(msg, key, bool(value))
                        else:
                            setattr(msg, key, value)


@dataclass
class PublisherInfo:
    """Publisher information"""
    publisher: Publisher
    message_type: str
    topic_name: str
    created_time: float


class LiveKitROS2Bridge(Node):
    """LiveKit ROS2 bridge node"""
    
    def __init__(self, room: rtc.Room, node_name: str = 'livekit_ros2_bridge'):
        super().__init__(node_name)
        
        self.room = room
        self._topic_publishers: Dict[str, PublisherInfo] = {}
        self.service_clients: Dict[str, Client] = {}
        self.image_subscriber = self.create_subscription(
            Image, '/image_raw', self.image_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=1)
        )
        
        # QoS configuration
        self.default_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )
        
        # Register LiveKit events
        self.room.on("data_received", self.on_data_received)

        self.source = None
        self._video_track_created = False
        
        # Statistics
        self.error_count = 0

        self.get_logger().info(f"LiveKit ROS2 bridge started: {node_name}")
    
    def on_data_received(self, data: rtc.DataPacket):
        """LiveKit data packet received callback"""
        try:
            # Parse JSON data
            packet = json.loads(data.data.decode('utf-8'))
            # debug
            self.get_logger().info(f"Received data packet: {packet}")
            packet_type = packet.get('packetType', '')
            
            if packet_type == 'ros2_message':
                self.handle_ros2_message(packet)
            elif packet_type == 'ros2_service_call':
                self.handle_ros2_service_call(packet)
            else:
                self.get_logger().warn(f"Unknown packet type: {packet_type}")
                
        except Exception as e:
            self.error_count += 1
            self.get_logger().error(f"Error processing data packet: {e}")
    
    def handle_ros2_message(self, packet: Dict[str, Any]):
        """Handle ROS2 message"""
        try:
            topic_name = packet.get('topicName', '')
            message_type = packet.get('messageType', '')
            data = packet.get('data', {})
            
            if not topic_name or not message_type:
                raise ValueError("Missing topic_name or message_type")
            
            # Get or create publisher
            publisher = self.get_or_create_publisher(topic_name, message_type)
            
            # Create and publish message
            msg = ROS2MessageFactory.create_message(message_type, data)
            publisher.publish(msg)
            
        except Exception as e:
            self.error_count += 1
            self.get_logger().error(f"Error handling ROS2 message: {e}")
    
    def handle_ros2_service_call(self, packet: Dict[str, Any]):
        """Handle ROS2 service call"""
        try:
            service_name = packet.get('service_name', '')
            service_type = packet.get('service_type', '')
            request_data = packet.get('request', {})
            request_id = packet.get('request_id', '')
            
            # Service call logic should be implemented here
            # Simplified version
            self.get_logger().info(f"Received service call request: {service_name}")
            
            # Send service response
            asyncio.create_task(self.send_feedback({
                'packet_type': 'ros2_service_response',
                'request_id': request_id,
                'success': True,
                'response': {'result': 'Service call completed'}
            }))
            
        except Exception as e:
            self.get_logger().error(f"Error handling service call: {e}")
    
    def get_or_create_publisher(self, topic_name: str, message_type: str) -> Publisher:
        """Get or create publisher"""
        key = f"{topic_name}:{message_type}"
        
        if key in self._topic_publishers:
            return self._topic_publishers[key].publisher
        
        # Create new publisher
        msg_class = ROS2MessageFactory.get_message_class(message_type)
        if msg_class is None:
            raise ValueError(f"Unsupported message type: {message_type}")
        
        publisher = self.create_publisher(msg_class, topic_name, self.default_qos)
        
        publisher_info = PublisherInfo(
            publisher=publisher,
            message_type=message_type,
            topic_name=topic_name,
            created_time=time.time()
        )
        
        self._topic_publishers[key] = publisher_info
        
        self.get_logger().info(f"Created publisher: {topic_name} ({message_type})")
        return publisher
    
    async def send_feedback(self, feedback: Dict[str, Any]):
        """Send feedback to LiveKit"""
        try:
            json_str = json.dumps(feedback)
            data_bytes = json_str.encode('utf-8')
            await self.room.local_participant.publish_data(data_bytes)
        except Exception as e:
            self.get_logger().error(f"Failed to send feedback: {e}")
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            'error_count': self.error_count,
            'publishers': {
                key: {
                    'topic_name': info.topic_name,
                    'message_type': info.message_type,
                    'created_time': info.created_time
                }
                for key, info in self._topic_publishers.items()
            }
        }
    
    def image_callback(self, msg: Image):
        if len(self.room.remote_participants) == 0:
            self.get_logger().debug("No participants connected, skipping frame upload")
            return
        
        # Create video track if not created yet
        if not self._video_track_created:
            asyncio.create_task(self._ensure_video_track_created())
            return
        
        """Handle image message and forward to LiveKit"""
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

    async def _ensure_video_track_created(self):
        """Ensure video track is created"""
        if not self._video_track_created:
            await self.create_video_track(640, 480, 30)
            self._video_track_created = True

    async def create_video_track(self, width, height, fps):
        """Publish a video track with the specified parameters."""
        self.source = rtc.VideoSource(width, height)
        track = rtc.LocalVideoTrack.create_video_track("wrist_camera", self.source)
        await self.room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_CAMERA,
                video_encoding=rtc.VideoEncoding(max_framerate=fps, max_bitrate=3_000_000),
                video_codec=rtc.VideoCodec.AV1,
            )
        )
        self.get_logger().info("Published LiveKit track (will only send frames when someone is watching)")
        return self.source


class LiveKitROS2BridgeManager:
    """LiveKit ROS2 bridge manager"""
    
    def __init__(self, livekit_url: str, livekit_token: str):
        self.livekit_url = livekit_url
        self.livekit_token = livekit_token
        self.room: Optional[rtc.Room] = None
        self.bridge: Optional[LiveKitROS2Bridge] = None
        self.ros_thread: Optional[threading.Thread] = None
        self.running = False
    
    async def start(self):
        """Start bridge service"""
        try:
            # Initialize ROS2
            rclpy.init()
            
            # Connect to LiveKit
            self.room = rtc.Room()
            await self.room.connect(self.livekit_url, self.livekit_token)

            # Create bridge node
            self.bridge = LiveKitROS2Bridge(self.room)
            
            # Start ROS2 thread
            self.running = True
            self.ros_thread = threading.Thread(target=self._ros_spin_thread, daemon=True)
            self.ros_thread.start()
            
            print("LiveKit ROS2 bridge service started")
            
        except Exception as e:
            print(f"Failed to start bridge service: {e}")
            raise
    
    def _ros_spin_thread(self):
        """ROS2 thread function"""
        try:
            while self.running and rclpy.ok():
                rclpy.spin_once(self.bridge, timeout_sec=0.1)
        except Exception as e:
            print(f"ROS2 thread error: {e}")
    
    async def stop(self):
        """Stop bridge service"""
        print("Stopping bridge service...")
        
        self.running = False
        
        if self.ros_thread and self.ros_thread.is_alive():
            self.ros_thread.join(timeout=1.0)
        
        if self.bridge:
            self.bridge.destroy_node()
        
        if self.room:
            await self.room.disconnect()
        
        if rclpy.ok():
            rclpy.shutdown()
        
        print("Bridge service stopped")
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get statistics"""
        if self.bridge:
            return self.bridge.get_statistics()
        return {}