#!/usr/bin/env python3
"""N3DS video capture module
"""
import logging
import time
from array import array
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Tuple, Union

import tkinter as tk
import usb.core
import usb.util
import numpy as np
import pygame
from PIL import Image, ImageTk


VID_3DS = 0x16D0
PID_3DS = 0x06A3
DEFAULT_CONFIGURATION = 1
CAPTURE_INTERFACE = 0
CONTROL_TIMEOUT = 30
EP2_TIMEOUT = 50
EP2_IN = 2 | usb.util.ENDPOINT_IN

CMDIN_I2C_READ = 0x21
CMDOUT_I2C_WRITE = 0x21
CMDOUT_CAPTURE_START = 0x40

I2CADDR_3DSCONFIG = 0x14
N3DSCFG_BITSTREAM_VER = 1

EXTRA_DATA_SIZE = 0xB02
AUDIO_SAMPLE_RATE = 0x7FD8 # Hz
AUDIO_CHANNELS = 2

FRAME_WIDTH = 240
FRAME_HEIGHT = 720
IMAGE_SIZE = FRAME_WIDTH * FRAME_HEIGHT * 3 # Assuming RGB24 format
FRAME_SIZE = IMAGE_SIZE + EXTRA_DATA_SIZE

N3DS_DISPLAY1_WIDTH = 400
N3DS_DISPLAY2_WIDTH = 320
N3DS_DISPLAY_HEIGHT = FRAME_WIDTH

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


