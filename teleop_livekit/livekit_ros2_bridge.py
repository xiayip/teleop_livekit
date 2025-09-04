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
from typing import List

# LiveKit imports
from livekit import rtc

# ROS2 imports
import rclpy
from rclpy.node import Node
from rclpy.publisher import Publisher
from rclpy.client import Client
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.duration import Duration as RclpyDuration
from tf2_ros import Buffer, TransformListener

# Common ROS2 message types
from std_msgs.msg import String, Float64, Float32, Int16, Int32, UInt16, UInt8, Int8, UInt64, Int64, Header
from geometry_msgs.msg import Twist, PoseStamped, Pose, Point, Quaternion, Vector3
from sensor_msgs.msg import Image, CompressedImage, JointState
from nav_msgs.msg import Odometry
from builtin_interfaces.msg import Time, Duration as MsgDuration
from tf2_geometry_msgs import do_transform_pose
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from controller_manager_msgs.srv import SwitchController, ListControllers

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
        'sensor_msgs/msg/JointState': JointState,
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
        
        # livekit stuff
        self.room = room
        self.room.on("data_received", self.on_data_received)
        self.room.on("participant_connected", lambda participant: self.get_logger().info(f"Participant connected: {participant.identity}"))
        self.room.on("participant_disconnected", lambda participant: self.get_logger().info(f"Participant disconnected: {participant.identity}"))
        self.video_source = None

        # ROS stuff
        self._topic_publishers: Dict[str, PublisherInfo] = {}
        self.service_clients: Dict[str, Client] = {}
        self.image_subscriber = self.create_subscription(
            Image, '/image_raw', self.image_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=1)
        )
        self.offset_pose_subscriber = self.create_subscription(  # covert offset pose to absolute ee pose
            PoseStamped, '/ee_offset_pose', self.offset_pose_subscriber_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=1)
        )
        self.ee_pose_publisher = self.create_publisher(
            PoseStamped, '/servo_node/pose_target_cmds', QoSProfile(depth=10)
        )
        self.joint_trajectory_publisher = self.create_publisher(
            JointTrajectory, '/joint_trajectory', 10)
        self.controller_switch_client = self.create_client(
            SwitchController, '/controller_manager/switch_controller')
        self.controller_list_client = self.create_client(
            ListControllers, '/controller_manager/list_controllers')
        # preset pose
        self.ready_to_tap_pose = JointTrajectory()
        self.ready_to_tap_pose.joint_names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']
        self.ready_to_tap_pose.points.append(JointTrajectoryPoint(positions=[-1.5707, 0.5725, -1.3114, 0.0, 0.7077, 0.0], time_from_start=MsgDuration(sec=1, nanosec=0)))
        self.ready_to_pick_pose = JointTrajectory()
        self.ready_to_pick_pose.joint_names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']
        self.ready_to_pick_pose.points.append(JointTrajectoryPoint(positions=[1.5707, 0.71, -0.58, 0.0, 1.2, 0.0], time_from_start=MsgDuration(sec=1, nanosec=0)))
        # debug
        self.debug_publisher = self.create_publisher(PoseStamped, '/debug/ee_pose', 10)
        # log init ee pose Transform
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self, spin_thread=True)
        self.ee_frame = 'link_grasp_center'
        self.base_frame = 'base_link'
        self._init_ee2base = None
        # QoS configuration
        self.default_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )
        # Statistics
        self.error_count = 0
        self.get_logger().info(f"LiveKit ROS2 bridge started: {node_name}")
    
    def on_data_received(self, data: rtc.DataPacket):
        """LiveKit data packet received callback"""
        try:
            # Parse JSON data
            packet = json.loads(data.data.decode('utf-8'))
            # debug
            # self.get_logger().info(f"Received data packet: {packet}")
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
        self.get_logger().info(f"Received service call packet: {packet}")
        try:
            service_name = packet.get('serviceName', '')
            service_type = packet.get('serviceType', '')
            request_data = packet.get('request', {})
            request_id = packet.get('requestId', '')
            
            # Service call logic should be implemented here
            # Simplified version
            self.get_logger().info(f"Received service call request: {service_name}")

            success = False
            if service_name == 'start_teleop':
                self.get_logger().info("Starting teleoperation...")
                # Implement teleoperation start logic here
                if self._ensure_init_pose(force=True) and self.switch_to_forward_position_controller():
                    success = True
                    self.get_logger().info("Initial pose confirmed.")
                else:
                    self.get_logger().error("Initial pose not set, cannot start teleoperation.")
            elif service_name == 'goto_pick_pose':
                self.get_logger().info("Going to pick pose...")
                if (self.switch_to_joint_trajectory_controller()):
                    joint_trajectory = self.ready_to_pick_pose
                    self.joint_trajectory_publisher.publish(joint_trajectory)
                    success = True
                else:
                    self.get_logger().error("Failed to switch to joint trajectory controller.")
            elif service_name == 'goto_tap_pose':
                self.get_logger().info("Going to tap pose...")
                if (self.switch_to_joint_trajectory_controller()):
                    joint_trajectory = self.ready_to_tap_pose
                    self.joint_trajectory_publisher.publish(joint_trajectory)
                    success = True
                else:
                    self.get_logger().error("Failed to switch to joint trajectory controller.")

            # Send service response
            asyncio.create_task(self.send_feedback({
                'packetType': 'ros2_service_response',
                'requestId': request_id,
                'success': success,
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
        """Process a ROS Image message: ensure track then capture frame.
        Returns True if a frame was captured, False otherwise.
        """
        # Skip if no viewers
        if len(self.room.remote_participants) == 0:
            self.get_logger().debug("No participants connected, skipping frame upload")
            return False
        frame_bytes = bytearray(msg.data)
        frame = rtc.VideoFrame(
            width=msg.width,
            height=msg.height,
            type=rtc.VideoBufferType.RGB24,
            data=frame_bytes,
        )
        if self.video_source is not None:
            self.video_source.capture_frame(frame)
        
    def offset_pose_subscriber_callback(self, msg: PoseStamped):
        if not self._ensure_init_pose():
            self.get_logger().error("Initial pose not set, cannot apply offset")
            return
        # self.get_logger().info(f"Received offset pose: {msg.pose}")
        new_ee_pose = do_transform_pose(msg.pose, self._init_ee2base)
        # Build PoseStamped
        ee_pose_msg = PoseStamped()
        ee_pose_msg.header = Header()
        ee_pose_msg.header.stamp = self.get_clock().now().to_msg()
        ee_pose_msg.header.frame_id = self.base_frame
        ee_pose_msg.pose = new_ee_pose
        self.ee_pose_publisher.publish(ee_pose_msg)

    def _ensure_init_pose(self, force: bool = False):
        """Capture the initial absolute pose of ee_frame in base_frame using TF."""
        if self._init_ee2base is not None and not force:
            return True
        try:
            # Store as tuple for fast access: (px,py,pz, qx,qy,qz,qw)
            self._init_ee2base = self.tf_buffer.lookup_transform(self.base_frame, self.ee_frame, rclpy.time.Time(), timeout=RclpyDuration(seconds=1.0))
            self.get_logger().info(
                f"[PoseHandler] Captured init_pose of {self.ee_frame} in {self.base_frame}: "
                f"Transform: {self._init_ee2base.transform.translation}, Rotation: {self._init_ee2base.transform.rotation}")
            return True
        except Exception as e:
            self.get_logger().error(f"[PoseHandler] Failed to capture init_pose via TF: {e}")
            return False
        
    async def create_video_track(self, width: int, height: int, fps: int):
        """Create and publish LiveKit video track."""
        self.video_source = rtc.VideoSource(width, height)
        track = rtc.LocalVideoTrack.create_video_track("wrist_camera", self.video_source)
        await self.room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_CAMERA,
                video_encoding=rtc.VideoEncoding(max_framerate=fps, max_bitrate=3_000_000),
                video_codec=rtc.VideoCodec.AV1,
            ),
        )
        self.get_logger().info("Published LiveKit track (will only send frames when someone is watching)")

    def get_active_controllers(self) -> Optional[List[str]]:
        """List current controllers and their states."""
        if not self.controller_list_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().error("Controller list service not available")
            return None
        request = ListControllers.Request()
        future = self.controller_list_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=2.0)
        if future.result() is not None:
            controllers = future.result().controller
            active_controllers = [c.name for c in controllers if c.state == 'active']
            self.get_logger().info(f"Active controllers: {active_controllers}")
            return active_controllers
        self.get_logger().error("Failed to get controller list or no response")
        return None

    def switch_controllers(self, active_controllers: List[str], deactivate_controllers: List[str]):
        """Switch the active arm controller."""
        # check if the controller has already been activated
        current_active = self.get_active_controllers()
        if not current_active:
            self.get_logger().error("No active controllers found")
            return False
        if all(ac in current_active for ac in active_controllers):
            self.get_logger().info(f"Controllers {active_controllers} already active, no switch needed")
            return True

        # send service call to switch arm controller
        request = SwitchController.Request()
        request.activate_controllers = active_controllers
        request.deactivate_controllers = deactivate_controllers
        request.strictness = SwitchController.Request.BEST_EFFORT
        future = self.controller_switch_client.call_async(request)
        self.get_logger().info(f"Switching controllers: {deactivate_controllers} -> {active_controllers}")
        if future.result() is not None and future.result().ok:
            self.get_logger().info(f"Controller switch result: {future.result().ok}")
            return True
        self.get_logger().error("Controller switch failed or no response")
        return False

    def switch_to_joint_trajectory_controller(self):
        """Switch to joint trajectory controller."""
        return self.switch_controllers(
            active_controllers=['z1_arm_trajectory_controller'],
            deactivate_controllers=['z1_arm_forward_position_controller']
        )

    def switch_to_forward_position_controller(self):
        """Switch to forward position controller."""
        return self.switch_controllers(
            active_controllers=['z1_arm_forward_position_controller'],
            deactivate_controllers=['z1_arm_trajectory_controller']
        )

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
            await self.bridge.create_video_track(640, 480, 30)
            
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