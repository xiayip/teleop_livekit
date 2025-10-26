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
from rclpy.action import ActionClient
from rclpy.action.client import ClientGoalHandle
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.duration import Duration as RclpyDuration
from tf2_ros import Buffer, TransformListener

# Common ROS2 message types
from std_msgs.msg import String, Float64, Float32, Int16, Int32, UInt16, UInt8, Int8, UInt64, Int64, Header
from geometry_msgs.msg import Twist, TwistStamped, PoseStamped, Pose, Point, Quaternion, Vector3
from sensor_msgs.msg import Image, CompressedImage, JointState
from nav_msgs.msg import Odometry
from builtin_interfaces.msg import Time, Duration as MsgDuration
from tf2_geometry_msgs import do_transform_pose
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from controller_manager_msgs.srv import SwitchController, ListControllers
from std_msgs.msg import ByteMultiArray

class ROS2MessageFactory:
    """ROS2 message factory class"""
    
    @classmethod
    def get_message_class(cls, message_type: str):
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
                    # if time stamp detected, use local time
                    if key == 'stamp':
                        if hasattr(attr, 'sec') and hasattr(attr, 'nanosec'):
                            now = rclpy.clock.Clock().now().to_msg()
                            attr.sec = now.sec
                            attr.nanosec = now.nanosec
                            continue
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
        self.action_clients: Dict[str, ActionClient] = {}
        self.action_goal_handles: Dict[str, ClientGoalHandle] = {}
        self.image_subscriber = self.create_subscription(
            Image, '/image_raw', self.image_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=1)
        )
        self.compressed_pointcloud_subscriber = self.create_subscription(
            ByteMultiArray, '/compressed_pointcloud', self.compressed_pointcloud_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=1)
        )
        # QoS configuration
        self.default_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )
        # LiveKit publishes must happen on the asyncio loop started in the main thread
        try:
            self._asyncio_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._asyncio_loop = None
        # Sending guard for pointcloud
        self._send_lock = threading.Lock()
        self._is_sending_pointcloud = False
        # Continuous id for pointcloud messages
        self._pointcloud_seq = 0
        # Statistics
        self.error_count = 0
        self.get_logger().info(f"LiveKit ROS2 bridge started: {node_name}")
    
    def _submit_to_loop(self, coro: asyncio.coroutines):
        """Submit coroutine to asyncio loop"""
        if self._asyncio_loop is None:
            self.get_logger().error("No asyncio event loop available to run coroutine")
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, self._asyncio_loop)
        except Exception as e:
            self.get_logger().error(f"Failed to submit coroutine to loop: {e}")

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
            elif packet_type == 'ros2_action_send_goal':
                self.handle_ros2_action_send_goal(packet)
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

            if not service_name or not service_type:
                raise ValueError("Missing serviceName or serviceType")
            # Get or create service client
            if service_name in self.service_clients:
                service_client = self.service_clients[service_name]
            else:
                service_class = ROS2MessageFactory.get_message_class(service_type)
                if service_class is None:
                    raise ValueError(f"Unsupported service type: {service_type}")
                service_client = self.create_client(service_class, service_name)
                self.service_clients[service_name] = service_client
            # Wait for service to be available
            if not service_client.wait_for_service(timeout_sec=5.0):
                self.get_logger().error(f"Service {service_name} not available")
                self._submit_to_loop(self.send_feedback({
                    'packetType': 'ros2_service_response',
                    'requestId': request_id,
                    'success': False,
                    'response': {'error': f'Service {service_name} not available'}
                }))
                return
            # Send service response
            request_msg = ROS2MessageFactory.create_message(service_type + 'Request', request_data)
            self.get_logger().info(f"Calling service {service_name} with request: {request_data}")
            future = service_client.call_async(request_msg)
            rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
            if future.result() is not None:
                response_msg = future.result()
                # Convert response message to dict
                response_dict = {}
                for field in response_msg.get_fields_and_field_types().keys():
                    response_dict[field] = getattr(response_msg, field)
                self.get_logger().info(f"Service {service_name} response: {response_dict}")
                self._submit_to_loop(self.send_feedback({
                    'packetType': 'ros2_service_response',
                    'requestId': request_id,
                    'success': True,
                    'response': response_dict
                }))
                return
            else:
                self.get_logger().error(f"Service {service_name} call failed or no response")
                self._submit_to_loop(self.send_feedback({
                    'packetType': 'ros2_service_response',
                    'requestId': request_id,
                    'success': False,
                    'response': {'error': 'Service call failed or no response'}
                }))
                return
            
        except Exception as e:
            self.get_logger().error(f"Error handling service call: {e}")
    
    def handle_ros2_action_send_goal(self, packet: Dict[str, Any]):
        """Handle ROS2 action send goal request"""
        self.get_logger().info(f"Received action send goal packet: {packet}")
        try:
            action_name = packet.get('actionName', '')
            action_type = packet.get('actionType', '')
            goal_data = packet.get('goal', {})
            goal_id = packet.get('goalId', '')
            
            if not action_name or not action_type:
                raise ValueError("Missing actionName or actionType")
            
            # Get or create action client
            action_client = self.get_or_create_action_client(action_name, action_type)
            
            if action_client is None:
                raise ValueError(f"Failed to create action client for {action_name}")
            
            # Wait for action server to be available
            if not action_client.wait_for_server(timeout_sec=5.0):
                self.get_logger().error(f"Action server {action_name} not available")
                self._submit_to_loop(self.send_feedback({
                    'packetType': 'ros2_action_response',
                    'goalId': goal_id,
                    'status': 'server_not_available',
                    'message': f'Action server {action_name} not available'
                }))
                return
            
            # Get the goal message type
            action_class = ROS2MessageFactory.get_message_class(action_type)
            if action_class is None:
                raise ValueError(f"Unsupported action type: {action_type}")
            
            # Create goal message
            goal_msg = action_class.Goal()
            ROS2MessageFactory._populate_message(goal_msg, goal_data)
            
            # Send goal asynchronously
            self.get_logger().info(f"Sending goal to action {action_name}")
            send_goal_future = action_client.send_goal_async(
                goal_msg,
                feedback_callback=lambda feedback_msg: self._action_feedback_callback(
                    goal_id, feedback_msg
                )
            )
            send_goal_future.add_done_callback(
                lambda future: self._action_goal_response_callback(goal_id, future)
            )
            
        except Exception as e:
            self.error_count += 1
            self.get_logger().error(f"Error handling action send goal: {e}")
            # Send error feedback
            self._submit_to_loop(self.send_feedback({
                'packetType': 'ros2_action_response',
                'goalId': packet.get('goalId', ''),
                'status': 'error',
                'message': str(e)
            }))
    
    def get_or_create_action_client(self, action_name: str, action_type: str) -> Optional[ActionClient]:
        """Get or create action client"""
        key = f"{action_name}:{action_type}"
        
        if key in self.action_clients:
            return self.action_clients[key]
        
        # Create new action client
        action_class = ROS2MessageFactory.get_message_class(action_type)
        if action_class is None:
            self.get_logger().error(f"Unsupported action type: {action_type}")
            return None
        
        try:
            action_client = ActionClient(self, action_class, action_name)
            self.action_clients[key] = action_client
            self.get_logger().info(f"Created action client: {action_name} ({action_type})")
            return action_client
        except Exception as e:
            self.get_logger().error(f"Failed to create action client: {e}")
            return None
    
    def _action_feedback_callback(self, goal_id: str, feedback_msg):
        """Action feedback callback"""
        try:
            self.get_logger().info(f"Action feedback for goal {goal_id}: {feedback_msg.feedback}")
            # Send feedback to LiveKit
            self._submit_to_loop(self.send_feedback({
                'packetType': 'ros2_action_feedback',
                'goalId': goal_id,
                'feedback': str(feedback_msg.feedback)
            }))
        except Exception as e:
            self.get_logger().error(f"Error in action feedback callback: {e}")
    
    def _action_goal_response_callback(self, goal_id: str, future):
        """Action goal response callback"""
        self.get_logger().info(f"Action goal response for goal {goal_id}")  
        try:
            goal_handle = future.result()
            if not goal_handle.accepted:
                self.get_logger().info(f"Goal {goal_id} rejected")
                self._submit_to_loop(self.send_feedback({
                    'packetType': 'ros2_action_response',
                    'goalId': goal_id,
                    'status': 'rejected',
                    'message': 'Goal was rejected by action server'
                }))
                return
            
            self.get_logger().info(f"Goal {goal_id} accepted")
            # Store goal handle
            self.action_goal_handles[goal_id] = goal_handle
            
            # Send acceptance feedback
            self._submit_to_loop(self.send_feedback({
                'packetType': 'ros2_action_response',
                'goalId': goal_id,
                'status': 'accepted',
                'message': 'Goal accepted by action server'
            }))
            
            # Wait for result
            get_result_future = goal_handle.get_result_async()
            get_result_future.add_done_callback(
                lambda result_future: self._action_result_callback(goal_id, result_future)
            )
            
        except Exception as e:
            self.get_logger().error(f"Error in action goal response callback: {e}")
            asyncio.create_task(self.send_feedback({
                'packetType': 'ros2_action_response',
                'goalId': goal_id,
                'status': 'error',
                'message': str(e)
            }))
    
    def _action_result_callback(self, goal_id: str, future):
        """Action result callback"""
        try:
            result = future.result()
            self.get_logger().info(f"Action result for goal {goal_id}: {result.result}")
            
            # Remove goal handle
            if goal_id in self.action_goal_handles:
                del self.action_goal_handles[goal_id]
            
            # Send result to LiveKit
            self._submit_to_loop(self.send_feedback({
                'packetType': 'ros2_action_result',
                'goalId': goal_id,
                'status': 'succeeded',
                'result': str(result.result)
            }))
        except Exception as e:
            self.get_logger().error(f"Error in action result callback: {e}")
            self._submit_to_loop(self.send_feedback({
                'packetType': 'ros2_action_result',
                'goalId': goal_id,
                'status': 'error',
                'message': str(e)
            }))
    
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
            self.get_logger().info("No participants connected, skipping frame upload", throttle_duration_sec=5)
            return False
        frame_bytes = bytearray(msg.data)
        frame = rtc.VideoFrame(
            width=msg.width,
            height=msg.height,
            type=rtc.VideoBufferType.RGB24,
            data=frame_bytes,
        )
        # self.get_logger().info(f"Uploading image frame of size {len(frame_bytes)} bytes")
        if self.video_source is not None:
            self.video_source.capture_frame(frame)
    
    def _publish_large_payload(self, payload: bytes, *, topic: str, chunk_size: int = 50000, on_done: Optional[Callable[[Optional[Exception]], None]] = None, message_id: Optional[int] = None):
        """Send bytes payload across LiveKit data channel safely from the ROS thread."""
        if self._asyncio_loop is None:
            self.get_logger().error("No asyncio event loop available for LiveKit publish")
            return
        def _publish_chunks():
            async def _do_publish():
                total_chunks = (len(payload) + chunk_size - 1) // chunk_size
                for idx in range(0, len(payload), chunk_size):
                    chunk = payload[idx: idx + chunk_size]
                    metadata = json.dumps({
                        "id": message_id,
                        "chunk": idx // chunk_size,
                        "total": total_chunks,
                    }).encode("utf-8")
                    await self.room.local_participant.publish_data(metadata, topic=f"{topic}:meta")
                    await self.room.local_participant.publish_data(chunk, topic=topic)
            return _do_publish()
        future = asyncio.run_coroutine_threadsafe(_publish_chunks(), self._asyncio_loop)
        def _done_cb(f: asyncio.Future):
            exc: Optional[Exception] = None
            try:
                f.result()
            except Exception as e:
                exc = e
                self.get_logger().error(f"Failed to publish LiveKit data on topic '{topic}': {e}")
            if on_done is not None:
                try:
                    on_done(exc)
                except Exception as cb_e:
                    self.get_logger().error(f"on_done callback error for topic '{topic}': {cb_e}")
        future.add_done_callback(_done_cb)

    def _flatten_bytes(self, data) -> bytes:
        """Convert various containers to a flat bytes buffer."""
        if len(data) > 0 and isinstance(data[0], (bytes, bytearray, memoryview)):
            return b"".join(bytes(seg) for seg in data)
        return bytes(data)
    
    def _on_pointcloud_send_done(self, exc: Optional[Exception]):
        with self._send_lock:
            self._is_sending_pointcloud = False

    def compressed_pointcloud_callback(self, msg: ByteMultiArray):
        """Process a ROS CompressedPointCloud2 message: send as chunked binary over LiveKit."""
        try:
            payload = self._flatten_bytes(msg.data)
            self.get_logger().debug(f"Received compressed pointcloud of size {len(payload)} bytes")
            if len(self.room.remote_participants) == 0:
                return False
            # Skip if a previous send is still in progress
            self._pointcloud_seq += 1
            with self._send_lock:
                if self._is_sending_pointcloud:
                    self.get_logger().debug("Pointcloud send in progress, skipping this frame")
                    return False
                self._is_sending_pointcloud = True
                msg_id = self._pointcloud_seq
            # Schedule chunked publish; flag will be reset in on_done
            self._publish_large_payload(payload, topic="pointcloud", on_done=self._on_pointcloud_send_done, message_id=msg_id)
        except Exception as e:
            # Reset flag on error if we set it
            self.get_logger().error(f"Failed to publish pointcloud data: {e}")
            return False
        return True
        
    async def create_video_track(self, width: int, height: int, fps: int):
        """Create and publish LiveKit video track."""
        self.video_source = rtc.VideoSource(width, height)
        track = rtc.LocalVideoTrack.create_video_track("wrist_camera", self.video_source)
        await self.room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_CAMERA,
                video_encoding=rtc.VideoEncoding(max_framerate=fps, max_bitrate=3_000_000),
                video_codec=rtc.VideoCodec.VP8,
            ),
        )
        self.get_logger().info("Published LiveKit track (will only send frames when someone is watching)")

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