#!/usr/bin/env python3
"""
消息处理器模块，包含从LiveKit接收的消息的处理器类
采用缓存+定时发布模式，避免在LiveKit回调中直接发布消息
"""

from abc import ABC, abstractmethod
import threading
import time
import rclpy
from rclpy.action import ActionClient
from geometry_msgs.msg import PoseStamped, Twist, Pose
from std_msgs.msg import Header
from moveit_msgs.action import MoveGroup
from tf2_ros import Buffer, TransformListener
from rclpy.duration import Duration
from tf2_geometry_msgs import do_transform_pose

class MessageHandler(ABC):
    """Base abstract class for handling messages from LiveKit.
    
    1. process() 方法只缓存数据，不直接发布
    2. 每种类型的Handler有自己的发布策略
    3. 支持不同的发布模式：定时发布、立即执行、异步处理
    """
    
    def __init__(self, ros_node):
        """Initialize with a ROS node for publishing."""
        self.ros_node = ros_node
        self._cached_data = None
        self._data_lock = threading.RLock()
        self._last_update_time = 0
        self.publish_mode = "immediate"  # "timer", "immediate", "async"
    
    @abstractmethod
    def process(self, data_dict):
        """Process and cache the data dictionary."""
        pass
    
    def has_new_data(self, max_age_seconds=1.0):
        """Check if there's new data within the specified age."""
        with self._data_lock:
            if self._cached_data is None:
                return False
            return (time.time() - self._last_update_time) < max_age_seconds
    
    def get_publish_mode(self):
        """Get the publishing mode for this handler."""
        return self.publish_mode


class TimedMessageHandler(MessageHandler):
    """Base class for handlers that need timed publishing (Pose, Twist, etc.)."""
    
    def __init__(self, ros_node, publish_rate_hz=30.0):
        """Initialize with a ROS node and create a timer for publishing."""
        super().__init__(ros_node)
        self.publish_mode = "timer"
        self.publish_rate_hz = publish_rate_hz
        self.timer = None
        self.is_running = False
        
    @abstractmethod
    def get_cached_message(self):
        """Get the latest cached message for publishing. Returns None if no data."""
        pass
    
    @abstractmethod
    def publish_cached_message(self):
        """Publish the cached message if available."""
        pass
        
    def start_publishing(self):
        """Start the periodic publishing timer."""
        if self.is_running:
            self.ros_node.get_logger().warning(f"Timer already running for {self.__class__.__name__}")
            return
        
        timer_period = 1.0 / self.publish_rate_hz
        self.timer = self.ros_node.create_timer(timer_period, self._publish_callback)
        self.is_running = True
        
        self.ros_node.get_logger().info(f"Started {self.__class__.__name__} timer at {self.publish_rate_hz} Hz")
    
    def stop_publishing(self):
        """Stop the periodic publishing timer."""
        if self.timer is not None:
            self.timer.destroy()
            self.timer = None
            
        self.is_running = False
        self.ros_node.get_logger().info(f"Stopped {self.__class__.__name__} timer")
    
    def _publish_callback(self):
        """Timer callback to publish cached messages."""
        try:
            # Only publish if there's recent data
            if self.has_new_data(max_age_seconds=1.0):
                self.publish_cached_message()
        except Exception as e:
            self.ros_node.get_logger().error(f"Error in {self.__class__.__name__} publishing: {e}")


class ImmediateMessageHandler(MessageHandler):
    """Base class for handlers that execute immediately (Service calls, discrete actions)."""
    
    def __init__(self, ros_node):
        """Initialize with immediate execution mode."""
        super().__init__(ros_node)
        self.publish_mode = "immediate"
    
    def process(self, data_dict):
        """Process data and execute immediately."""
        with self._data_lock:
            self._cached_data = data_dict.copy()
            self._last_update_time = time.time()
        
        # Execute immediately
        self.execute_command(data_dict)
    
    @abstractmethod
    def execute_command(self, data_dict):
        """Execute the command immediately."""
        pass


