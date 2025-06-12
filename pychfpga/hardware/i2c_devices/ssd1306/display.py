import time


import freetype

from . import font


class Display:
    # Display geometry
    WIDTH = None
    HEIGHT = None
    BYTES_PER_PIXEL = None

    # Basic colors
    WHITE = 1 # R5_G6_B5 format
    BLACK = 0

    def __init__(self):
        # self.BYTES_PER_LINE = self.WIDTH * self.BYTES_PER_PIXEL
        self.fb = memoryview(bytearray(self.WIDTH * self.HEIGHT//8)) # frame buffer, 2 bytes per pixel
        # self.zeros = memoryview(bytearray(self.BYTES_PER_LINE)) # preallocate a line of zeros for efficiency


        self.fb_y0 = 0  # current lowest modified frame buffer line
        self.fb_y1 = self.HEIGHT-1  # current highest modified frame buffer line

        self.brightness = 0 # dim level: 0= display off, 1: min brightness, 16: max brightness
        self.last_time = time.time()

        # current font information
        self.font_path = '/home/jfcliche/Downloads/Roboto/static/'
        self.font_name = 'Roboto-Medium.ttf'
        self.font_size = 8

        # self.font_path = '/home/jfcliche/Downloads/oldschool_pc_font_pack_v2.2_linux/ttf - Mx (mixed outline+bitmap)/'
        # self.font_name = 'Mx437_Acer_VGA_8x8.ttf'
        self.text_x = 0
        self.text_y = 0
        self.fg = self.WHITE
        self.bg = self.BLACK

    def init(self):
        # self.set_brightness(16)
        # self.clear()
        pass

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
        for addr in range(len(self.fb)):
            self.fb[addr] = 0
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
        for x in range(x0, x1+1):
            self.set_pixel(x, y, color)
        self.fb_y0 = min(self.fb_y0, y)
        self.fb_y1 = max(self.fb_y1, y)

    def vline(self, x, y0, y1, color = WHITE):
        """ Draws an vertical line in the frame buffer

        Parameters:

            x, y0, y1 (int): coordinates of the line. Line will be drawn between (x,y0) and (x,y1).

        """
        for y in range(y0, y1+1):
            self.set_pixel(x, y, color)
        self.fb_y0 = min(self.fb_y0, y0)
        self.fb_y1 = max(self.fb_y1, y1)

    def set_pixel(self, x, y, color):
        if x < 0 or x >= self.WIDTH or y < 0 or y >= self.HEIGHT-1:
            return
        addr = x + self.WIDTH*(y//8)
        bit = 1 << (y & 7)
        self.fb[addr] = (self.fb[addr] & ~bit) | (bit if color else 0)

    def draw_glyph(self, x, y, glyph, width, height, fg=1,  bg=0, rotate=False) -> None:
        """ Writes a freetype glyph in the frame buffer.

        Parameters:

            x, y (int): coordinate of the upper-left corner of the bitmap

            fg (int): foreground color

            bg: background color
        """
        # t0 = time.ticks_ms()
        fb = self.fb
        bm = glyph.bitmap
        # addr = (x + y * self.WIDTH) * self.BYTES_PER_PIXEL
        # ta = time.ticks_cpu()
        # voffset = height - glyph.bitmap_top
        for row in range(height):
            r = row - height + 1 + glyph.bitmap_top
            for col in range(width):
                color = (bm.buffer[bm.pitch*r+(col>>3)] >> (7-(col & 7))) & 1 if r >= 0 and r < bm.rows and col < bm.width else 0
                if rotate:
                    self.set_pixel(x+row, y-col, color)
                else:
                    self.set_pixel(x+col, y+row, color)

        # expand the refresh zone to include modified lines
        self.fb_y0 = min(self.fb_y0, y)
        self.fb_y1 = max(self.fb_y1, y + bm.rows -1)


    # def set_font(self, font_name, font_size):
    #     if font_name is not None:
    #         self.font_name = font_name

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

    def print(self, text, x=None, y=None, fg=None, bg=None, update=True, font_name=None, font_size=None, rotate=False):
        """ Print text in the frame buffer

        Parameters:

            text (str): string to print

            x, y (int): coordinate of where to start to print. If not specified, continues from last current location.

            fg, bg (int): sets the foreground and background color by calling ``set_fg_color()`` and ``set_bg_color`` . If not specified, the current colors are used.

            update (bool): if True, the the frame buffer is sent to the display after the print is complete.

            font_size (int): Indicates which font to use by calling ``set_font()``. If not specified, the current font is used.

        """
        if font_name:
            self.font_name = font_name
        if font_size:
            self.font_size = font_size
        if x is not None:
            self.text_x = x
        if y is not None:
            self.text_y = y
        if fg is not None:
            self.set_fg_color(fg)
        if bg is not None:
            self.set_bg_color(bg)

        face = freetype.Face(self.font_path + self.font_name)
        face.set_pixel_sizes(0, self.font_size)
        font_height = font_size
        # print(f'Printing {text} at {self.text_x=}, {self.text_y=}')
        for c in text:
            if c == '\r':
                if rotate:
                    self.text_y = self.HEIGHT-1
                else:
                    self.text_x = 0
                continue
            elif c == '\n':
                if rotate:
                    self.text_x += font_height
                else:
                    self.text_y += font_height
                continue


            face.load_char(ord(c), freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_MONO)
            glyph = face.glyph
            bitmap = glyph.bitmap
            font_width = bitmap.width
            advance = (face.glyph.linearHoriAdvance+65535)//65536

            if rotate and self.text_y < font_width-1:
                self.text_y = self.HEIGHT-1
                self.text_x += font_height
                print('ret')
            elif not rotate and self.text_x + font_width -1 >= self.WIDTH:
                self.text_x = 0
                self.text_y += font_height

            print(f' char {c} @ {self.text_x}, {self.text_y}, width={font_width}, adv={advance}')

            self.draw_glyph(self.text_x, self.text_y, glyph, width= advance, height=font_height, fg=self.fg, bg=self.bg, rotate=rotate)

            if rotate:
                self.text_y -= advance
                if self.text_y < 0:
                    self.text_y = self.HEIGHT-1
                    self.text_x += font_height
            else:
                self.text_x += advance
                if self.text_x >= self.WIDTH:
                    self.text_x = 0
                    self.text_y += font_height

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
