#!/usr/bin/env python3
"""
消息处理器模块，包含从LiveKit接收的消息的处理器类
"""

from abc import ABC, abstractmethod
import rclpy
from rclpy.action import ActionClient
from geometry_msgs.msg import PoseStamped, Twist
from std_msgs.msg import Header
from moveit_msgs.action import MoveGroup


class MessageHandler(ABC):
    """Base abstract class for handling messages from LiveKit."""
    
    def __init__(self, ros_node):
        """Initialize with a ROS node for publishing."""
        self.ros_node = ros_node
    
    @abstractmethod
    def process(self, data_dict):
        """Process the data dictionary and perform appropriate actions."""
        pass


class PoseMessageHandler(MessageHandler):
    """Handler for pose messages from LiveKit."""
    
    def __init__(self, ros_node):
        """Initialize with a ROS node and create a pose publisher."""
        super().__init__(ros_node)
        self.publisher = ros_node.create_publisher(PoseStamped, '/ee_pose', 10)
    
    def process(self, data_dict):
        """Process pose data and publish a PoseStamped message."""
        position = data_dict.get("position", {})
        rotation = data_dict.get("rotation", {})
        timestamp = data_dict.get("timestamp", 0)
        
        pose_msg = self._convert_to_pose_stamped(position, rotation, timestamp)
        self.publisher.publish(pose_msg)
        self.ros_node.get_logger().debug(f"Published pose: {pose_msg.pose.position}")
        return pose_msg
    
    def _convert_to_pose_stamped(self, position, rotation, timestamp):
        """Convert position and rotation data to ROS PoseStamped message."""
        pose_msg = PoseStamped()
        # Set header
        pose_msg.header = Header()
        pose_msg.header.stamp = rclpy.time.Time(nanoseconds=int(timestamp * 1e6)).to_msg()
        pose_msg.header.frame_id = "livekit_frame"
        
        # Set position
        pose_msg.pose.position.x = float(position.get("x", 0.0))
        pose_msg.pose.position.y = float(position.get("y", 0.0))
        pose_msg.pose.position.z = float(position.get("z", 0.0))
        
        # Set orientation
        pose_msg.pose.orientation.x = float(rotation.get("x", 0.0))
        pose_msg.pose.orientation.y = float(rotation.get("y", 0.0))
        pose_msg.pose.orientation.z = float(rotation.get("z", 0.0))
        pose_msg.pose.orientation.w = float(rotation.get("w", 1.0))
        
        return pose_msg


class TwistMessageHandler(MessageHandler):
    """Handler for twist/velocity messages from LiveKit."""
    
    def __init__(self, ros_node):
        """Initialize with a ROS node and create a twist publisher."""
        super().__init__(ros_node)
        self.publisher = ros_node.create_publisher(Twist, '/cmd_vel', 10)
    
    def process(self, data_dict):
        """Process velocity data and publish a Twist message."""
        linear_velocity = data_dict.get("linear", {})
        angular_velocity = data_dict.get("angular", {})
        
        if linear_velocity is None:
            linear_velocity = {}
        if angular_velocity is None:
            angular_velocity = {}
            
        twist_msg = self._convert_to_twist(linear_velocity, angular_velocity)
        self.publisher.publish(twist_msg)
        self.ros_node.get_logger().debug(f"Published twist: {twist_msg.linear}")
        return twist_msg
    
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


class MotionCommandHandler(MessageHandler):
    """Handler for motion command messages from LiveKit."""
    
    def __init__(self, ros_node):
        """Initialize with a ROS node and create an action client."""
        super().__init__(ros_node)
        self.move_group_client = ActionClient(ros_node, MoveGroup, '/move_action')
    
    def process(self, data_dict):
        """Process motion command data and take appropriate action."""
        if data_dict.get("arm_motion") == "arm_ready_to_pick":
            self.ros_node.get_logger().info("Initiating arm_ready_to_pick motion")
            self._send_move_group_goal()
        return data_dict
    
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


class MessageHandlerFactory:
    """Factory for creating appropriate message handlers based on topic."""
    
    @staticmethod
    def create_handler(topic, ros_node):
        """Create and return an appropriate handler for the given topic."""
        if topic == "ee_pose":
            return PoseMessageHandler(ros_node)
        elif topic == "velocity":
            return TwistMessageHandler(ros_node)
        elif topic == "motion_command":
            return MotionCommandHandler(ros_node)
        else:
            return None