class PoseMessageHandler(TimedMessageHandler):
    """Handler for pose messages from LiveKit. Uses high-frequency timed publishing (30Hz)."""

    def __init__(self, ros_node, publish_rate_hz=30.0):
        """Initialize with a ROS node and create a pose publisher with 30Hz rate."""
        super().__init__(ros_node, publish_rate_hz)
        self.publisher = ros_node.create_publisher(PoseStamped, '/servo_node/pose_target_cmds', 10)
        # TF utilities
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, ros_node, spin_thread=True)
        # Frames
        self.ee_frame = 'link_grasp_center'
        self.base_frame = 'base_link'
        self._init_ee2base = None
        # debug
        self.debug_publisher = self.ros_node.create_publisher(PoseStamped, '/debug/ee_pose', 10)

    def _ensure_init_pose(self, force: bool = False):
        """Capture the initial absolute pose of ee_frame in base_frame using TF."""
        if self._init_ee2base is not None and not force:
            return True
        try:
            # Store as tuple for fast access: (px,py,pz, qx,qy,qz,qw)
            self._init_ee2base = self.tf_buffer.lookup_transform(self.base_frame, self.ee_frame, rclpy.time.Time(), timeout=Duration(seconds=1.0))
            self.ros_node.get_logger().info(
                f"[PoseHandler] Captured init_pose of {self.ee_frame} in {self.base_frame}: "
                f"Transform: {self._init_ee2base.transform.translation}, Rotation: {self._init_ee2base.transform.rotation}")
            return True
        except Exception as e:
            self.ros_node.get_logger().error(f"[PoseHandler] Failed to capture init_pose via TF: {e}")
            return False

    def process(self, data_dict):
        """Process and cache pose data."""
        with self._data_lock:
            self._cached_data = data_dict.copy()
            self._last_update_time = time.time()
        self.ros_node.get_logger().debug(f"Cached pose data: {data_dict.get('position', {})}")

    def get_cached_message(self):
        """Get the latest cached pose message."""
        with self._data_lock:
            if self._cached_data is None:
                return None
            position = self._cached_data.get("p", {})
            rotation = self._cached_data.get("q", {})
            timestamp = self._cached_data.get("timestamp", time.time())
            first_enter = self._cached_data.get("first_enter", 0)
        
        # Handle first_enter flag: capture init pose
        if first_enter == 1:
            self._ensure_init_pose(force=True)

        if self._ensure_init_pose() is False:
            self.ros_node.get_logger().error("Failed to ensure initial pose")
            return None

        return self._convert_to_tcp_pose(position, rotation, timestamp)

    def publish_cached_message(self):
        """Publish the cached pose message if available."""
        msg = self.get_cached_message()
        if msg is not None:
            self.publisher.publish(msg)
            #debug
            self.ros_node.get_logger().debug(
                f"Published pose in frame '{msg.header.frame_id}': p=({msg.pose.position.x:.3f},{msg.pose.position.y:.3f},{msg.pose.position.z:.3f})")
            self.debug_publisher.publish(msg)
            return True
        return False
    
    def _convert_to_tcp_pose(self, position, rotation, timestamp):

        # Build relative transform from VR (keep existing axis mapping)
        delta_pose = Pose()
        delta_pose.position.x = position[0]
        delta_pose.position.y = position[1]
        delta_pose.position.z = position[2]
        delta_pose.orientation.x = rotation[0]
        delta_pose.orientation.y = rotation[1]
        delta_pose.orientation.z = rotation[2]
        delta_pose.orientation.w = rotation[3]

        # print self._init_pose
        new_ee_pose = do_transform_pose(delta_pose, self._init_ee2base)

        if new_ee_pose is None:
            self.ros_node.get_logger().error("Failed to transform pose using TF")
            return None

        # Build PoseStamped
        pose_msg = PoseStamped()
        pose_msg.header = Header()
        pose_msg.header.stamp = self.ros_node.get_clock().now().to_msg()
        pose_msg.header.frame_id = self.base_frame
        pose_msg.pose = new_ee_pose
        return pose_msg


