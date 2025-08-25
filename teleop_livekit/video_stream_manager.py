#!/usr/bin/env python3
"""
VideoStreamManager: Handles LiveKit video track creation and frame capture.
Keeps the video-related logic out of the main bridge node.
"""

import asyncio
from typing import Optional
from livekit import rtc
from sensor_msgs.msg import Image


class VideoStreamManager:
    def __init__(self, room: rtc.Room, logger):
        self.room = room
        self.logger = logger
        self.source: Optional[rtc.VideoSource] = None
        self._track_created = False
        self.supported_encodings = {'rgb8'}
        self.default_fps = 30
        self.max_bitrate = 3_000_000
        self.codec = rtc.VideoCodec.AV1

    def process_image(self, msg: Image) -> bool:
        """Process a ROS Image message: ensure track then capture frame.
        Returns True if a frame was captured, False otherwise.
        """
        # Skip if no viewers
        if len(self.room.remote_participants) == 0:
            self.logger.debug("No participants connected, skipping frame upload")
            return False

        # Ensure track exists (create asynchronously on first frame)
        if not self._track_created:
            asyncio.create_task(self._ensure_track(msg.width, msg.height, self.default_fps))
            # Skip this frame; start sending on next callbacks
            return False

        # Validate encoding
        if msg.encoding not in self.supported_encodings:
            self.logger.warn(f"Unsupported encoding {msg.encoding}, skipping frame")
            return False

        # Capture frame
        try:
            frame_bytes = bytearray(msg.data)
            frame = rtc.VideoFrame(
                width=msg.width,
                height=msg.height,
                type=rtc.VideoBufferType.RGB24,
                data=frame_bytes,
            )
            if self.source is not None:
                self.source.capture_frame(frame)
                return True
            return False
        except Exception as e:
            self.logger.error(f"Error capturing frame: {e}")
            return False

    async def _ensure_track(self, width: int, height: int, fps: int):
        if self._track_created:
            return
        await self._create_video_track(width, height, fps)
        self._track_created = True

    async def _create_video_track(self, width: int, height: int, fps: int):
        """Create and publish LiveKit video track."""
        self.source = rtc.VideoSource(width, height)
        track = rtc.LocalVideoTrack.create_video_track("wrist_camera", self.source)
        await self.room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_CAMERA,
                video_encoding=rtc.VideoEncoding(max_framerate=fps, max_bitrate=self.max_bitrate),
                video_codec=self.codec,
            ),
        )
        self.logger.info("Published LiveKit track (will only send frames when someone is watching)")
