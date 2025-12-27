#!/usr/bin/env python3
"""
LiveKit RTC Implementation - Concrete implementation of RTCInterface for LiveKit
"""

from typing import Dict, Any, Optional
import asyncio

from livekit import rtc

from .rtc_interface import (
    RTCInterface,
    VideoEncodingConfig,
    VideoFrame,
    VideoBufferType,
    VideoCodec,
    DataPacket,
    Participant,
)


class LiveKitRTC(RTCInterface):
    """
    LiveKit implementation of RTCInterface.
    
    Wraps the LiveKit Python SDK to provide a unified RTC interface.
    """
    
    # Mapping from abstract types to LiveKit types
    _VIDEO_BUFFER_MAP = {
        VideoBufferType.RGB24: rtc.VideoBufferType.RGB24,
        VideoBufferType.RGBA: rtc.VideoBufferType.RGBA,
        VideoBufferType.I420: rtc.VideoBufferType.I420,
        VideoBufferType.NV12: rtc.VideoBufferType.NV12,
    }
    
    _VIDEO_CODEC_MAP = {
        VideoCodec.VP8: rtc.VideoCodec.VP8,
        VideoCodec.VP9: rtc.VideoCodec.VP9,
        VideoCodec.H264: rtc.VideoCodec.H264,
        VideoCodec.AV1: rtc.VideoCodec.AV1,
    }
    
    def __init__(self):
        """
        Initialize LiveKit RTC.
        
        Creates a new LiveKit room internally. Call connect() to establish connection.
        """
        super().__init__()
        self._room = rtc.Room()
        self._connected = False
        self._video_sources: Dict[str, rtc.VideoSource] = {}
        
        # Set up LiveKit event handlers
        self._room.on("data_received", self._handle_data_received)
        self._room.on("participant_connected", self._handle_participant_connected)
        self._room.on("participant_disconnected", self._handle_participant_disconnected)
    
    @property
    def room(self) -> rtc.Room:
        """Get the underlying LiveKit room (for advanced usage)"""
        return self._room
    
    # ========== Event Handlers ==========
    
    def _handle_data_received(self, data: rtc.DataPacket):
        """Handle LiveKit data received event"""
        if self._on_data_received:
            packet = DataPacket(
                data=data.data,
                topic=getattr(data, 'topic', None),
                participant_id=getattr(data, 'participant', {}).get('identity') if hasattr(data, 'participant') else None
            )
            self._on_data_received(packet)
    
    def _handle_participant_connected(self, participant: rtc.RemoteParticipant):
        """Handle participant connected event"""
        if self._on_participant_connected:
            p = Participant(identity=participant.identity, id=participant.sid)
            self._on_participant_connected(p)
    
    def _handle_participant_disconnected(self, participant: rtc.RemoteParticipant):
        """Handle participant disconnected event"""
        if self._on_participant_disconnected:
            p = Participant(identity=participant.identity, id=participant.sid)
            self._on_participant_disconnected(p)
    
    # ========== Connection Management ==========
    
    async def connect(self, url: str, token: str) -> bool:
        """Connect to LiveKit server"""
        try:
            await self._room.connect(url, token)
            self._connected = True
            return True
        except Exception as e:
            self._connected = False
            raise e
    
    async def disconnect(self) -> None:
        """Disconnect from LiveKit server"""
        await self._room.disconnect()
        self._connected = False
    
    @property
    def is_connected(self) -> bool:
        """Check if connected to LiveKit server"""
        return self._connected
    
    # ========== Data Channel ==========
    
    async def publish_data(self, data: bytes, topic: Optional[str] = None) -> None:
        """Publish data via LiveKit data channel"""
        if topic:
            await self._room.local_participant.publish_data(data, topic=topic)
        else:
            await self._room.local_participant.publish_data(data)
    
    # ========== Video Streaming ==========
    
    def create_video_source(self, width: int, height: int) -> rtc.VideoSource:
        """Create a LiveKit video source"""
        return rtc.VideoSource(width, height)
    
    async def publish_video_track(
        self, 
        track_name: str, 
        video_source: rtc.VideoSource,
        encoding: Optional[VideoEncodingConfig] = None
    ) -> None:
        """Publish a video track to LiveKit"""
        video_track = rtc.LocalVideoTrack.create_video_track(track_name, video_source)
        
        # Configure encoding
        if encoding:
            lk_encoding = rtc.VideoEncoding(
                max_framerate=encoding.max_framerate,
                max_bitrate=encoding.max_bitrate
            )
            lk_codec = self._VIDEO_CODEC_MAP.get(encoding.codec, rtc.VideoCodec.VP8)
            
            options = rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_CAMERA,
                video_encoding=lk_encoding,
                video_codec=lk_codec,
            )
        else:
            options = rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_CAMERA,
            )
        
        await self._room.local_participant.publish_track(video_track, options)
    
    def capture_video_frame(self, video_source: rtc.VideoSource, frame: VideoFrame) -> None:
        """Capture a video frame to LiveKit video source"""
        lk_buffer_type = self._VIDEO_BUFFER_MAP.get(
            frame.buffer_type, rtc.VideoBufferType.RGB24
        )
        
        lk_frame = rtc.VideoFrame(
            width=frame.width,
            height=frame.height,
            type=lk_buffer_type,
            data=frame.data
        )
        video_source.capture_frame(lk_frame)
    
    # ========== Participant Management ==========
    
    @property
    def remote_participants(self) -> Dict[str, Participant]:
        """Get dictionary of remote participants"""
        result = {}
        for sid, participant in self._room.remote_participants.items():
            result[sid] = Participant(
                identity=participant.identity,
                id=participant.sid
            )
        return result
    
    @property
    def local_participant_id(self) -> Optional[str]:
        """Get local participant ID"""
        if self._room.local_participant:
            return self._room.local_participant.sid
        return None
