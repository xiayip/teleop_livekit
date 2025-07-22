#!/usr/bin/env python3
"""
LiveKit发布者模块，处理与LiveKit服务的连接和数据发送/接收
"""

import json
import logging
from livekit import rtc


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
        from .message_handlers import MessageHandlerFactory
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
