#!/usr/bin/env python3
"""
Teleop Bridge - Business logic for robot teleoperation via LiveKit
Handles specific topics with filtering, throttling, and change detection
"""

import time
import json
from typing import Optional

from livekit import rtc
from sensor_msgs.msg import Image, JointState
from nav_msgs.msg import Odometry
from std_msgs.msg import ByteMultiArray
from geometry_msgs.msg import Pose
import rclpy.qos

from .livekit_ros2_bridge_base import LiveKitROS2BridgeBase, ROS2MessageFactory


class TeleopBridge(LiveKitROS2BridgeBase):
    """
    Teleoperation-specific bridge with business logic for:
    - Video streaming (image/compressed point cloud)
    - Robot state feedback (odometry, joint states)
    - Change detection and throttling
    """
    
    def __init__(self, room: rtc.Room, node_name: str = 'teleop_bridge'):
        super().__init__(room, node_name)
        
        # Video streaming
        self.video_source = None
        
        # Point cloud streaming
        self._send_lock = __import__('threading').Lock()
        self._is_sending_pointcloud = False
        self._pointcloud_seq = 0
        
        # Odometry feedback with change detection
        self._odometry_publish_interval = 0.2  # 5 Hz max
        self._last_odometry_publish_time = 0.0
        self._last_sent_pose: Optional[Pose] = None
        self._pose_position_threshold = 0.001  # 1mm
        self._pose_orientation_threshold = 0.01  # ~0.57 degrees
        
        # Joint state feedback with change detection
        self._joint_state_publish_interval = 0.2  # 5 Hz max
        self._last_joint_state_publish_time = 0.0
        self._last_sent_joint_positions: Optional[list] = None
        self._joint_position_threshold = 0.001  # ~0.057 degrees
        
        # Set up topic subscriptions
        self.setup_subscriptions()
        
        self.get_logger().info("Teleop bridge initialized")
    
    def setup_subscriptions(self):
        """Set up specific topic subscriptions for teleoperation"""
        # Image streaming
        self.image_subscriber = self.create_subscription(
            Image, '/image_raw', self.image_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=1)
        )
        
        # Compressed point cloud streaming
        self.compressed_pointcloud_subscriber = self.create_subscription(
            ByteMultiArray, '/compressed_pointcloud', self.compressed_pointcloud_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=1)
        )
        
        # Robot state feedback
        self.odometry_subscriber = self.create_subscription(
            Odometry, '/odin1/odometry', self.odometry_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=1)
        )
        
        self.joint_state_subscriber = self.create_subscription(
            JointState, '/joint_states', self.joint_state_callback,
            qos_profile=rclpy.qos.QoSProfile(depth=1)
        )
        
        self.get_logger().info("Teleop subscriptions created")
    
    # ========== Video Streaming ==========
    
    def image_callback(self, msg: Image):
        """Handle image messages for video streaming"""
        try:
            if self.video_source is None:
                self._create_video_track()
        
            if len(self.room.remote_participants) == 0:
                self.get_logger().info("No participants connected, skipping frame upload", throttle_duration_sec=5)
                return False
            
            # Convert ROS Image to LiveKit VideoFrame
            # Assuming BGR8 encoding
            frame_bytes = bytearray(msg.data)
            frame = rtc.VideoFrame(
                width=msg.width,
                height=msg.height,
                type=rtc.VideoBufferType.RGB24,
                data=frame_bytes
            )
            if self.video_source is not None:
                self.video_source.capture_frame(frame)
                
        except Exception as e:
            self.get_logger().error(f"Error in image callback: {e}")
    
    def _create_video_track(self):
        """Create LiveKit video track"""
        self.video_source = rtc.VideoSource(640, 480)
        video_track = rtc.LocalVideoTrack.create_video_track("wrist_camera", self.video_source)
        self._submit_to_loop(
            self.room.local_participant.publish_track(
                video_track,
                rtc.TrackPublishOptions(
                    source=rtc.TrackSource.SOURCE_CAMERA,
                    video_encoding=rtc.VideoEncoding(max_framerate=30, max_bitrate=3_000_000),
                    video_codec=rtc.VideoCodec.VP8,
                ),
            )
        )
        self.get_logger().info("Video track published")
    
    # ========== Point Cloud Streaming ==========
    
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
            self.publish_large_payload(payload, topic="pointcloud", on_done=self._on_pointcloud_send_done, message_id=msg_id)
        except Exception as e:
            # Reset flag on error if we set it
            self.get_logger().error(f"Failed to publish pointcloud data: {e}")
            return False
        return True
    
    # ========== Robot State Feedback with Change Detection ==========
    
    def odometry_callback(self, msg: Odometry):
        """
        Send odometry pose to LiveKit with:
        - Time-based throttling (max 5 Hz)
        - Change detection (skip if pose unchanged)
        """
        try:
            now = time.monotonic()
            
            # Time throttling
            if now - self._last_odometry_publish_time < self._odometry_publish_interval:
                return
            
            pose_msg = msg.pose.pose
            
            # Change detection
            if self._last_sent_pose is not None:
                if not self._pose_has_changed(pose_msg):
                    return
            
            # Update state and send
            self._last_odometry_publish_time = now
            self._last_sent_pose = pose_msg
            
            json_str = ROS2MessageFactory.message_to_json(pose_msg)
            self._submit_to_loop(self.send_feedback({
                'packetType': 'ros2_message',
                'topicName': '/pose',
                'messageType': 'geometry_msgs/msg/Pose',
                'data': json.loads(json_str)
            }))
            
        except Exception as e:
            self.get_logger().error(f"Failed to send odometry: {e}")
    
    def _pose_has_changed(self, current_pose: Pose) -> bool:
        """Check if pose has changed beyond threshold"""
        last = self._last_sent_pose
        
        # Position change
        pos_delta = (
            abs(current_pose.position.x - last.position.x) +
            abs(current_pose.position.y - last.position.y) +
            abs(current_pose.position.z - last.position.z)
        )
        
        # Orientation change
        ori_delta = (
            abs(current_pose.orientation.x - last.orientation.x) +
            abs(current_pose.orientation.y - last.orientation.y) +
            abs(current_pose.orientation.z - last.orientation.z) +
            abs(current_pose.orientation.w - last.orientation.w)
        )
        
        return (pos_delta >= self._pose_position_threshold or 
                ori_delta >= self._pose_orientation_threshold)
    
    def joint_state_callback(self, msg: JointState):
        """
        Send joint states to LiveKit with:
        - Time-based throttling (max 5 Hz)
        - Joint filtering (only joint1-6)
        - Change detection (skip if positions unchanged)
        """
        try:
            now = time.monotonic()
            
            # Time throttling
            if now - self._last_joint_state_publish_time < self._joint_state_publish_interval:
                return
            
            # Filter to joint1-6
            filtered_state = self._filter_joint_state(msg)
            if not filtered_state.name:
                return
            
            # Change detection
            if self._last_sent_joint_positions is not None:
                if not self._joint_positions_have_changed(filtered_state.position):
                    return
            
            # Update state and send
            self._last_joint_state_publish_time = now
            self._last_sent_joint_positions = list(filtered_state.position)
            
            json_str = ROS2MessageFactory.message_to_json(filtered_state)
            
            self._submit_to_loop(self.send_feedback({
                'packetType': 'ros2_message',
                'topicName': '/joint_states',
                'messageType': 'sensor_msgs/msg/JointState',
                'data': json.loads(json_str)
            }))
            
        except Exception as e:
            self.get_logger().error(f"Failed to send joint state: {e}")
    
    def _filter_joint_state(self, msg: JointState) -> JointState:
        """Filter joint state to only include joint1-6"""
        allowed_names = {'joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6'}
        
        filtered = JointState()
        filtered.header = msg.header
        
        for idx, name in enumerate(msg.name):
            if name in allowed_names and idx < len(msg.position):
                filtered.name.append(name)
                filtered.position.append(msg.position[idx])
        
        return filtered
    
    def _joint_positions_have_changed(self, current_positions: list) -> bool:
        """Check if joint positions have changed beyond threshold"""
        if len(current_positions) != len(self._last_sent_joint_positions):
            return True
        
        position_delta = sum(
            abs(curr - last)
            for curr, last in zip(current_positions, self._last_sent_joint_positions)
        )
        
        return position_delta >= self._joint_position_threshold
