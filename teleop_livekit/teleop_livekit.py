#!/usr/bin/env python3
import asyncio
import logging
from signal import SIGINT, SIGTERM
from ament_index_python.packages import get_package_share_directory

# Import local modules
from .livekit_ros2_bridge import LiveKitROS2BridgeManager

# Main program
async def main():
    """Main function"""
    import signal
    
    # LiveKit configuration
    # LIVEKIT_URL = "wss://zwindz1-lam2j1uj.livekit.cloud"
    # LIVEKIT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3NjkzMjQ5NDIsImlkZW50aXR5Ijoicm9ib3QiLCJpc3MiOiJBUEl3bmZFaE5jVGNmc0EiLCJuYmYiOjE3NTkzMjQ5NDMsInN1YiI6InJvYm90IiwidmlkZW8iOnsiY2FuUHVibGlzaCI6dHJ1ZSwiY2FuUHVibGlzaERhdGEiOnRydWUsImNhblN1YnNjcmliZSI6dHJ1ZSwicm9vbSI6InRlc3Rfcm9vbSIsInJvb21Kb2luIjp0cnVlfX0.f5ziBbFHnajK_kY0HVpAxuxXqPKOotsaAJNEPzRhQfs"

    LIVEKIT_URL = "wss://livekit.zwind-robot.com"
    LIVEKIT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjIxMjA2MzA5MzUsImlkZW50aXR5IjoiejEiLCJpc3MiOiJBUElIdEdUYVV2YzlGNHkiLCJuYW1lIjoiejEiLCJuYmYiOjE3NjA2MzQ1MzUsInN1YiI6InoxIiwidmlkZW8iOnsicm9vbSI6InRlc3Rfcm9vbSIsInJvb21Kb2luIjp0cnVlfX0.ewJqQezIdniNe3HB1pHyAClA6MrNjxVyCdcCjV97DSw"

    # Create bridge manager
    manager = LiveKitROS2BridgeManager(LIVEKIT_URL, LIVEKIT_TOKEN)
    
    # Set up signal handling
    def signal_handler(sig, frame):
        print("\nReceived stop signal...")
        asyncio.create_task(manager.stop())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Start service
        await manager.start()
        
        # Keep running
        while manager.running:
            await asyncio.sleep(1.0)
            
            # Periodically output statistics
            # stats = manager.get_statistics()
            # print(f"Statistics: {stats}")

    except KeyboardInterrupt:
        print("\nProgram interrupted by user")
    except Exception as e:
        print(f"Program error: {e}")
    finally:
        await manager.stop()

def run():
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()