#!/usr/bin/env python3
"""
LiveKit ROS2 Bridge Base - Generic bidirectional bridge between LiveKit and ROS2
Provides generic infrastructure for:
- ROS2 message <-> LiveKit data packet conversion
- Dynamic topic publishing/subscribing
- Service and action client support
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
from rclpy.action import ActionClient
from rclpy.action.client import ClientGoalHandle
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.duration import Duration as RclpyDuration

# Common ROS2 message types
from std_msgs.msg import String, Float64, Float32, Int16, Int32, UInt16, UInt8, Int8, UInt64, Int64, Header
from geometry_msgs.msg import Twist, TwistStamped, PoseStamped, Pose, Point, Quaternion, Vector3
from sensor_msgs.msg import Image, CompressedImage, JointState
from nav_msgs.msg import Odometry
from builtin_interfaces.msg import Time, Duration as MsgDuration
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint


class ROS2MessageFactory:
    """ROS2 message factory for dynamic message type handling"""
    
    @classmethod
    def get_message_class(cls, message_type: str):
        """Get message class from type string (e.g., 'geometry_msgs/msg/Twist')"""
        try:
            parts = message_type.split('/')
            if len(parts) == 3:
                package_name, msg_or_srv, message_name = parts
                module = importlib.import_module(f"{package_name}.{msg_or_srv}")
                return getattr(module, message_name)
            elif len(parts) == 2:
                package_name, message_name = parts
                module = importlib.import_module(package_name)
                return getattr(module, message_name)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"Cannot import message type {message_type}: {e}")
    
    @classmethod
    def json_to_message(cls, message_type: str, data: Dict[str, Any]):
        """Convert JSON dict to ROS2 message"""
        message_class = cls.get_message_class(message_type)
        msg = message_class()
        cls._populate_message(msg, data)
        return msg
    
    @classmethod
    def _populate_message(cls, msg, data: Dict[str, Any]):
        """Recursively populate message fields from dict"""
        for key, value in data.items():
            if not hasattr(msg, key):
                continue
            
            attr = getattr(msg, key)
            
            if isinstance(value, dict):
                cls._populate_message(attr, value)
            elif isinstance(value, list):
                if hasattr(attr, 'append'):
                    for item in value:
                        if isinstance(item, dict):
                            sub_msg = type(attr[0])() if len(attr) > 0 else None
                            if sub_msg:
                                cls._populate_message(sub_msg, item)
                                attr.append(sub_msg)
                        else:
                            attr.append(item)
                else:
                    setattr(msg, key, value)
            else:
                setattr(msg, key, value)
    
    @classmethod
    def message_to_json(cls, msg) -> str:
        """Convert ROS2 message to JSON string"""
        return cls._message_to_dict_json(msg)
    
    @classmethod
    def _message_to_dict_json(cls, msg) -> str:
        """Convert message to JSON with special handling"""
        result = {}
        
        for field in msg.get_fields_and_field_types().keys():
            value = getattr(msg, field)
            
            if hasattr(value, 'get_fields_and_field_types'):
                result[field] = json.loads(cls._message_to_dict_json(value))
            elif isinstance(value, list):
                result[field] = [
                    json.loads(cls._message_to_dict_json(item)) 
                    if hasattr(item, 'get_fields_and_field_types') 
                    else item 
                    for item in value
                ]
            else:
                result[field] = value
        
        return json.dumps(result)


@dataclass
class PublisherInfo:
    """Publisher information"""
    publisher: Publisher
    message_type: str
    topic_name: str
    created_time: float


class LiveKitROS2BridgeBase(Node):
    """
    Base class for LiveKit-ROS2 bridge providing generic bidirectional communication.
    
    Handles:
    - LiveKit data packet reception and routing
    - Dynamic ROS2 publisher/subscriber creation
    - ROS2 service and action client management
    - Message serialization/deserialization
    
    Subclasses should override:
    - setup_subscriptions(): Create specific topic subscriptions
    - Custom callback methods for business logic
    """
    
    def __init__(self, room: rtc.Room, node_name: str = 'livekit_ros2_bridge'):
        super().__init__(node_name)
        
        # LiveKit setup
        self.room = room
        self.room.on("data_received", self.on_data_received)
        self.room.on("participant_connected", self._on_participant_connected)
        self.room.on("participant_disconnected", self._on_participant_disconnected)
        
        # ROS2 infrastructure
        self._topic_publishers: Dict[str, PublisherInfo] = {}
        self.service_clients: Dict[str, Client] = {}
        self.action_clients: Dict[str, ActionClient] = {}
        self.action_goal_handles: Dict[str, ClientGoalHandle] = {}
        
        # QoS configuration
        self.default_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )
        
        # AsyncIO loop for LiveKit operations
        try:
            self._asyncio_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._asyncio_loop = None
        
        # Statistics
        self.error_count = 0
        
        self.get_logger().info(f"LiveKit ROS2 Bridge Base initialized: {node_name}")
    
    def _on_participant_connected(self, participant):
        """Handle participant connection"""
        self.get_logger().info(f"Participant connected: {participant.identity}")
    
    def _on_participant_disconnected(self, participant):
        """Handle participant disconnection"""
        self.get_logger().info(f"Participant disconnected: {participant.identity}")
    
    def setup_subscriptions(self):
        """
        Override this method to set up specific topic subscriptions.
        Called after base initialization.
        """
        pass
    
    def _submit_to_loop(self, coro):
        """Submit coroutine to asyncio loop"""
        if self._asyncio_loop is None:
            self.get_logger().error("No asyncio event loop available")
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, self._asyncio_loop)
        except Exception as e:
            self.get_logger().error(f"Failed to submit coroutine: {e}")
    
    # ========== LiveKit Data Reception ==========
    
    def on_data_received(self, data: rtc.DataPacket):
        """Handle incoming LiveKit data packets"""
        try:
            packet = json.loads(data.data.decode('utf-8'))
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
        """Handle ROS2 message publication from LiveKit"""
        try:
            topic_name = packet['topicName']
            message_type = packet['messageType']
            data = packet['data']
            
            # Get or create publisher
            publisher = self._get_or_create_publisher(topic_name, message_type)
            
            # Convert and publish
            msg = ROS2MessageFactory.json_to_message(message_type, data)
            publisher.publish(msg)
            
        except Exception as e:
            self.get_logger().error(f"Failed to handle ROS2 message: {e}")
    
    def handle_ros2_service_call(self, packet: Dict[str, Any]):
        """Handle ROS2 service call from LiveKit"""
        try:
            service_name = packet['serviceName']
            service_type = packet['serviceType']
            request_data = packet['request']
            request_id = packet.get('requestId', '')
            
            # Get or create service client
            client = self._get_or_create_service_client(service_name, service_type)
            
            # Create request
            request_class = ROS2MessageFactory.get_message_class(service_type + '_Request')
            request = request_class()
            ROS2MessageFactory._populate_message(request, request_data)
            
            # Call service asynchronously
            future = client.call_async(request)
            future.add_done_callback(
                lambda f: self._handle_service_response(f, request_id, service_name)
            )
            
        except Exception as e:
            self.get_logger().error(f"Failed to call service: {e}")
    
    def handle_ros2_action_send_goal(self, packet: Dict[str, Any]):
        """Handle ROS2 action goal from LiveKit"""
        try:
            action_name = packet['actionName']
            action_type = packet['actionType']
            goal_data = packet['goal']
            action_id = packet.get('actionId', '')
            
            # Get or create action client
            client = self._get_or_create_action_client(action_name, action_type)
            
            # Create goal
            goal_class = ROS2MessageFactory.get_message_class(action_type + '_Goal')
            goal = goal_class()
            ROS2MessageFactory._populate_message(goal, goal_data)
            
            # Send goal
            send_goal_future = client.send_goal_async(goal)
            send_goal_future.add_done_callback(
                lambda f: self._handle_action_goal_response(f, action_id, action_name)
            )
            
        except Exception as e:
            self.get_logger().error(f"Failed to send action goal: {e}")
    
    # ========== ROS2 -> LiveKit Publishing ==========
    
    async def send_feedback(self, feedback_data: Dict[str, Any]):
        """Send feedback to LiveKit (override for custom formatting)"""
        try:
            json_str = json.dumps(feedback_data)
            await self.room.local_participant.publish_data(json_str.encode('utf-8'))
        except Exception as e:
            self.get_logger().error(f"Failed to send feedback: {e}")
    
    def publish_to_livekit(self, packet_data: Dict[str, Any]):
        """Publish data packet to LiveKit from ROS callback"""
        self._submit_to_loop(self.send_feedback(packet_data))
    
    # ========== Large Payload Handling (Chunked Transfer) ==========
    
    def publish_large_payload(
        self,
        payload: bytes,
        topic: str,
        message_id: Optional[int] = None,
        chunk_size: int = 50000,
        on_done: Optional[Callable[[Optional[Exception]], None]] = None
    ):
        """
        Publish large binary payload to LiveKit with automatic chunking.
        
        LiveKit has size limits on data packets (~16KB). This method automatically
        splits large payloads into chunks and sends them with metadata for reassembly.
        
        Args:
            payload: Binary data to send
            topic: LiveKit data channel topic name
            message_id: Optional message ID for tracking (auto-generated if None)
            chunk_size: Size of each chunk in bytes (default: 50KB)
            on_done: Optional callback(exception) called when transfer completes
        
        Protocol:
            For each chunk:
            1. Send metadata to '<topic>:meta' with: {id, chunk, total}
            2. Send chunk data to '<topic>'
        
        Thread-safe: Can be called from ROS callbacks
        """
        if self._asyncio_loop is None:
            self.get_logger().error("No asyncio event loop available for large payload publish")
            if on_done:
                on_done(RuntimeError("No asyncio loop available"))
            return
        
        # Generate message ID if not provided
        if message_id is None:
            message_id = int(time.time() * 1000000)  # Microsecond timestamp
        
        async def _publish_chunks():
            """Async coroutine to publish chunks sequentially"""
            total_chunks = (len(payload) + chunk_size - 1) // chunk_size
            
            for chunk_idx in range(total_chunks):
                start_pos = chunk_idx * chunk_size
                end_pos = min(start_pos + chunk_size, len(payload))
                chunk = payload[start_pos:end_pos]
                
                # Send metadata first
                metadata = json.dumps({
                    "id": message_id,
                    "chunk": chunk_idx,
                    "total": total_chunks,
                    "size": len(chunk)
                }).encode('utf-8')
                
                await self.room.local_participant.publish_data(
                    metadata, 
                    topic=f"{topic}:meta"
                )
                
                # Then send chunk data
                await self.room.local_participant.publish_data(
                    chunk,
                    topic=topic
                )
            
            self.get_logger().debug(
                f"Large payload sent: {len(payload)} bytes in {total_chunks} chunks to '{topic}'"
            )
        
        # Submit to asyncio loop
        future = asyncio.run_coroutine_threadsafe(_publish_chunks(), self._asyncio_loop)
        
        # Attach completion callback
        def _done_callback(f: asyncio.Future):
            exc: Optional[Exception] = None
            try:
                f.result()
            except Exception as e:
                exc = e
                self.get_logger().error(
                    f"Failed to publish large payload to '{topic}': {e}"
                )
            
            # Call user callback if provided
            if on_done is not None:
                try:
                    on_done(exc)
                except Exception as cb_e:
                    self.get_logger().error(
                        f"on_done callback error for topic '{topic}': {cb_e}"
                    )
        
        future.add_done_callback(_done_callback)
    
    async def publish_large_payload_async(
        self,
        payload: bytes,
        topic: str,
        message_id: Optional[int] = None,
        chunk_size: int = 50000
    ):
        """
        Async version of publish_large_payload for use in async contexts.
        
        Args:
            payload: Binary data to send
            topic: LiveKit data channel topic name
            message_id: Optional message ID for tracking
            chunk_size: Size of each chunk in bytes
        
        Raises:
            Exception if publish fails
        """
        if message_id is None:
            message_id = int(time.time() * 1000000)
        
        total_chunks = (len(payload) + chunk_size - 1) // chunk_size
        
        for chunk_idx in range(total_chunks):
            start_pos = chunk_idx * chunk_size
            end_pos = min(start_pos + chunk_size, len(payload))
            chunk = payload[start_pos:end_pos]
            
            # Send metadata
            metadata = json.dumps({
                "id": message_id,
                "chunk": chunk_idx,
                "total": total_chunks,
                "size": len(chunk)
            }).encode('utf-8')
            
            await self.room.local_participant.publish_data(
                metadata,
                topic=f"{topic}:meta"
            )
            
            # Send chunk
            await self.room.local_participant.publish_data(
                chunk,
                topic=topic
            )
        
        self.get_logger().debug(
            f"Large payload sent (async): {len(payload)} bytes in {total_chunks} chunks"
        )
    
    # ========== Dynamic Publisher/Client Management ==========
    
    def _get_or_create_publisher(self, topic_name: str, message_type: str) -> Publisher:
        """Get existing or create new publisher"""
        key = f"{topic_name}:{message_type}"
        
        if key in self._topic_publishers:
            return self._topic_publishers[key].publisher
        
        # Create new publisher
        msg_class = ROS2MessageFactory.get_message_class(message_type)
        publisher = self.create_publisher(msg_class, topic_name, self.default_qos)
        
        self._topic_publishers[key] = PublisherInfo(
            publisher=publisher,
            message_type=message_type,
            topic_name=topic_name,
            created_time=time.time()
        )
        
        self.get_logger().info(f"Created publisher for {topic_name} ({message_type})")
        return publisher
    
    def _get_or_create_service_client(self, service_name: str, service_type: str) -> Client:
        """Get existing or create new service client"""
        if service_name in self.service_clients:
            return self.service_clients[service_name]
        
        srv_class = ROS2MessageFactory.get_message_class(service_type)
        client = self.create_client(srv_class, service_name)
        self.service_clients[service_name] = client
        
        self.get_logger().info(f"Created service client for {service_name}")
        return client
    
    def _get_or_create_action_client(self, action_name: str, action_type: str) -> ActionClient:
        """Get existing or create new action client"""
        if action_name in self.action_clients:
            return self.action_clients[action_name]
        
        action_class = ROS2MessageFactory.get_message_class(action_type)
        client = ActionClient(self, action_class, action_name)
        self.action_clients[action_name] = client
        
        self.get_logger().info(f"Created action client for {action_name}")
        return client
    
    # ========== Callbacks for Service/Action Results ==========
    
    def _handle_service_response(self, future, request_id: str, service_name: str):
        """Handle service response"""
        try:
            response = future.result()
            response_json = ROS2MessageFactory.message_to_json(response)
            
            self._submit_to_loop(self.send_feedback({
                'packetType': 'ros2_service_response',
                'serviceName': service_name,
                'requestId': request_id,
                'response': json.loads(response_json)
            }))
        except Exception as e:
            self.get_logger().error(f"Service call failed: {e}")
    
    def _handle_action_goal_response(self, future, action_id: str, action_name: str):
        """Handle action goal acceptance"""
        try:
            goal_handle = future.result()
            if not goal_handle.accepted:
                self.get_logger().warn(f"Action goal rejected: {action_name}")
                return
            
            self.action_goal_handles[action_id] = goal_handle
            
            # Wait for result
            result_future = goal_handle.get_result_async()
            result_future.add_done_callback(
                lambda f: self._handle_action_result(f, action_id, action_name)
            )
        except Exception as e:
            self.get_logger().error(f"Action goal response failed: {e}")
    
    def _handle_action_result(self, future, action_id: str, action_name: str):
        """Handle action result"""
        try:
            result = future.result().result
            result_json = ROS2MessageFactory.message_to_json(result)
            
            self._submit_to_loop(self.send_feedback({
                'packetType': 'ros2_action_result',
                'actionName': action_name,
                'actionId': action_id,
                'result': json.loads(result_json)
            }))
            
            # Clean up
            if action_id in self.action_goal_handles:
                del self.action_goal_handles[action_id]
                
        except Exception as e:
            self.get_logger().error(f"Action result failed: {e}")
