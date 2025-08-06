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
from .control_stream_handler import ControlStreamHandler
from .video_stream_publisher import VideoStreamPublisher


async def main():
    # Load configuration
    pkg_share = get_package_share_directory('teleop_livekit')
    
    try:
        with open(os.path.join(pkg_share, 'config', 'livekit.yaml')) as f:
            livekit_cfg = yaml.safe_load(f)
        
        with open(os.path.join(pkg_share, 'config', 'camera.yaml')) as f:
            video_cfg = yaml.safe_load(f)
            
        # Load control stream configuration (with defaults if file doesn't exist)
        try:
            with open(os.path.join(pkg_share, 'config', 'control_stream.yaml')) as f:
                control_cfg = yaml.safe_load(f)
        except FileNotFoundError:
            # Use default configuration if file doesn't exist
            control_cfg = {
                'control_stream': {
                    'publish_rate_hz': 30.0,
                    'max_data_age_seconds': 1.0,
                    'debug_logging': False
                }
            }
            logging.warning("control_stream.yaml not found, using default settings")
            
    except FileNotFoundError:
        # if required config files are not found, raise an error
        raise FileNotFoundError("Configuration files not found. Please ensure 'livekit.yaml' and 'camera.yaml' exist in the package's config directory.")
    
    width, height, fps = video_cfg['color_width'], video_cfg['color_height'], video_cfg['color_fps']

    # Initialize and connect control stream handler
    control_handler = ControlStreamHandler(livekit_cfg['url'], livekit_cfg['token'])
    await control_handler.connect()
    source = await control_handler.publish_video_track(width, height, fps)

    # Initialize ROS node for video streaming
    rclpy.init()
    ros_node = VideoStreamPublisher(source, control_handler.remote_counter)
    
    # Register message handlers with their individual publishing strategies  
    # Each handler now manages its own timing:
    # - PoseMessageHandler: 50Hz for precise end-effector control
    # - TwistMessageHandler: 30Hz for responsive velocity control  
    # - MotionCommandHandler: Immediate execution for discrete actions
    control_handler.register_handlers_from_node(ros_node)
    
    # Log the handler configuration
    from .message_handlers import MessageHandlerFactory
    config = MessageHandlerFactory.get_handler_config()
    for topic, info in config.items():
        rate_info = f" at {info['publish_rate_hz']} Hz" if info['publish_rate_hz'] else " (immediate)"
        logging.info(f"Topic '{topic}': {info['description']}{rate_info}")
    
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
    await control_handler.disconnect()


def run():
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()