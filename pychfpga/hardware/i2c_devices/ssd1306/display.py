import time

from . import font


class Display:
    # Display geometry
    WIDTH = None
    HEIGHT = None
    BYTES_PER_PIXEL = None

    # Basic colors
    WHITE = 0b11111_111111_11111 # R5_G6_B5 format
    BLACK = 0b00000_000000_00000
    YELLOW = 0b11111_111111_00000
    GREEN = 0b00000_111111_00000
    BLUE = 0b00000_000000_11111

    def __init__(self):
        self.BYTES_PER_LINE = self.WIDTH * self.BYTES_PER_PIXEL
        self.fb = memoryview(bytearray(self.BYTES_PER_LINE * self.HEIGHT)) # frame buffer, 2 bytes per pixel
        self.zeros = memoryview(bytearray(self.BYTES_PER_LINE)) # preallocate a line of zeros for efficiency


        self.fb_y0 = 0  # current lowest modified frame buffer line
        self.fb_y1 = self.HEIGHT-1  # current highest modified frame buffer line

        self.brightness = 0 # dim level: 0= display off, 1: min brightness, 16: max brightness
        self.last_time = time.time()

        # current font information
        self.font = None  # Font bitmap table
        self.font_width = None  # font horizontal pitch in pixels
        self.font_height = None # font vertical pitch in pixels

        self.set_font(5)
        self.text_x = 0
        self.text_y = 0
        self.fg = self.WHITE
        self.bg = self.BLACK

    def init(self):
        self.set_brightness(16)
        self.clear()


    def set_brightness(self, brightness=16):
        if brightness == 16:
            self.last_time = time.time()
        if self.brightness == brightness:
            return
        self._set_brightness(brightness)
        self.brightness = brightness

    def write_frame_buffer(self, y0=None, y1=None):
        """ Sends the specified lines of the frame buffer to the hardware display.

            This method should be provided by the hardware-specific subclass.
        """
        raise NotImplementedError()

    def update(self, y0=None, y1=None):
        self.write_frame_buffer(y0=y0, y1=y1)

    def clear(self, update=True):
        addr = 0
        fb = self.fb
        for j in range(self.HEIGHT):
            fb[addr: addr + self.BYTES_PER_LINE] = self.zeros
            addr += self.BYTES_PER_LINE
        self.fb_y0 = 0
        self.fb_y1 = self.HEIGHT - 1
        self.text_x = self.text_y = 0
        if update:
            self.write_frame_buffer()

    @classmethod
    def encode_color(cls, r, g, b):
        """ Convert a RGB value into an integer color code that is easily usable by the display

        Format: 16-bit color code: RRRRRGGGGGGBBBBB (i.e. R5G6B5)

        Parameters:
            r,g,b (int): values between 0-255

        Returns:
            int: 16-bit color code
        """
        return (r & 0b11111000) << 8 | (g & 0b11111100) << 3 | (b >> 3)

    def hline(self, x0, x1, y, color = WHITE):
        """ Draws an horizontal line in the frame buffer

        Parameters:

            x0, x1, y (int): coordinates of the line. Line will be drawn between (x0,y) and (x1,y).

        """
        fb = self.fb
        a = (x0 + y * self.WIDTH) * self.BYTES_PER_PIXEL
        for i in range(x1 - x0):
            fb[a] = color >> 8; a +=1
            fb[a] = color & 0xFF; a +=1
        self.fb_y0 = min(self.fb_y0, y)
        self.fb_y1 = max(self.fb_y1, y)

    def vline(self, x, y0, y1, color = WHITE):
        """ Draws an vertical line in the frame buffer

        Parameters:

            x, y0, y1 (int): coordinates of the line. Line will be drawn between (x,y0) and (x,y1).

        """
        fb = self.fb
        a = (x + y0 * self.WIDTH) * self.BYTES_PER_PIXEL
        for i in range(y1 - y0):
            fb[a] = color >> 8
            fb[a+1] = color & 0xFF
            a += self.BYTES_PER_LINE
        self.fb_y0 = min(self.fb_y0, y0)
        self.fb_y1 = max(self.fb_y1, y1)

    def draw_row_wise_mono_bitmap(self, x: int, y: int, data: list, width=8, height=8, fg=WHITE,  bg=BLACK) -> None:
        """ Writes a 8x8 monochrome bitmap in the frame buffer.

        The routine is optimized to be efficient in micropython.
        As currently written, it works only for BYTES_PER_PIXEL=2, with R=5 bits, G=6 bits and  B=5 bits.

        Parameters:

            x, y (int): coordinate of the upper-left corner of the bitmap

            r, g, b (int): foreground color (0-255)

            bg_r, bg_g, bg_b: background color (0-255)
        """
        # t0 = time.ticks_ms()
        fb = self.fb
        addr = (x + y * self.WIDTH) * self.BYTES_PER_PIXEL
        # ta = time.ticks_cpu()
        for j in range(height): # scan rows
            d = data[j]
            a = addr
            for i in range(width): # scan columns
                if (d & (0x80 >> i)):
                    fb[a] = fg >> 8; a +=1
                    fb[a] = fg & 0xFF; a +=1
                else:
                    fb[a] = bg >> 8; a +=1
                    fb[a] = bg & 0xFF; a +=1
            addr += self.BYTES_PER_LINE

        # expand the refresh zone to include modified lines
        self.fb_y0 = min(self.fb_y0, y)
        self.fb_y1 = max(self.fb_y1, y + height -1)

    def draw_col_wise_mono_bitmap(self, x: int, y: int, data: list, width=5, height=7, fg=WHITE, bg=BLACK) -> None:
        """ Writes a 5x7 monochrome bitmap in the frame buffer. Data bytes represent columns.

        Parameters:

            x, y (int): coordinate of the upper-left corner of the bitmap

            r, g, b (int): foreground color (0-255)

            bg_r, bg_g, bg_b: background color (0-255)
        """
        # t0 = time.ticks_ms()
        fb = self.fb
        addr = (x + y * self.WIDTH) * self.BYTES_PER_PIXEL
        # ta = time.ticks_cpu()
        for col in range(width): # scan columns
            d = data[col]
            a = addr
            for row in range(height): # scan rows
                if (d & (1 << row)):
                    fb[a] = fg >> 8;
                    fb[a + 1] = fg & 0xFF;
                else:
                    fb[a] = bg >> 8;
                    fb[a + 1] = bg & 0xFF;
                a += self.BYTES_PER_LINE
            addr += self.BYTES_PER_PIXEL

        # expand the refresh zone to include modified lines
        self.fb_y0 = min(self.fb_y0, y)
        self.fb_y1 = max(self.fb_y1, y + height -1)

    def set_font(self, font_size):
        if font_size==8:
            self.font = font.font8x8
            self.font_width = 8
            self.font_height = 8
            self.font_is_row_wise = True
        else:
            self.font = font.font5x7
            self.font_width = 5
            self.font_height = 7
            self.font_is_row_wise = False

    def set_fg_color(self, color):
        if isinstance(color, tuple):
            self.fg = self.encode_color(color)
        else:
            self.fg = color

    def set_bg_color(self, color):
        if isinstance(color, tuple):
            self.bg = self.encode_color(color)
        else:
            self.bg = color

    def print(self, text, x=None, y=None, fg=None, bg=None, update=True, font_size=None):
        """ Print text in the frame buffer

        Parameters:

            text (str): string to print

            x, y (int): coordinate of where to start to print. If not specified, continues from last current location.

            fg, bg (int): sets the foreground and background color by calling ``set_fg_color()`` and ``set_bg_color`` . If not specified, the current colors are used.

            update (bool): if True, the the frame buffer is sent to the display after the print is complete.

            font_size (int): Indicates which font to use by calling ``set_font()``. If not specified, the current font is used.

        """
        if font_size:
            self.set_font(font_size)
        if x is not None:
            self.text_x = x
        if y is not None:
            self.text_y = y
        if fg is not None:
            self.set_fg_color(fg)
        if bg is not None:
            self.set_bg_color(bg)
        font = self.font
        font_width = self.font_width
        font_height = self.font_height
        font_is_row_wise = self.font_is_row_wise

        # print(f'Printing {text} at {self.text_x=}, {self.text_y=}')
        for c in text:
            if c == '\r':
                self.text_x = 0
                continue
            elif c == '\n':
                self.text_y += font_height
                continue
            if self.text_x + font_width > self.WIDTH:
                self.text_x = 0
                self.text_y += font_height

            if font_is_row_wise: # implied 8x8 font with row-wise encoding
                cc = ord(c) * self.font_height # x8
                bitmap = font[cc: cc + font_height]
                self.draw_row_wise_mono_bitmap(self.text_x, self.text_y, bitmap, width=font_width, height=font_height, fg=self.fg, bg=self.bg)
            else: # implied 5x7 font with column-wise encoding
                cc = ord(c) * self.font_width
                bitmap = font[cc: cc + font_width]
                self.draw_col_wise_mono_bitmap(self.text_x, self.text_y, bitmap, width=font_width, height=font_height, fg=self.fg, bg=self.bg)
            self.text_x += font_width

            # wrap text
            if self.text_x >= self.WIDTH:
                self.text_x = 0
                self.text_y += font_height

        # print(f' {self.fb_y0=}, {self.fb_y1=}')

        if update:
            self.write_frame_buffer()

    def test_text(self,r=255, g=255, b=255):
        self.clear_frame_buffer()
        self.write_frame_buffer()
        t0 = time.ticks_ms()
        for x in range(96):
            c = 8*(x+32)
            self.draw_8x8_mono_bitmap2((x % 12) * 8, x//12 * 8, font[c: c+8], r, g, b)
        t1 = time.ticks_ms()
        self.write_frame_buffer()
        t2 = time.ticks_ms()
        print(f'draw={t1-t0} ms, refresh={t2-t1} ms')
