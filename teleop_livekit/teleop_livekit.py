#!/usr/bin/env python3
import asyncio
import logging
import os
import threading
import yaml
from signal import SIGINT, SIGTERM
import rclpy
from ament_index_python.packages import get_package_share_directory

# 导入本地模块
from .livekit_publisher import LiveKitPublisher
from .livekit_client import LiveKitClient


async def main():
    # Load configuration
    pkg_share = get_package_share_directory('teleop_livekit')
    
    try:
        with open(os.path.join(pkg_share, 'config', 'livekit.yaml')) as f:
            livekit_cfg = yaml.safe_load(f)
        
        with open(os.path.join(pkg_share, 'config', 'camera.yaml')) as f:
            video_cfg = yaml.safe_load(f)
    except FileNotFoundError:
        # if config files are not found, raise an error
        raise FileNotFoundError("Configuration files not found. Please ensure 'livekit.yaml' and 'camera.yaml' exist in the package's config directory.")
    
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