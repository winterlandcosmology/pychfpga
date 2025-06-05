
# PyPi packages
import time

# Local packages
from .display import Display

# register definitions
SET_CONTRAST = 0x81
SET_ENTIRE_ON = 0xA4
SET_NORM_INV = 0xA6
SET_DISP = 0xAE
SET_MEM_ADDR = 0x20
SET_COL_ADDR = 0x21
SET_PAGE_ADDR = 0x22
SET_DISP_START_LINE = 0x40
SET_SEG_REMAP = 0xA0
SET_MUX_RATIO = 0xA8
SET_COM_OUT_DIR = 0xC0
SET_DISP_OFFSET = 0xD3
SET_COM_PIN_CFG = 0xDA
SET_DISP_CLK_DIV = 0xD5
SET_PRECHARGE = 0xD9
SET_VCOM_DESEL = 0xDB
SET_CHARGE_PUMP = 0x8D


class SSD1306(Display):
    """ Object to operate SSD1331-based OLED displays via its SPI interface

    Parameters:

        spi (SPI_with_CS): SPI_with_CS instance (SPI object with chip select handling)

        cs_pin (machine.Pin): pin that controls the display's chip select line. The pin mode must be set by the user.

        cd_pin (machine.Pin): pin that controls the display's command/data line. The pin mode must be set by the user.

        res_pin (machine.Pin): pin that controls the display's reset line. The pin mode must be set by the user.

    """

    WIDTH = 128
    HEIGHT = 64
    BYTES_PER_PIXEL = 1



    def __init__(self, i2c, address, port):

        super().__init__()
        self.i2c = i2c
        self.cmd = bytearray((0x40, 0)) # command bytes. The last two bytes are updated as needed

        self.address = address
        self.port = port

        self.width = self.WIDTH
        self.height = self.HEIGHT
        # self.buffer = bytearray(self.pages * self.width)

        self.external_vcc = True
        self.pages = self.height // 8
        # super().__init__(self.buffer, self.width, self.height, framebuf.MONO_VLSB)
        # self.init_display()
        self.buffer = bytearray(self.pages * self.width)

    def select(self):
        """
        Selects the proper I2C port to talk to this device.
        """
        self.i2c.select_bus(self.port)

    def write_command(self, data):
        """ Writes data bytes

        Parameters:

            data (list of int or bytes): list of integers representing the
                command bytes to send to the display. Can also be a byte
                string or bytearray.

        """
        if isinstance(data, int):
            self.i2c.write_read(self.address, [0x80, data])
        else:
            self.i2c.write_read(self.address, [0x80] + list(data))

    def write_data(self, data):
            self.i2c.write_read(self.address, bytearray([0x40] + list(data)))

    def init(self):
        """ Initializes the display controller to the desired display mode
        """
        for cmd in (

            SET_DISP | 0x00,  # off
            # address setting
            SET_MEM_ADDR,
            0x00,  # horizontal
            # resolution and layout
            SET_DISP_START_LINE | 0x00,
            SET_SEG_REMAP | 0x01,  # column addr 127 mapped to SEG0
            SET_MUX_RATIO,
            self.height - 1,
            SET_COM_OUT_DIR | 0x08,  # scan from COM[N] to COM0
            SET_DISP_OFFSET,
            0x00,
            SET_COM_PIN_CFG,
            0x02 if self.width > 2 * self.height else 0x12,
            # timing and driving scheme
            SET_DISP_CLK_DIV,
            0x80,
            SET_PRECHARGE,
            0x22 if self.external_vcc else 0xF1,
            SET_VCOM_DESEL,
            0x30,  # 0.83*Vcc
            # display
            SET_CONTRAST,
            0xFF,  # maximum
            SET_ENTIRE_ON,  # output follows RAM contents
            SET_NORM_INV,  # not inverted
            # charge pump
            SET_CHARGE_PUMP,
            0x10 if self.external_vcc else 0x14,
            SET_DISP | 0x01,
        ):
            self.write_command(cmd)

        # self.write_command((0xAF,))  # display ON

        # self.write_command((0x26, 1))  # Enable rectangle fill

        super().init()

    def reset(self):
        """ Pulses the hardware reset line of the display
        """

        # All the display needs to be refreshed
        self.fb_y0 = 0
        self.fb_y1 = 63



    def write_frame_buffer(self, y0=None, y1=None):
        """ Sends the specified lines of the frame buffer to the hardware display.

        If no lines are specified, only the block of lines that were modified since the last call are updated.

        Parameters:

            y0, y1 (int): first and last line of the block to be updated. If None, the higest/lowest line modified since the last call is used.

        """
        # sets the window
        # self.cmd[4] = y0 = y0 if y0 is not None else self.fb_y0
        # self.cmd[5] = y1 = y1 if y1 is not None else self.fb_y1
        # if y1 < 0:
        #     return
        # with self.spi:
        #     self._write_command(self.cmd) # send the window command
        #     self._write_data(self.fb[y0 * self.BYTES_PER_LINE: (y1+1) * self.BYTES_PER_LINE]) # fb is a memoryview, indexing does not allocate new memory
        # self.fb_y0 = self.HEIGHT - 1
        # self.fb_y1 = -1 # -1 is faster to check than y0 > y1

        # tb = time.ticks_cpu()
        # t1 = time.ticks_ms()
        # self.write_frame_buffer(y, y+7)
        # t2= time.ticks_ms()
        # print(f'draw={t1-t0} ms, refresh={t2-t1} ms, buf access={tb-ta} cycles')

        x0 = 0
        x1 = self.width - 1
        if self.width == 64:
            # displays with width of 64 pixels are shifted by 32
            x0 += 32
            x1 += 32
        self.write_command(SET_COL_ADDR)
        self.write_command(x0)
        self.write_command(x1)
        self.write_command(SET_PAGE_ADDR)
        self.write_command(0)
        self.write_command(self.pages - 1)
        self.write_data(self.buffer[:8])



    def display_on(self):
        self.write_command((SET_DISP | 0x01,))

    def display_off(self):
        self.write_command((SET_DISP | 0x00,))

    def poweroff(self):
        self.write_cmd(SET_DISP | 0x00)

    def poweron(self):
        self.write_cmd(SET_DISP | 0x01)

    def contrast(self, contrast):
        self.write_cmd(SET_CONTRAST)
        self.write_cmd(contrast)

    def invert(self, invert):
        self.write_cmd(SET_NORM_INV | (invert & 1))


    def _set_brightness(self, brightness):
        if not brightness:
            self.write_command((0xAE,))  # display OFF
        else:
            self.write_command((0xAF, 0x87, min(15, brightness-1))) # dislay ON, set brightness


    def set_window(self, x1, y1, x2, y2):
        self.write_command((0x15, x1, x2, 0x75, y1, y2))

    def draw_color_bitmap(self, x, y, width, height, data):
        self.set_window(x, y, x + width - 1, y + height - 1)
        for d in data:
            r = (d >> 11) & 0b11111
            g = (d >> 5) & 0b111111
            b = d & 0b11111
            self.write_data([r << 3 | (g & 0b111), (g & 0b111) | b << 3])

    def draw_line(self, x1, y1, x2, y2, r=255, g=255, b=255):
        self.write_command((0x21, x1, y1, x2, y2, r, g, b))
        time.sleep(0.001)

    def draw_rect(self, x1, y1, x2, y2, line_r=255, line_g=255, line_b=255, fill_r=0, fill_g=0, fill_b=0):
        self.write_command((0x22, x1, y1, x2, y2, line_r,
                           line_g, line_b, fill_r, fill_g, fill_b))
        time.sleep(0.001)

    def copy(self, src_x1, src_y1, src_x2, src_y2, dest_x, dest_y):
        self.write_command(
            (0x23, src_x1, src_y1, src_x2, src_y2, dest_x, dest_y))
        time.sleep(0.001)

    def dim_rect(self, x1=0, y1=0, x2=95, y2=63):
        """ Reduce the intensity of the pixels in the specified rectangle. Subsequent calls have no effect.
        """
        self.write_command((0x24, x1, y1, x2, y2))
        time.sleep(0.001)

    def set_master_intensity(self, attn=15):
        """ Sets the master display intensity, from 0 to 15.
        """
        self.write_command((0x87, attn & 0x0F))

    def set_dim(self, dim=255):
        """ Sets the display dim level.

        The dim command seems to erase the display memory, so the frame buffer has to be sent back.
        This causes flicker.
        We cannot completely extinguish the pixels with dim=0.
        """
        self.write_command((0xAB, 0, dim,dim,dim,31))
        self.write_frame_buffer(0, 63, cmd=0xAC)
        # self.write_command((0xAC, ))

    def clear_display(self, x1=0, y1=0, x2=WIDTH-1, y2=HEIGHT-1):
        """ Clears the display's pixels in the specified rectangle coordinates.

        This operates on the display directly, using the hardware clear command.
        The frame buffer is unaffected.
        If no arguments are provided, the whole display is cleared.

        Parameters:

            x1, y1, x2, y2 (int): coordinates of the rectangles to be cleared
        """
        self.write_command((0x25, x1, y1, x2, y2))
        time.sleep(0.001)
        # All the display needs to be refreshed
        self.fb_y0 = 0
        self.fb_y1 = 63

    def set_fill(self, ena, rev_copy=False):
        a = 0x00
        if ena:
            a |= 0x01
        if rev_copy:
            a |= 0x10
        self.write_command((0x26, a))

    # Valid time intervals: 6, 10, 100 or 200 frames

    def set_scroll(self, nb_offset_cols, start_row, nb_rows, nb_offset_rows, time_interval=100):
        TIME_INTERVALS = {6: 0x00, 10: 0x01, 100: 0x2, 200: 0x3}
        if time_interval in TIME_INTERVALS:
            self.write_command([0x27, nb_offset_cols, start_row,
                               nb_rows, nb_offset_rows, TIME_INTERVALS[time_interval]])

    def stop_scroll(self):
        self.write_command((0x2E,))

    def start_scroll(self):
        self.write_command((0x2F,))
