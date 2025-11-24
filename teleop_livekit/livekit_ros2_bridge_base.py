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
        if message_class is None:
            raise ValueError(f"Unsupported message type: {message_type}")
        
        msg = message_class()
        cls._populate_message(msg, data)
        return msg
    
    @classmethod
    def _populate_message(cls, msg, data: Dict[str, Any]):
        """Populate message data"""
        for key, value in data.items():
            if not hasattr(msg, key):
                continue
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
    
    @classmethod
    def message_to_json(cls, msg) -> str:
        """Convert ROS2 message to JSON string"""
        return json.dumps(cls._message_to_dict_json(msg))
    
    @classmethod
    def _message_to_dict_json(cls, msg) -> str:
        """Convert ROS2 message instance to a JSON-serializable structure"""
        if msg is None:
            return None
        if hasattr(msg, 'get_fields_and_field_types'):
            serialized = {}
            for field_name in msg.get_fields_and_field_types().keys():
                value = getattr(msg, field_name)
                serialized[field_name] = cls._convert_value(value)
            return serialized
        return cls._convert_value(msg)
    
    @classmethod
    def _convert_value(cls, value: Any) -> Any:
        """Convert individual field to JSON-serializable value"""
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            return list(value)
        if isinstance(value, (list, tuple)):
            return [cls._convert_value(item) for item in value]
        if isinstance(value, dict):
            return {key: cls._convert_value(item) for key, item in value.items()}
        if hasattr(value, 'get_fields_and_field_types'):
            return cls._message_to_dict_json(value)
        return str(value)

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
            action_name = packet.get('actionName', '')
            action_type = packet.get('actionType', '')
            goal_data = packet.get('goal', {})
            goal_id = packet.get('goalId', '')
            
            # Get or create action client
            client = self._get_or_create_action_client(action_name, action_type)

            if client is None:
                raise ValueError(f"Failed to create action client for {action_name}")

            # Wait for action server to be available
            if not client.wait_for_server(timeout_sec=5.0):
                self.get_logger().error(f"Action server {action_name} not available")
                self._submit_to_loop(self.send_feedback({
                    'packetType': 'ros2_action_response',
                    'goalId': goal_id,
                    'status': 'server_not_available',
                    'message': f'Action server {action_name} not available'
                }))
                return
            
            # Create goal
            goal_class = ROS2MessageFactory.get_message_class(action_type)
            if goal_class is None:
                raise ValueError(f"Unsupported action type: {action_type}")
            
            # Create goal message
            goal_msg = goal_class.Goal()
            ROS2MessageFactory._populate_message(goal_msg, goal_data)
            
            # Send goal
            self.get_logger().info(f"Sending goal to action {action_name}")
            send_goal_future = client.send_goal_async(goal_msg)
            send_goal_future.add_done_callback(
                lambda f: self._handle_action_goal_response(f, goal_id)
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
                'requestId': request_id,
                'serviceName': service_name,
                'response': json.loads(response_json)
            }))
        except Exception as e:
            self.get_logger().error(f"Service call failed: {e}")

    def handle_ros2_action_feedback(self, goal_id: str, feedback_msg):
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
    
    def _handle_action_goal_response(self, future, goal_id: str):
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
                lambda result_future: self._handle_action_result(result_future, goal_id)
            )
            
        except Exception as e:
            self.get_logger().error(f"Error in action goal response callback: {e}")
            self._submit_to_loop(self.send_feedback({
                'packetType': 'ros2_action_response',
                'goalId': goal_id,
                'status': 'error',
                'message': str(e)
            }))
    
    def _handle_action_result(self, future, goal_id: str):
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
