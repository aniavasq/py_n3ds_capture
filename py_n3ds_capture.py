#!/usr/bin/env python3
"""N3DS video capture module
"""
import asyncio
import logging
import time
from array import array
from enum import Enum
from threading import Thread
from typing import Union

import tkinter as tk
import pyaudio
import usb.core
import usb.util
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

SAMPLE_SIZE_8 = 2192
AUDIO_SAMPLE_RATE = 32728 # Hz
AUDIO_CHANNELS = 2

FRAME_WIDTH = 240
FRAME_HEIGHT = 720
IMAGE_SIZE = FRAME_WIDTH * FRAME_HEIGHT * 3 # Assuming RGB24 format
FRAME_SIZE = IMAGE_SIZE + SAMPLE_SIZE_8

N3DS_DISPLAY1_WIDTH = 400
N3DS_DISPLAY2_WIDTH = 320
N3DS_DISPLAY_HEIGHT = FRAME_WIDTH
TRANSPARENT_RGBA= (0, 0, 0, 0)

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
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.audio_thread = Thread(
            target=self.loop.run_until_complete,
            args=(self.async_worker(),)
        )
        self.audio_samples_queue = asyncio.Queue()

        self.p = pyaudio.PyAudio()
        self.channels = min(
            AUDIO_CHANNELS,
            self.p.get_default_output_device_info()['maxOutputChannels'])
        self.sample_rate = AUDIO_SAMPLE_RATE
        if self.channels < AUDIO_CHANNELS:
            self.sample_rate = AUDIO_SAMPLE_RATE * 2
        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.sample_rate,
            output=True,
        )


    def _process_audio(self, audio_sample: array) -> None:
        """Extract audio data and process as needed
        """
        self.stream.write(audio_sample.tobytes())


    async def async_worker(self) -> None:
        """Async audio worker
        """
        while True:
            try:
                sample = await self.audio_samples_queue.get()
                if sample is None:
                    break
                self._process_audio(sample)
            except asyncio.QueueEmpty:
                pass
            except N3DSCaptureException as e:
                logging.error(e)


    def push_sample(self, audio_sample: array) -> None:
        """Push sample
        """
        asyncio.run_coroutine_threadsafe(
            self.audio_samples_queue.put(audio_sample),
            self.loop
        )


    def start(self) -> None:
        """Start thread
        """
        self.audio_thread.start()


    def join(self) -> None:
        """Join thread
        """
        self.audio_thread.join()


    def close(self) -> None:
        """Close PyAudio stream
        """
        self.stream.stop_stream()
        self.stream.close()
        self.p.terminate()



class N3DSCaptureCard:
    """N3DS video capture class
    """

    def __init__(self) -> None:
        self.device: usb.core.Device
        self.interface: usb.core.Interface

        self.root = tk.Tk()
        self.root.configure(background='black')

        self.upper_display = tk.Canvas(
            self.root,
            width=N3DS_DISPLAY1_WIDTH,
            height=N3DS_DISPLAY_HEIGHT,
            bd=0,
            highlightthickness=0
        )
        self.upper_display.pack(side=tk.TOP)
        transparent_image = ImageTk.PhotoImage(
            Image.new(
                mode='RGBA',
                size=(N3DS_DISPLAY1_WIDTH, N3DS_DISPLAY_HEIGHT),
                color=TRANSPARENT_RGBA)
        )
        self.upper_display.create_image(0, 0, anchor='nw', image=transparent_image)

        self.lower_display = tk.Canvas(
            self.root,
            width=N3DS_DISPLAY2_WIDTH,
            height=N3DS_DISPLAY_HEIGHT,
            bd=0,
            highlightthickness=0
        )
        self.lower_display.pack(side=tk.BOTTOM)
        transparent_image = ImageTk.PhotoImage(
            Image.new(
                mode='RGBA',
                size=(N3DS_DISPLAY2_WIDTH, N3DS_DISPLAY_HEIGHT),
                color=TRANSPARENT_RGBA)
        )
        self.lower_display.create_image(0, 0, anchor='nw', image=transparent_image)

        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.frames_per_second = tk.StringVar()
        self.root.title(TITLE.format(fps=0.0))
        self.last_fps_update_time = time.time()
        self.start_time = 0
        self.frame_count = 0

        self.n3ds_audio = N3DSCaptureAudio()

        transfer_size = (FRAME_SIZE + 0x1ff) & ~0x1ff
        self.transferred = array('B', '\x00'.encode('utf-8') * transfer_size)
        self.seed = array('B')


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


    def _on_close(self) -> None:
        """Dispose resources and destroy window
        """
        self.dispose_resources()
        self.root.destroy()


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
        try:
            frame_image = ImageTk.PhotoImage(Image.frombuffer(
                'RGB',
                (FRAME_WIDTH, FRAME_HEIGHT),
                rgb_array,
                'raw', 'RGB', 0, 1 # decoder params
            ).rotate(90, expand=True))

            # Update cavas with the new images
            self.upper_display.create_image(0, 0, anchor='nw', image=frame_image)
            self.lower_display.create_image(N3DS_DISPLAY2_WIDTH, 0, anchor='ne', image=frame_image)

            # Update the Tkinter
            self.upper_display.update()
        except tk.TclError:
            pass


    def _calculate_fps(self) -> None:
        """Calculate Frames per Second
        """
        try:
            # Update frames per second in the window title
            current_time = time.time()

            if current_time - self.last_fps_update_time >= 2.0:
                elapsed_time = current_time - self.start_time
                fps = self.frame_count / elapsed_time if elapsed_time > 0 else 0
                title = TITLE.format(fps=fps)
                self.frames_per_second.set(title)
                logging.debug(title)
                self.root.title(self.frames_per_second.get())
                self.last_fps_update_time = current_time
        except tk.TclError:
            pass


    def _capture_and_show_frames(self) -> None:
        """Capture and show the frames using root.after
        """
        frame_result = self._grab_frame()

        if frame_result == CaptureResult.OK:
            frame_buf = self.transferred
            self.n3ds_audio.push_sample(frame_buf[IMAGE_SIZE:])
            self._show_frame(frame_buf[:IMAGE_SIZE])
            self._calculate_fps()
            self.frame_count += 1
        elif frame_result == CaptureResult.ERROR:
            self.dispose_resources()
        elif frame_result == CaptureResult.SKIP:
            pass

        # Schedule the function to run again inmediatly
        self.root.after('idle', self._capture_and_show_frames)


    def process_frames(self) -> None:
        """Capture and show the frames using root.after
        """
        self.start_time = time.time()

        self.n3ds_audio.start()

        try:
            # Start the first call of capture and show
            self._capture_and_show_frames()
            # Start the Tkinter main loop
            self.root.mainloop()
        except N3DSCaptureException as e:
            logging.exception(e)
            self.dispose_resources()
        finally:
            self.n3ds_audio.push_sample(None)
            self.n3ds_audio.join()


if __name__ == '__main__':
    capture_card = N3DSCaptureCard()
    if capture_card.device_init():
        try:
            capture_card.process_frames()
        finally:
            capture_card.dispose_resources()
