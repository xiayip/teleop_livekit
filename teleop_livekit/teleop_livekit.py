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
    # LIVEKIT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3NTk5MzYyODYsImlzcyI6IkFQSXduZkVoTmNUY2ZzQSIsIm5iZiI6MTc1MDkzNjI4Niwic3ViIjoiejEiLCJ2aWRlbyI6eyJjYW5QdWJsaXNoIjp0cnVlLCJjYW5QdWJsaXNoRGF0YSI6dHJ1ZSwiY2FuU3Vic2NyaWJlIjp0cnVlLCJyb29tIjoibXktcm9vbSIsInJvb21Kb2luIjp0cnVlfX0.4NZQOGMPEM6f6-pl4MriDYdmufK-LctP1mesMTQpPNo"
    
    LIVEKIT_URL = "ws://47.111.148.68:7880"
    LIVEKIT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE4MjY5NjU3MjUsImlzcyI6InVuaXgiLCJuYW1lIjoiY2FyIiwibmJmIjoxNzU0OTY1NzI1LCJzdWIiOiJjYXIiLCJ2aWRlbyI6eyJyb29tIjoiZGluZ2RhbmciLCJyb29tSm9pbiI6dHJ1ZX19.jrTpNOI-LMIosludj_Pf7MaYmZfJkTYBR0orzH1gkYA"

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