class TwistMessageHandler(TimedMessageHandler):
    """Handler for twist/velocity messages from LiveKit. Uses medium-frequency timed publishing (30Hz)."""
    
    def __init__(self, ros_node, publish_rate_hz=30.0):
        """Initialize with a ROS node and create a twist publisher with 30Hz rate."""
        super().__init__(ros_node, publish_rate_hz)
        self.publisher = ros_node.create_publisher(Twist, '/cmd_vel', 10)
    
    def process(self, data_dict):
        """Process and cache velocity data."""
        with self._data_lock:
            self._cached_data = data_dict.copy()
            self._last_update_time = time.time()
            
        self.ros_node.get_logger().debug(f"Cached twist data: {data_dict.get('linear', {})}")
    
    def get_cached_message(self):
        """Get the latest cached twist message."""
        with self._data_lock:
            if self._cached_data is None:
                return None
                
            linear_velocity = self._cached_data.get("linear", {})
            angular_velocity = self._cached_data.get("angular", {})
            
            if linear_velocity is None:
                linear_velocity = {}
            if angular_velocity is None:
                angular_velocity = {}
                
            return self._convert_to_twist(linear_velocity, angular_velocity)
    
    def publish_cached_message(self):
        """Publish the cached twist message if available."""
        msg = self.get_cached_message()
        if msg is not None:
            self.publisher.publish(msg)
            self.ros_node.get_logger().debug(f"Published cached twist: {msg.linear}")
            return True
        return False
    
    def _convert_to_twist(self, linear_velocity, angular_velocity):
        """Convert velocity data to ROS Twist message."""
        twist_msg = Twist()
        
        # Set linear velocity - explicitly convert all values to float
        twist_msg.linear.x = float(linear_velocity.get("x", 0.0))
        twist_msg.linear.y = float(linear_velocity.get("y", 0.0))
        twist_msg.linear.z = float(linear_velocity.get("z", 0.0))
        
        # Set angular velocity - explicitly convert all values to float
        twist_msg.angular.x = float(angular_velocity.get("x", 0.0))
        twist_msg.angular.y = float(angular_velocity.get("y", 0.0))
        twist_msg.angular.z = float(angular_velocity.get("z", 0.0))
        
        return twist_msg


class MotionCommandHandler(ImmediateMessageHandler):
    """Handler for motion command messages from LiveKit. 
    
    Uses immediate execution for discrete actions like service calls and action requests.
    """
    
    def __init__(self, ros_node):
        """Initialize with a ROS node and create an action client."""
        super().__init__(ros_node)
        self.move_group_client = ActionClient(ros_node, MoveGroup, '/move_action')
    
    def execute_command(self, data_dict):
        """Execute the motion command immediately."""
        command_type = data_dict.get("arm_motion")
        
        if command_type == "arm_ready_to_pick":
            self.ros_node.get_logger().info("Executing arm_ready_to_pick motion immediately")
            self._send_move_group_goal()
        elif command_type:
            self.ros_node.get_logger().warning(f"Unknown motion command: {command_type}")
        
        self.ros_node.get_logger().debug(f"Executed motion command: {data_dict}")
    
    def _send_move_group_goal(self):
        """Send a MoveGroup action goal."""
        # Wait for the action server to be available
        if not self.move_group_client.wait_for_server(timeout_sec=1.0):
            self.ros_node.get_logger().error('MoveGroup action server not available')
            return
            
        # Create and send the goal
        goal_msg = MoveGroup.Goal()
        # Configure your MoveGroup goal here based on application needs
        
        self.ros_node.get_logger().info('Sending MoveGroup action goal')
        
        # Send the goal and register callbacks
        send_goal_future = self.move_group_client.send_goal_async(goal_msg)
        send_goal_future.add_done_callback(self._goal_response_callback)
    
    def _goal_response_callback(self, future):
        """Handle the goal response."""
        goal_handle = future.result()
        
        if not goal_handle.accepted:
            self.ros_node.get_logger().error('MoveGroup goal rejected')
            return
            
        self.ros_node.get_logger().info('MoveGroup goal accepted')
        
        # Request the result
        get_result_future = goal_handle.get_result_async()
        get_result_future.add_done_callback(self._get_result_callback)
    
    def _get_result_callback(self, future):
        """Handle the action result."""
        result = future.result().result
        self.ros_node.get_logger().info(f'MoveGroup action completed with result: {result}')


