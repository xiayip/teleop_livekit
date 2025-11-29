#!/usr/bin/env python3
"""
Teleop Manager - Manages the lifecycle of the teleoperation bridge
"""

import asyncio
import threading
from typing import Optional, Dict, Any

from livekit import rtc
import rclpy

from .teleop_bridge import TeleopBridge


class TeleopManager:
    """
    Manages the teleoperation bridge lifecycle:
    - LiveKit room connection
    - ROS2 node initialization
    - Threading for ROS2 spinning
    """
    
    def __init__(self, livekit_url: str, livekit_token: str):
        self.livekit_url = livekit_url
        self.livekit_token = livekit_token
        self.room: Optional[rtc.Room] = None
        self.bridge: Optional[TeleopBridge] = None
        self.ros_thread: Optional[threading.Thread] = None
        self.running = False
    
    async def start(self):
        """Start the teleoperation bridge"""
        try:
            # Initialize ROS2
            rclpy.init()
            
            # Connect to LiveKit room
            self.room = rtc.Room()
            await self.room.connect(self.livekit_url, self.livekit_token)
            
            # Create teleop bridge node
            self.bridge = TeleopBridge(self.room)
            
            # Start ROS2 spinning thread
            self.running = True
            self.ros_thread = threading.Thread(target=self._ros_spin_thread, daemon=True)
            self.ros_thread.start()
            
            print("Teleoperation bridge started successfully")
            
        except Exception as e:
            print(f"Failed to start teleop bridge: {e}")
            raise
    
    def _ros_spin_thread(self):
        """ROS2 node spinning thread"""
        try:
            while self.running and rclpy.ok():
                rclpy.spin_once(self.bridge, timeout_sec=0.1)
        except Exception as e:
            print(f"ROS2 spin thread error: {e}")
    
    async def stop(self):
        """Stop the teleoperation bridge"""
        print("Stopping teleoperation bridge...")
        
        self.running = False
        
        # Stop ROS thread
        if self.ros_thread and self.ros_thread.is_alive():
            self.ros_thread.join(timeout=1.0)
        
        # Destroy ROS node
        if self.bridge:
            self.bridge.destroy_node()
        
        # Disconnect from LiveKit
        if self.room:
            await self.room.disconnect()
        
        # Shutdown ROS
        if rclpy.ok():
            rclpy.shutdown()
        
        print("Teleoperation bridge stopped")
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get bridge statistics"""
        if self.bridge:
            return {
                'error_count': self.bridge.error_count,
                'publishers': len(self.bridge._topic_publishers),
                'service_clients': len(self.bridge.service_clients),
                'action_clients': len(self.bridge.action_clients),
            }
        return {}


async def main_async(livekit_url: str, livekit_token: str):
    """Async main function"""
    manager = TeleopManager(livekit_url, livekit_token)
    
    try:
        await manager.start()
        
        # Keep running until interrupted
        while True:
            await asyncio.sleep(1.0)
            
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        await manager.stop()


def main():
    """Main entry point"""
    import sys

    LIVEKIT_URL = "wss://livekit.zwind-robot.com"
    LIVEKIT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjIxMjA2MzA5MzUsImlkZW50aXR5IjoiejEiLCJpc3MiOiJBUElIdEdUYVV2YzlGNHkiLCJuYW1lIjoiejEiLCJuYmYiOjE3NjA2MzQ1MzUsInN1YiI6InoxIiwidmlkZW8iOnsicm9vbSI6InRlc3Rfcm9vbSIsInJvb21Kb2luIjp0cnVlfX0.ewJqQezIdniNe3HB1pHyAClA6MrNjxVyCdcCjV97DSw"
    
    asyncio.run(main_async(LIVEKIT_URL, LIVEKIT_TOKEN))

if __name__ == '__main__':
    main()