class N3DSCaptureCard:
    """N3DS video capture class
    """

    def __init__(self) -> None:
        self.device: usb.core.Device
        self.interface: usb.core.Interface

        self.root = tk.Tk()
        self.root.configure(background='black')
        self.upper_display = tk.Label(self.root, bd=0, highlightthickness=0)
        self.upper_display.pack(side=tk.TOP)

        self.lower_display = tk.Label(self.root, bd=0, highlightthickness=0)
        self.lower_display.pack(side=tk.BOTTOM)

        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.frames_per_second = tk.StringVar()
        self.root.title(TITLE.format(fps=0.0))
        self.last_fps_update_time = time.time()
        self.start_time = 0
        self.frame_count = 0

        pygame.mixer.init(frequency=AUDIO_SAMPLE_RATE, size=-16, channels=AUDIO_CHANNELS)
        self.executor = ThreadPoolExecutor(max_workers=1)


    def vend_in(self, b_request: int, w_value: int, w_length: int) -> Union[array, int]:
        """Read vendor request from control endpoint.  Returns bytes transferred (<0 = libusb error)
        """
        return self.device.ctrl_transfer(
            bmRequestType=usb.util.CTRL_TYPE_VENDOR | usb.util.ENDPOINT_IN,
            bRequest=b_request,
            wValue=w_value,
            wIndex=0,
            data_or_wLength=w_length,
            timeout=CONTROL_TIMEOUT
        )


    def vend_out(self, b_request: int, w_value: int, w_length: int) -> Union[array, int]:
        """Write vendor request to control endpoint.  Returns bytes transferred (<0 = libusb error)
        """
        return self.device.ctrl_transfer(
            bmRequestType=usb.util.CTRL_TYPE_VENDOR | usb.util.ENDPOINT_OUT,
            bRequest=b_request,
            wValue=w_value,
            wIndex=0,
            data_or_wLength=w_length,
            timeout=CONTROL_TIMEOUT
        )


    def bulk_in(self, length: int) -> array:
        """Read from bulk endpoint. Returns libusb error code
        """
        return self.device.read(
            endpoint=EP2_IN,
            size_or_buffer=length,
            timeout=EP2_TIMEOUT
        )


    def read_config(self, cfg_addr, buf, count) -> bool:
        """Read FPGA configuration regs
        """
        return 0 < count <= 256 and \
            self.vend_out(CMDOUT_I2C_WRITE, I2CADDR_3DSCONFIG, cfg_addr) and \
            self.vend_in(CMDIN_I2C_READ, I2CADDR_3DSCONFIG, buf) == count


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

        self.vend_out(CMDOUT_CAPTURE_START, 0, 0)
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

        if self.executor:
            self.executor.shutdown()


    def grab_frame(self) -> Tuple[CaptureResult, np.ndarray[np.uint8]]:
        """Gets 240x720 RGB24 (rotated) frame.
        """
        try:
            result = self.vend_out(CMDOUT_CAPTURE_START, 0, 0)
        except usb.core.USBTimeoutError as usb_err:
            logging.exception(usb_err)
            return (CaptureResult.SKIP, [])
        except AttributeError:
            return (CaptureResult.ERROR, [])

        if result < 0:
            return (CaptureResult.ERROR, [])

        transfer_size = (FRAME_SIZE + 0x1ff) & ~0x1ff  # multiple of maxPacketSize
        try:
            transferred = np.array(self.bulk_in(transfer_size), dtype=np.uint8)
        except usb.core.USBTimeoutError as usb_err:
            logging.exception(usb_err)
            return (CaptureResult.SKIP, [])

        if len(transferred) < FRAME_SIZE:
            return (CaptureResult.SKIP, [])

        return (CaptureResult.OK, transferred)


    def get_version(self) -> int:
        """5 = first public release. 6 = 3D capture support (beta)
        """
        version = array('i', [0])
        self.read_config(N3DSCFG_BITSTREAM_VER, version, 1)

        return version[0]


    def on_close(self) -> None:
        """Dispose resources and destroy window
        """
        self.dispose_resources()
        self.root.destroy()


    def process_audio(self, frame_data: np.ndarray[np.uint8]) -> None:
        """Extract audio data and process as needed
        """
        audio_sample = np.trim_zeros(frame_data[IMAGE_SIZE:].view(np.uint16))
        if len(audio_sample) > 0:
            sound = pygame.mixer.Sound(buffer=audio_sample.tobytes())
            sound.play()


    def rotate_and_crop_frame(
        self, frame_array: np.ndarray[np.uint8]
    ) -> Tuple[ImageTk.PhotoImage, ImageTk.PhotoImage]:
        """Rotate the image 90 degrees to the left and
        split the image into upper and lower display
        """
        frame_image = Image.fromarray(frame_array, 'RGB').rotate(90, expand=True)

        # Convert PIL Images to PhotoImages
        upper_image = ImageTk.PhotoImage(
            frame_image.crop((0, 0, N3DS_DISPLAY1_WIDTH, FRAME_WIDTH)))
        lower_image = ImageTk.PhotoImage(
            frame_image.crop((N3DS_DISPLAY1_WIDTH, 0, FRAME_HEIGHT, FRAME_WIDTH)))

        return upper_image, lower_image


    def show_frame(self, frame_data: np.ndarray[np.uint8]) -> None:
        """Show the image from the frame buffer
        """
        # Extract RGB image data
        frame_array = frame_data[:IMAGE_SIZE].reshape((FRAME_HEIGHT, FRAME_WIDTH, 3))

        try:
            # Check if the Tkinter window still exists
            if self.root.winfo_exists():
                upper_image, lower_image = self.rotate_and_crop_frame(frame_array)

                # Update labels with the new images
                self.upper_display.configure(image=upper_image)
                self.lower_display.configure(image=lower_image)

                # Update the Tkinter window
                self.root.update()

                # Update frames per second in the window title
                current_time = time.time()

                if current_time - self.last_fps_update_time >= 1.0:
                    elapsed_time = current_time - self.start_time
                    fps = self.frame_count / elapsed_time if elapsed_time > 0 else 0
                    self.frames_per_second.set(TITLE.format(fps=fps))
                    self.root.title(self.frames_per_second.get())
                    self.last_fps_update_time = current_time
        except tk.TclError:
            # Handle the TclError when the window has been destroyed
            pass


    def capture_and_show_frames(self) -> None:
        """Capture and show the frames in a loop
        """
        self.start_time = time.time()

        if self.device is None:
            time.sleep(0.02)
            self.device_init()

        try:
            while True:
                frame_result, frame_data = self.grab_frame()

                if frame_result == CaptureResult.OK:
                    future_audio = self.executor.submit(self.process_audio, frame_data)
                    self.show_frame(frame_data)
                    future_audio.result()  # Wait for the audio processing to finish
                    self.frame_count += 1
                elif frame_result == CaptureResult.ERROR:
                    self.dispose_resources()
                    break
                elif frame_result == CaptureResult.SKIP:
                    self.show_frame(np.zeros(FRAME_SIZE, dtype=np.uint8))
        except N3DSCaptureException as e:
            logging.exception(e)
            self.dispose_resources()
        finally:
            self.root.mainloop()  # Start the Tkinter main loop


if __name__ == '__main__':
    capture_card = N3DSCaptureCard()
    if capture_card.device_init():
        try:
            capture_card.capture_and_show_frames()
        finally:
            capture_card.dispose_resources()