class MessageHandlerManager:
    """
    消息处理管理器 - 为不同类型的Handler提供独立的发布策略
    
    支持三种发布模式：
    1. Timer模式：高频连续数据 (Pose: 50Hz, Twist: 30Hz)  
    2. Immediate模式：离散命令立即执行 (MotionCommand)
    3. Async模式：异步处理 (未来扩展用)
    """
    
    def __init__(self, ros_node):
        """Initialize the message handler manager."""
        self.ros_node = ros_node
        self.handlers = {}
        self.timer_handlers = []  # Track handlers that need timers
        
    def register_handler(self, topic, handler):
        """Register a message handler with appropriate management strategy."""
        if not isinstance(handler, MessageHandler):
            raise ValueError("Handler must be a MessageHandler instance")
        
        self.handlers[topic] = handler
        
        # Start timer for TimedMessageHandler instances
        if isinstance(handler, TimedMessageHandler):
            handler.start_publishing()
            self.timer_handlers.append(handler)
            self.ros_node.get_logger().info(f"Registered timed handler for {topic} at {handler.publish_rate_hz} Hz")
        else:
            self.ros_node.get_logger().info(f"Registered immediate handler for {topic}")
    
    def process_message(self, topic, data_dict):
        """Process a message for the given topic."""
        handler = self.handlers.get(topic)
        if handler:
            handler.process(data_dict)
            return True
        else:
            self.ros_node.get_logger().warning(f"No handler registered for topic: {topic}")
            return False
    
    def stop_all(self):
        """Stop all timer-based handlers."""
        for handler in self.timer_handlers:
            handler.stop_publishing()
        
        self.ros_node.get_logger().info("Stopped all message handlers")
    
    def get_handler_info(self):
        """Get information about all registered handlers."""
        info = {}
        for topic, handler in self.handlers.items():
            info[topic] = {
                'type': handler.__class__.__name__,
                'mode': handler.get_publish_mode(),
                'rate': getattr(handler, 'publish_rate_hz', None)
            }
        return info


class MessageHandlerFactory:
    """Factory for creating appropriate message handlers based on topic."""
    
    @staticmethod
    def create_handler(topic, ros_node):
        """Create and return an appropriate handler for the given topic."""
        if topic == "ee_pose":
            return PoseMessageHandler(ros_node, publish_rate_hz=30.0)  # High freq for pose
        elif topic == "velocity":
            return TwistMessageHandler(ros_node, publish_rate_hz=30.0)  # Medium freq for velocity
        elif topic == "motion_command":
            return MotionCommandHandler(ros_node)  # Immediate execution
        else:
            return None
    
    @staticmethod
    def create_manager_with_handlers(ros_node, topics=None):
        """Create a MessageHandlerManager with all standard handlers registered.
        
        Args:
            ros_node: The ROS node instance
            topics: List of topics to register. If None, registers all standard topics.
        
        Returns:
            MessageHandlerManager instance with handlers registered
        """
        if topics is None:
            topics = ["ee_pose", "velocity", "motion_command"]
            
        manager = MessageHandlerManager(ros_node)
        
        for topic in topics:
            handler = MessageHandlerFactory.create_handler(topic, ros_node)
            if handler:
                manager.register_handler(topic, handler)
                
        return manager
    
    @staticmethod
    def get_handler_config():
        """Get the configuration for all handler types."""
        return {
            "ee_pose": {
                "type": "TimedMessageHandler", 
                "publish_rate_hz": 50.0,
                "description": "End-effector pose - high frequency for smooth control"
            },
            "velocity": {
                "type": "TimedMessageHandler",
                "publish_rate_hz": 30.0, 
                "description": "Velocity commands - medium frequency for responsive control"
            },
            "motion_command": {
                "type": "ImmediateMessageHandler",
                "publish_rate_hz": None,
                "description": "Discrete motion commands - immediate execution"
            }
        }
