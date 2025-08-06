#!/usr/bin/env python3
"""
控制流处理模块，处理VR端控制指令接收和LiveKit房间管理
"""

import json
import logging
from livekit import rtc


class ControlStreamHandler:
    def __init__(self, url, token):
        """Initialize the control stream handler with LiveKit connection details."""
        self.url = url
        self.token = token
        self.room = rtc.Room()
        self.remote_counter = {"count": 0}
        self.source = None
        self.message_manager = None
        
    async def connect(self):
        """Connect to the LiveKit room and set up event handlers."""
        await self.room.connect(self.url, self.token)
        logging.info("Connected to LiveKit room: [%s]", self.room.name)
        self._setup_event_handlers()
    
    def setup_message_handlers(self, ros_node):
        """Set up message handlers with their individual publishing strategies."""
        from .message_handlers import MessageHandlerFactory
        
        # Create manager with all handlers (each with their own timing)
        self.message_manager = MessageHandlerFactory.create_manager_with_handlers(ros_node)
        
        # Log handler configuration
        handler_info = self.message_manager.get_handler_info()
        for topic, info in handler_info.items():
            rate_info = f" at {info['rate']} Hz" if info['rate'] else " (immediate)"
            logging.info(f"Configured {topic}: {info['type']}{rate_info}")
    
    def register_handlers_from_node(self, ros_node):
        """Register all available message handlers from a ROS node."""
        self.setup_message_handlers(ros_node)
    
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
                        
            if self.message_manager:
                try:
                    data_dict = json.loads(data.data.decode('utf-8'))
                    self.message_manager.process_message(data.topic, data_dict)
                except json.JSONDecodeError:
                    logging.error("Failed to decode JSON data: %s", data.data.decode('utf-8'))
                except Exception as e:
                    logging.error(f"Error processing message on topic {data.topic}: {e}")
            else:
                logging.warning("Message manager not initialized")
    
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
        """Disconnect from the LiveKit room and stop all message handlers."""
        # Stop message handlers first
        if self.message_manager:
            self.message_manager.stop_all()
            
        # Then disconnect from room
        await self.room.disconnect()
        logging.info("Disconnected from LiveKit room and stopped all message handlers")
