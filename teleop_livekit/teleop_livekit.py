#!/usr/bin/env python3
import asyncio
import logging
import os
import threading
import yaml
from abc import ABC, abstractmethod

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from sensor_msgs.msg import Image
from geometry_msgs.msg import Pose, PoseStamped, Twist, Vector3, Quaternion
from std_msgs.msg import Header
from moveit_msgs.action import MoveGroup
from ament_index_python.packages import get_package_share_directory

import json
from livekit import api, rtc
from signal import SIGINT, SIGTERM


# 基础消息处理器类
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


# 消息处理器工厂
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


class LiveKitPublisher:
    def __init__(self, url, token):
        """Initialize the LiveKit publisher with connection details."""
        self.url = url
        self.token = token
        self.room = rtc.Room()
        self.remote_counter = {"count": 0}
        self.source = None
        self.message_handlers = {}
        
    async def connect(self):
        """Connect to the LiveKit room and set up event handlers."""
        await self.room.connect(self.url, self.token)
        logging.info("Connected to LiveKit room: [%s]", self.room.name)
        self._setup_event_handlers()
    
    def register_handler(self, topic, handler):
        """Register a message handler for a specific topic."""
        self.message_handlers[topic] = handler
        logging.info(f"Registered handler for topic: {topic}")
    
    def register_handlers_from_node(self, ros_node):
        """Register all available message handlers from a ROS node."""
        # Register standard handlers
        topics = ["ee_pose", "velocity", "motion_command"]
        for topic in topics:
            handler = MessageHandlerFactory.create_handler(topic, ros_node)
            if handler:
                self.register_handler(topic, handler)
    
    def _setup_event_handlers(self):
        """Set up handlers for participant connect/disconnect events."""
        @self.room.on("participant_connected")
        def _on_connect(participant: rtc.Participant):
            self.remote_counter["count"] += 1
            logging.info("Participant connected: %s (total=%d)",
                        participant.identity, self.remote_counter["count"])

        @self.room.on("participant_disconnected")
        def _on_disconnect(participant: rtc.Participant):
            self.remote_counter["count"] = max(0, self.remote_counter["count"] - 1)
            logging.info("Participant disconnected: %s (total=%d)",
                        participant.identity, self.remote_counter["count"])
            
        @self.room.on("data_received")
        def on_data_received(data: rtc.DataPacket):
            if (data.data is None or not data.data):
                logging.warning("Received empty data packet from %s", data.participant.identity)
                return
            
            logging.info("received data from %s topic %s: %s", data.participant.identity, 
                        data.topic, data.data)
                        
            # 使用注册的处理器处理消息
            handler = self.message_handlers.get(data.topic)
            if handler:
                try:
                    data_dict = json.loads(data.data.decode('utf-8'))
                    handler.process(data_dict)
                except json.JSONDecodeError:
                    logging.error("Failed to decode JSON data: %s", data.data.decode('utf-8'))
                except Exception as e:
                    logging.error(f"Error processing message on topic {data.topic}: {e}")
            else:
                logging.warning(f"No handler registered for topic: {data.topic}")
    
    def create_video_source(self, width, height):
        """Create a video source with the specified dimensions."""
        self.source = rtc.VideoSource(width, height)
        return self.source
    
    async def publish_video_track(self, width, height, fps):
        """Publish a video track with the specified parameters."""
        self.source = self.create_video_source(width, height)
        track = rtc.LocalVideoTrack.create_video_track("ros_image", self.source)
        await self.room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_CAMERA,
                video_encoding=rtc.VideoEncoding(max_framerate=fps, max_bitrate=3_000_000),
                video_codec=rtc.VideoCodec.AV1,
            )
        )
        logging.info("Published LiveKit track (will only send frames when someone is watching)")
        return self.source
    
    async def disconnect(self):
        """Disconnect from the LiveKit room."""
        await self.room.disconnect()
        logging.info("Disconnected from LiveKit room")


class LiveKitClient(Node):
    def __init__(self, source: rtc.VideoSource, remote_counter: dict):
        super().__init__('livekit_image_publisher')
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


async def main():
    # Load configuration
    pkg_share = get_package_share_directory('orbbec_video_stream_livekit')
    
    with open(os.path.join(pkg_share, 'config', 'livekit.yaml')) as f:
        livekit_cfg = yaml.safe_load(f)
    
    with open(os.path.join(pkg_share, 'config', 'orbbec.yaml')) as f:
        video_cfg = yaml.safe_load(f)
    
    width, height, fps = video_cfg['color_width'], video_cfg['color_height'], video_cfg['color_fps']

    # Initialize and connect LiveKit publisher
    publisher = LiveKitPublisher(livekit_cfg['url'], livekit_cfg['token'])
    await publisher.connect()
    source = await publisher.publish_video_track(width, height, fps)

    # Initialize ROS node
    rclpy.init()
    ros_node = LiveKitClient(source, publisher.remote_counter)
    
    # Register message handlers for all supported topics
    publisher.register_handlers_from_node(ros_node)
    
    def ros_spin():
        rclpy.spin(ros_node)
    
    threading.Thread(target=ros_spin, daemon=True).start()

    # Wait for termination signal
    stop = asyncio.Event()
    for sig in (SIGINT, SIGTERM):
        asyncio.get_event_loop().add_signal_handler(sig, stop.set)
    await stop.wait()

    # Clean up
    ros_node.destroy_node()
    rclpy.shutdown()
    await publisher.disconnect()


def run():
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()