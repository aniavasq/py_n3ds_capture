#!/usr/bin/env python3
"""N3DS video capture module
"""
import logging
import time
from array import array
from enum import Enum
from typing import Union

import pygame
import usb.core
import usb.util


VID_3DS = 0x16D0
PID_3DS = 0x06A3
DEFAULT_CONFIGURATION = 1
CAPTURE_INTERFACE = 0
CONTROL_TIMEOUT = 30
EP2_TIMEOUT = 50
EP2_IN = 2 | usb.util.ENDPOINT_IN

CMDOUT_CAPTURE_START = 0x40

AUDIO_SAMPLE_SIZE = 2188 # bytes
AUDIO_SAMPLE_RATE = 32728 # Hz
AUDIO_CHANNELS = 2

FRAME_WIDTH = 240
FRAME_HEIGHT = 720
IMAGE_SIZE = FRAME_WIDTH * FRAME_HEIGHT * 3 # Assuming RGB24 format
FRAME_SIZE = IMAGE_SIZE + AUDIO_SAMPLE_SIZE

N3DS_DISPLAY1_WIDTH = 400
N3DS_DISPLAY2_WIDTH = 320
N3DS_DISPLAY_HEIGHT = FRAME_WIDTH
DISPLAY2_X = (N3DS_DISPLAY1_WIDTH - N3DS_DISPLAY2_WIDTH) // 2

TITLE = 'py N3DS Capture ({fps:.2f} FPS)'

logging.basicConfig(
    level=logging.DEBUG,
    format=(
        '{"time":"%(asctime)s",'
        '"level":"%(levelname)s",'
        '"message": "%(message)s"}'
    )
)


class CaptureResult(Enum):
    """Capture Result Enum
    """
    SKIP = 1
    OK = 0
    ERROR = -1


class N3DSCaptureException(Exception):
    """N3DS capture card exception
    """


class N3DSCaptureAudio:
    """N3DS Capture Card Audio
    """

    def __init__(self) -> None:
        pygame.mixer.init(
            frequency=AUDIO_SAMPLE_RATE,
            channels=AUDIO_CHANNELS,
            buffer=256
        )
        self.channel = pygame.mixer.Channel(0)
        self.channel.set_volume(0.5)


    def push_sample(self, audio_sample: array) -> None:
        """Push sample
        """
        self.channel.queue(
            pygame.mixer.Sound(buffer=audio_sample)
        )


    def close(self) -> None:
        """Close PyAudio stream
        """
        self.channel.stop()



