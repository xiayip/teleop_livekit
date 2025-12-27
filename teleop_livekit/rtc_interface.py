#!/usr/bin/env python3
"""
RTC Interface - Abstract base class for RTC communication
Defines the interface for different RTC implementations (LiveKit, Volcengine, etc.)
"""

import abc
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass
from enum import Enum


class VideoCodec(Enum):
    """Supported video codecs"""
    VP8 = "vp8"
    VP9 = "vp9"
    H264 = "h264"
    AV1 = "av1"


class VideoBufferType(Enum):
    """Video buffer types"""
    RGB24 = "rgb24"
    RGBA = "rgba"
    I420 = "i420"
    NV12 = "nv12"


@dataclass
class VideoEncodingConfig:
    """Video encoding configuration"""
    max_framerate: int = 30
    max_bitrate: int = 3_000_000
    codec: VideoCodec = VideoCodec.VP8


@dataclass
class VideoFrame:
    """Video frame data"""
    width: int
    height: int
    buffer_type: VideoBufferType
    data: bytes


@dataclass
class DataPacket:
    """Data packet received from remote"""
    data: bytes
    topic: Optional[str] = None
    participant_id: Optional[str] = None


@dataclass
class Participant:
    """Remote participant info"""
    identity: str
    id: str


class RTCInterface(abc.ABC):
    """
    Abstract base class for RTC communication.
    
    Implementations should handle:
    - Connection management
    - Data channel communication
    - Video/audio streaming
    - Participant management
    """
    
    def __init__(self):
        # Event callbacks
        self._on_data_received: Optional[Callable[[DataPacket], None]] = None
        self._on_participant_connected: Optional[Callable[[Participant], None]] = None
        self._on_participant_disconnected: Optional[Callable[[Participant], None]] = None
    
    # ========== Event Handler Registration ==========
    
    def on_data_received(self, callback: Callable[[DataPacket], None]):
        """Register callback for data received events"""
        self._on_data_received = callback
    
    def on_participant_connected(self, callback: Callable[[Participant], None]):
        """Register callback for participant connected events"""
        self._on_participant_connected = callback
    
    def on_participant_disconnected(self, callback: Callable[[Participant], None]):
        """Register callback for participant disconnected events"""
        self._on_participant_disconnected = callback
    
    # ========== Connection Management ==========
    
    @abc.abstractmethod
    async def connect(self, url: str, token: str) -> bool:
        """
        Connect to RTC server.
        
        Args:
            url: Server URL
            token: Authentication token
            
        Returns:
            True if connection successful
        """
        pass
    
    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from RTC server"""
        pass
    
    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        """Check if connected to RTC server"""
        pass
    
    # ========== Data Channel ==========
    
    @abc.abstractmethod
    async def publish_data(self, data: bytes, topic: Optional[str] = None) -> None:
        """
        Publish data to remote participants.
        
        Args:
            data: Raw bytes to send
            topic: Optional topic/channel name
        """
        pass
    
    # ========== Video Streaming ==========
    
    @abc.abstractmethod
    def create_video_source(self, width: int, height: int) -> Any:
        """
        Create a video source for publishing.
        
        Args:
            width: Video width
            height: Video height
            
        Returns:
            Video source handle (implementation-specific)
        """
        pass
    
    @abc.abstractmethod
    async def publish_video_track(
        self, 
        track_name: str, 
        video_source: Any,
        encoding: Optional[VideoEncodingConfig] = None
    ) -> None:
        """
        Publish a video track.
        
        Args:
            track_name: Name of the track
            video_source: Video source created by create_video_source
            encoding: Optional encoding configuration
        """
        pass
    
    @abc.abstractmethod
    def capture_video_frame(self, video_source: Any, frame: VideoFrame) -> None:
        """
        Capture a video frame to the source.
        
        Args:
            video_source: Video source handle
            frame: Video frame data
        """
        pass
    
    # ========== Participant Management ==========
    
    @property
    @abc.abstractmethod
    def remote_participants(self) -> Dict[str, Participant]:
        """Get dictionary of remote participants"""
        pass
    
    @property
    @abc.abstractmethod
    def local_participant_id(self) -> Optional[str]:
        """Get local participant ID"""
        pass