class N3DSCaptureCard:
    """N3DS video capture class
    """

    def __init__(self) -> None:
        self.device: usb.core.Device
        self.interface: usb.core.Interface

        transfer_size = (FRAME_SIZE + 0x1ff) & ~0x1ff
        self.transferred = array('B', '\x00'.encode('utf-8') * transfer_size)
        self.seed = array('B')

        self.clock = pygame.time.Clock()
        self.last_fps_update_time = time.time()
        self.start_time = 0
        self.frame_count = 0

        self.n3ds_audio = N3DSCaptureAudio()

        pygame.init()
        self.display = pygame.display.set_mode(
            (N3DS_DISPLAY1_WIDTH, N3DS_DISPLAY_HEIGHT * 2))
        title = TITLE.format(fps=0.0)
        pygame.display.set_caption(title)


    def _vend_out(
        self, b_request: int, w_value: int, data_or_w_length: Union[int, array]
    ) -> Union[array, int]:
        """Write vendor request to control endpoint.
        Returns bytes transferred (<0 = libusb error)
        """
        return self.device.ctrl_transfer(
            bmRequestType=usb.util.CTRL_TYPE_VENDOR | usb.util.ENDPOINT_OUT,
            bRequest=b_request,
            wValue=w_value,
            wIndex=0,
            data_or_wLength=data_or_w_length,
            timeout=CONTROL_TIMEOUT
        )


    def _bulk_in(self, size_or_buffer: Union[int, array]) -> array:
        """Read from bulk endpoint. Returns libusb error code
        """
        return self.device.read(
            endpoint=EP2_IN,
            size_or_buffer=size_or_buffer,
            timeout=EP2_TIMEOUT
        )


    def device_init(self) -> bool:
        """Open capture device (only call once)
        """
        self.device = usb.core.find(idVendor=VID_3DS, idProduct=PID_3DS)

        if self.device is None:
            return False

        self.device.set_configuration(DEFAULT_CONFIGURATION)

        for cfg in self.device:
            for iface in cfg:
                if iface and iface.bInterfaceNumber == CAPTURE_INTERFACE:
                    self.interface = iface
                if self.interface:
                    break

        if self.interface is None:
            return False

        usb.util.claim_interface(self.device, self.interface)

        self._vend_out(CMDOUT_CAPTURE_START, 0, 0)
        time.sleep(0.5)

        return True


    def dispose_resources(self) -> None:
        """Close capture device
        """
        if self.device:
            if self.interface:
                usb.util.release_interface(self.device, self.interface)
                self.interface = None
            usb.util.dispose_resources(self.device)
            self.device = None

        self.n3ds_audio.close()


    def _grab_frame(self) -> CaptureResult:
        """Gets 240x720 RGB24 (rotated) frame.
        """
        try:
            result = self._vend_out(CMDOUT_CAPTURE_START, 0, self.seed)
        except usb.core.USBTimeoutError as usb_err:
            logging.exception(usb_err)
            return CaptureResult.SKIP
        except AttributeError:
            return CaptureResult.ERROR

        if result < 0:
            return CaptureResult.ERROR

        try:
            self._bulk_in(self.transferred)
        except usb.core.USBTimeoutError as usb_err:
            logging.exception(usb_err)
            return CaptureResult.SKIP

        return CaptureResult.OK


    def _show_frame(self, rgb_array: array) -> None:
        """Show the image from the frame buffer
        """
        frame_surface = pygame.image.frombuffer(rgb_array, (FRAME_WIDTH, FRAME_HEIGHT), 'RGB')
        frame_surface = pygame.transform.rotate(frame_surface, 90)
        self.display.blit(frame_surface, (0, 0))
        display2_area = (N3DS_DISPLAY1_WIDTH, 0, N3DS_DISPLAY2_WIDTH, N3DS_DISPLAY_HEIGHT)
        self.display.blit(frame_surface, (DISPLAY2_X, N3DS_DISPLAY_HEIGHT), display2_area)
        pygame.display.flip()


    def _calculate_fps(self) -> None:
        """Calculate Frames per Second and Update the window title
        """
        current_time = time.time()

        if current_time - self.last_fps_update_time >= 2.0:
            elapsed_time = current_time - self.start_time
            fps = self.frame_count / elapsed_time if elapsed_time > 0 else 0
            title = TITLE.format(fps=fps)
            logging.debug(title)
            pygame.display.set_caption(title)
            self.last_fps_update_time = current_time


    def _capture_and_show_frames(self) -> None:
        """Capture and show the frames using root.after
        """
        frame_result = self._grab_frame()

        if frame_result == CaptureResult.OK:
            frame_buf = self.transferred
            self.n3ds_audio.push_sample(frame_buf[IMAGE_SIZE:FRAME_SIZE])
            self._show_frame(frame_buf[:IMAGE_SIZE])
            self._calculate_fps()
            self.frame_count += 1
        elif frame_result == CaptureResult.ERROR:
            self.dispose_resources()
        elif frame_result == CaptureResult.SKIP:
            pass


    def process_frames(self) -> None:
        """Capture and show the frames using root.after
        """
        self.start_time = time.time()

        running = True
        try:
            while running:
                self._capture_and_show_frames()
                self.clock.tick(60)
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        self.dispose_resources()
                        running = False
        except N3DSCaptureException as e:
            logging.error(e)
            self.dispose_resources()


if __name__ == '__main__':
    capture_card = N3DSCaptureCard()
    if capture_card.device_init():
        try:
            capture_card.process_frames()
        except KeyboardInterrupt:
            pass
        finally:
            capture_card.dispose_resources()
